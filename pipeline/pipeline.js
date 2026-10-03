'use strict';
/**
 * The decision pipeline as a LangGraph StateGraph. Cheap-to-expensive, stopping early:
 *
 *   START -> prefilter ──skip──────────────────────────────► END
 *              └► cross_repo ──blocked──► fallback ─────────► END
 *                    └► similarity ──near-duplicate──► END (commit re-keyed)
 *                          └► gar ► write_draft ► judge ──pass──► publish ► END
 *                                      ▲      │fail
 *                                      │      ├─ grounded but awkward ─► polish_draft ─► judge
 *                                      ├──────┤ retries left
 *                                      └ widen┤ cap reached (once)
 *                                             └─ cap reached again ───► fallback ► END
 *
 * Every node is wrapped by `traced()`, which appends a structured event to `state.trail`
 * and to the optional `deps.logger` (console + FactStore node_logs), so each stage of
 * each run is auditable and shows up in LangGraph's checkpointed history.
 */
const path = require('path');
const { StateGraph, Annotation, START, END, MemorySaver } = require('@langchain/langgraph');
const { prefilter } = require('./prefilter');
const { crossRepoCheck } = require('./registry');
const { judge, evaluate } = require('./critic');
const { chunkMarkdown } = require('./chunker');
const W = require('./writer');
const P = require('./prompts');

const GROUNDED_ONLY_TAGS = new Set(['style_mismatch', 'judge_low_confidence']);
const concat = (a, b) => a.concat(b);
const last = (_, b) => b;

const State = Annotation.Root({
  change: Annotation(),
  trail: Annotation({ reducer: concat, default: () => [] }),
  attempts: Annotation({ reducer: concat, default: () => [] }),
  metrics: Annotation({ reducer: (a, b) => ({ ...a, ...b }), default: () => ({}) }),
  forced: Annotation(),
  hypothetical: Annotation(),
  templatePath: Annotation(),
  knownFacts: Annotation(),
  styleText: Annotation(),
  iter: Annotation({ reducer: last, default: () => 0 }),
  widened: Annotation({ reducer: last, default: () => false }),
  polished: Annotation({ reducer: last, default: () => false }),
  feedback: Annotation({ reducer: last, default: () => [] }),
  draft: Annotation(),
  verdict: Annotation(),
  failure: Annotation({ reducer: last, default: () => null }),
  accepted: Annotation({ reducer: last, default: () => null }),
  decision: Annotation(),
});

function targetPathFor({ filePath, policy, subfolder, docsRoot }) {
  const rel = filePath.replace(/\.txt$/, '.md').replace(/^(docs\/|documentation\/)/, '');
  const base = policy.targetPath || path.join(docsRoot, 'services', policy.serviceName);
  return subfolder ? path.join(base, subfolder, rel) : path.join(base, rel);
}

const dedupe = (chunks) => {
  const seen = new Set();
  return chunks.filter((c) => (seen.has(c.id) ? false : seen.add(c.id)));
};

async function retrieve({ vectors, llm, repo, after, hypothetical, topK }) {
  const [ragVec, garVec] = await llm.embed([after.slice(0, 2000), hypothetical]);
  const [rag, gar] = await Promise.all([
    vectors.search(ragVec, { limit: topK, repo }),
    vectors.search(garVec, { limit: topK, repo }),
  ]);
  return dedupe([...gar, ...rag]).slice(0, topK)
    .map((h) => ({ id: h.id, score: h.score, heading: h.payload.heading, text: h.payload.text }));
}

/**
 * @param {object} change { repo, filePath, before, after (null = deleted), commit }
 * @param {object} deps   { cfg, llm, vectors, facts, registry, policy, instructions, templateFiles,
 *                          defaultTemplate, runId, githubHost?, logger?, escalate? }
 */
async function processChange(change, deps) {
  const { cfg, llm, vectors, facts, registry, policy, runId } = deps;
  const t = cfg.thresholds;
  const base = { runId, repo: change.repo, path: change.filePath, commit: change.commit };
  const mode = change.kind === 'code' ? 'code' : 'docs';
  const style = P.resolveStyle(deps.styles || P.loadStyles(), change.styleKey || policy.style);
  const styleTxt = P.styleText(style, policy);
  const changedFiles = change.changedFiles || [];
  base.mode = mode;
  base.style = style.key;

  /** Wrap a node: time it, log it, append to the trail. A node returns { update, note, status }. */
  const traced = (name, fn) => async (state) => {
    const started = Date.now();
    let out;
    try {
      out = await fn(state);
    } catch (err) {
      if (deps.logger) {
        await deps.logger.log({ ...base, node: name, status: 'error', ms: Date.now() - started, note: { error: err.message }, at: new Date().toISOString() });
      }
      throw err;
    }
    const { update = {}, note = {}, status = 'ok' } = out;
    const event = { ...base, node: name, status, ms: Date.now() - started, note, at: new Date().toISOString() };
    if (deps.logger) await deps.logger.log(event);
    return { ...update, trail: [event] };
  };

  const finish = (state, d) => ({ ...base, attempts: state.attempts || [], metrics: state.metrics || {}, action: 'none', ...d });
  const withMetrics = (s, m) => ({ ...s, metrics: { ...s.metrics, ...m } });

  const nodes = {
    prefilter: traced('prefilter', async (s) => {
      if (change.after == null) {
        const auto = policy.trust === 'auto';
        return {
          status: 'deleted', note: { reason: 'source document removed' },
          update: {
            decision: finish(s, {
              outcome: auto ? 'published' : 'pending_review', reviewerAction: auto ? 'auto_published' : 'needs_review',
              reason: 'source document removed', action: 'delete',
              targetPath: targetPathFor({ filePath: change.filePath, policy, docsRoot: cfg.paths.docsRoot }),
            }),
          },
        };
      }
      const pre = prefilter({ before: change.before || '', after: change.after, minDiffLines: t.minDiffLines });
      const update = { metrics: { prefilter: pre.metrics }, forced: pre.forced };
      if (!pre.proceed) {
        return {
          status: 'stop', note: { tag: pre.tag, reason: pre.reason, ...pre.metrics.diff },
          update: { ...update, decision: finish(withMetrics(s, update.metrics), { outcome: 'skipped', reviewerAction: 'none', rootCauseTag: pre.tag, reason: pre.reason }) },
        };
      }
      return { note: { reason: pre.reason, forced: pre.forced, ...pre.metrics.diff }, update };
    }),

    cross_repo: traced('cross_repo', async (s) => {
      const symbols = Object.keys(registry).filter((x) => change.after.includes(x));
      if (!symbols.length) return { status: 'skip', note: { registered: [] } };
      const cross = await crossRepoCheck({ symbols, repo: change.repo, registry, factstore: facts });
      const update = { metrics: { crossRepo: cross } };
      if (cross.complete) return { note: { registered: cross.registered }, update };
      const detail = cross.incomplete.map((i) => `${i.symbol} awaiting ${i.missing.join(', ')}`).join('; ');
      return {
        status: 'stop', note: { incomplete: cross.incomplete },
        update: {
          ...update,
          decision: finish(withMetrics(s, update.metrics), {
            outcome: 'fallback', reviewerAction: 'auto_rejected', rootCauseTag: 'cross_repo_incomplete',
            reason: `feature not available end to end: ${detail}`, draft: null,
          }),
        },
      };
    }),

    similarity: traced('similarity', async (s) => {
      let best;
      let hypothetical;
      if (mode === 'code') {
        // Code and prose are not comparable. Embed the GAR paragraph (what the docs *would* say)
        // and compare it with what the page already says.
        hypothetical = await W.generateHypothetical(llm, { filePath: change.filePath, before: change.before, after: change.after, mode, changedFiles });
        const [v] = await llm.embed([hypothetical]);
        const [hit] = await vectors.search(v, { limit: 1, repo: change.repo, path: change.filePath });
        best = [hit || { id: null, score: 0 }];
      } else {
        // Chunk-to-chunk, the same granularity as the index.
        const newChunks = chunkMarkdown(change.after);
        const vecs = await llm.embed(newChunks.map((c) => `${c.heading}\n${c.text}`));
        best = [];
        for (const v of vecs) {
          const [hit] = await vectors.search(v, { limit: 1, repo: change.repo });
          best.push(hit || { id: null, score: 0 });
        }
      }
      const min = best.length ? Math.min(...best.map((b) => b.score)) : 0;
      const update = { metrics: { minChunkSimilarity: min }, ...(hypothetical ? { hypothetical } : {}) };
      const note = { mode, minChunkSimilarity: min, chunks: best.length, threshold: t.similarityHigh, forced: !!s.forced };
      if (!s.forced && best.length && min >= t.similarityHigh) {
        await vectors.touchCommit(best.map((b) => b.id).filter(Boolean), change.commit);
        return {
          status: 'stop', note,
          update: {
            ...update,
            decision: finish(withMetrics(s, update.metrics), {
              outcome: 'refreshed', reviewerAction: 'none',
              reason: `${mode === 'code' ? 'the page already says this' : `all ${best.length} chunk(s)`} (similarity ${min.toFixed(3)} >= ${t.similarityHigh}); re-keyed to ${change.commit.slice(0, 7)}`,
            }),
          },
        };
      }
      return { note, update };
    }),

    gar: traced('gar', async (s) => {
      const hypothetical = s.hypothetical
        || await W.generateHypothetical(llm, { filePath: change.filePath, before: change.before, after: change.after, mode, changedFiles });
      const templatePath = await W.selectTemplate(llm, {
        filePath: change.filePath, content: change.after, templateFiles: deps.templateFiles, defaultTemplate: deps.defaultTemplate,
      });
      const knownFacts = await facts.approvedClaims(change.repo, change.filePath);
      return {
        note: { template: path.basename(templatePath || ''), knownFacts: knownFacts.length, style: style.key, styleFallback: style.fallback, reusedHypothetical: !!s.hypothetical },
        update: { hypothetical, templatePath, knownFacts, styleText: styleTxt },
      };
    }),

    write_draft: traced('write_draft', async (s) => {
      const topK = s.widened ? t.topKWidened : t.topK;
      const context = await retrieve({ vectors, llm, repo: change.repo, after: change.after, hypothetical: s.hypothetical, topK });
      const out = await W.draftDocument(llm, {
        mode, filePath: change.filePath, source: change.after, existing: change.existing, changedFiles,
        templatePath: s.templatePath, context, policy, style, instructions: deps.instructions, feedback: s.feedback,
      });
      return {
        note: { attempt: s.iter + 1, widened: s.widened, topK, contextChunks: context.length, feedbackItems: s.feedback.length, prompt: out.promptId, style: style.key },
        update: { iter: s.iter + 1, polished: false, draft: out.text },
      };
    }),

    judge: traced('judge', async (s) => {
      const verdict = await judge(llm, { mode, source: change.after, draft: s.draft, knownFacts: s.knownFacts, styleText: s.styleText, existing: change.existing, changedFiles });
      const failure = evaluate(verdict, t);
      const polishable = failure && GROUNDED_ONLY_TAGS.has(failure.tag) && !s.polished;
      const scores = { precision: verdict.precision, recall: verdict.recall, style: verdict.style, quality: verdict.quality };
      const note = { ...scores, failure: failure?.tag || null, willPolish: !!polishable, prompt: verdict.promptId };
      if (polishable) return { note, update: { verdict, failure } };
      const attempt = { n: s.iter, widened: s.widened, topK: s.widened ? t.topKWidened : t.topK, ...scores, failure: failure?.tag || null };
      return {
        note,
        update: { verdict, failure, attempts: [attempt], feedback: failure?.feedback || [], accepted: failure ? null : { draft: s.draft, verdict } },
      };
    }),

    polish_draft: traced('polish_draft', async (s) => {
      const draft = await W.polishOnly(llm, { draft: s.draft, policy, style, instructions: deps.instructions });
      return { note: { reason: s.failure.tag }, update: { draft, polished: true } };
    }),

    widen: traced('widen', async () => ({
      note: { topK: t.topKWidened, reason: 'iteration cap reached; one automatic retry with expanded context' },
      update: { widened: true, feedback: [] },
    })),

    publish: traced('publish', async (s) => {
      const { draft, verdict } = s.accepted;
      const subfolder = await W.classifyFolder(llm, { filePath: change.filePath, content: draft, folderSpec: W.extractFolderSpec(deps.instructions) });
      const host = deps.githubHost || 'github.com';
      const sourceUrl = mode === 'code'
        ? `https://${host}/${change.repo}/tree/${change.commit}`
        : `https://${host}/${change.repo}/blob/${change.commit}/${change.filePath}`;
      const content = W.withFrontmatter(draft, { filePath: change.filePath, sourceUrl, commit: change.commit, docKey: change.filePath });
      await facts.saveClaims({ repo: change.repo, filePath: change.filePath, commit: change.commit, claims: verdict.claims });
      const auto = policy.trust === 'auto';
      const final = { precision: verdict.precision, recall: verdict.recall, style: verdict.style, quality: verdict.quality };
      return {
        note: { trust: policy.trust, subfolder, claimsStored: verdict.claims.length },
        update: {
          decision: finish(s, {
            outcome: auto ? 'published' : 'pending_review', reviewerAction: auto ? 'auto_published' : 'needs_review',
            reason: `passed all checks in ${s.attempts.length} attempt(s)`, action: 'write', content, subfolder,
            targetPath: targetPathFor({ filePath: change.filePath, policy, subfolder, docsRoot: cfg.paths.docsRoot }),
            metrics: { ...s.metrics, final },
          }),
        },
      };
    }),

    fallback: traced('fallback', async (s) => {
      // Cross-repo blocks arrive with a decision already built; judge exhaustion builds it here.
      const d = s.decision || finish(s, {
        outcome: 'fallback', reviewerAction: 'auto_rejected', rootCauseTag: 'iteration_cap_exceeded',
        reason: `reflection loop did not converge in ${s.attempts.length} attempt(s); last check: ${s.failure?.tag}`,
        draft: s.draft, feedback: s.failure?.feedback,
      });
      const ticket = deps.escalate ? await deps.escalate(d) : null;
      return { status: 'fallback', note: { rootCauseTag: d.rootCauseTag, ticket }, update: { decision: { ...d, ticket } } };
    }),
  };

  const afterPrefilter = (s) => (s.decision ? END : 'cross_repo');
  const afterCross = (s) => (s.decision ? 'fallback' : 'similarity');
  const afterSimilarity = (s) => (s.decision ? END : 'gar');
  const afterJudge = (s) => {
    if (s.accepted) return 'publish';
    if (s.failure && GROUNDED_ONLY_TAGS.has(s.failure.tag) && !s.polished) return 'polish_draft';
    const cap = s.widened ? t.maxIterations * 2 : t.maxIterations;
    if (s.iter < cap) return 'write_draft';
    return s.widened ? 'fallback' : 'widen';
  };

  const app = new StateGraph(State)
    .addNode('prefilter', nodes.prefilter)
    .addNode('cross_repo', nodes.cross_repo)
    .addNode('similarity', nodes.similarity)
    .addNode('gar', nodes.gar)
    .addNode('write_draft', nodes.write_draft)
    .addNode('judge', nodes.judge)
    .addNode('polish_draft', nodes.polish_draft)
    .addNode('widen', nodes.widen)
    .addNode('publish', nodes.publish)
    .addNode('fallback', nodes.fallback)
    .addEdge(START, 'prefilter')
    .addConditionalEdges('prefilter', afterPrefilter, ['cross_repo', END])
    .addConditionalEdges('cross_repo', afterCross, ['fallback', 'similarity'])
    .addConditionalEdges('similarity', afterSimilarity, ['gar', END])
    .addEdge('gar', 'write_draft')
    .addEdge('write_draft', 'judge')
    .addConditionalEdges('judge', afterJudge, ['publish', 'polish_draft', 'write_draft', 'widen', 'fallback'])
    .addEdge('polish_draft', 'judge')
    .addEdge('widen', 'write_draft')
    .addEdge('publish', END)
    .addEdge('fallback', END)
    .compile({ checkpointer: new MemorySaver() });

  const final = await app.invoke({ change }, {
    configurable: { thread_id: `${runId}:${change.repo}:${change.filePath}` },
    recursionLimit: 25 + t.maxIterations * 8,
    runName: 'docs-sync-decision',
    tags: ['docs-sync', change.repo],
  });
  return { ...final.decision, trail: final.trail };
}

module.exports = { processChange, targetPathFor, State };
