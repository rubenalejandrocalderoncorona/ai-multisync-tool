#!/usr/bin/env node
'use strict';
/**
 * Run the decision pipeline over the documents changed in a source repo checkout.
 *
 * Modes (config/repos.json -> repos[repo].mode): 'docs' (default) syncs changed docs files; 'code' drafts pages
 * from source-code changes; 'both' does both.
 *
 * Env:
 *   CHANGED_FILES   newline-separated doc paths (relative to source-repo/)
 *   SOURCE_REPO     org/repo            SOURCE_SHA   commit being synced
 *   SOURCE_BEFORE   previous commit (default: SOURCE_SHA~1)
 *   SOURCE_DIR      checkout dir (default: source-repo)
 *   FULL_SYNC       1 = (code mode) regenerate every declared page from the whole repo
 *   RUN_ID          correlation id (default: random)
 *   + AI_*, QDRANT_*, FACTSTORE_DATABASE_URL, see pipeline/config.js
 *
 * Writes: pipeline-results.json (decisions, no document bodies) and rejected/<file>.md drafts.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { repoPolicy } = require('../pipeline/config');
const { processChange } = require('../pipeline/pipeline');
const { escalate } = require('../pipeline/fallback');
const W = require('../pipeline/writer');
const { loadStyles } = require('../pipeline/prompts');
const { buildCodeChanges } = require('../pipeline/codesource');
const { buildDeps, applyDecision } = require('./lib');

const G = require('./gitutil');
const { syncContext } = require('../pipeline/context');
const { pagesScope } = require('../pipeline/codesource');

async function main() {
  const d = buildDeps();
  const { cfg } = d;
  const repo = process.env.SOURCE_REPO || 'unknown/unknown';
  const sourceDir = process.env.SOURCE_DIR || 'source-repo';
  const commit = process.env.SOURCE_SHA || G.head(sourceDir);
  const before = process.env.SOURCE_BEFORE || `${commit}~1`;
  const runId = process.env.RUN_ID || crypto.randomUUID();
  const files = (process.env.CHANGED_FILES || '').split(/\s+/).filter(Boolean);

  if (!cfg.ai.apiKey && !/^https?:\/\/(localhost|127\.|ollama)/.test(cfg.ai.baseUrl)) {
    throw new Error('INTERNAL_AI_API_KEY is required (or point AI_API_BASE_URL at a local OpenAI-compatible server).');
  }

  await d.facts.migrate();
  await d.vectors.ensureCollection();
  await d.codeVectors.ensureCollection();

  const policy = repoPolicy(d.reposConfig, repo);
  const instructions = fs.existsSync(cfg.paths.instructions) ? fs.readFileSync(cfg.paths.instructions, 'utf-8') : '';
  const templateFiles = W.findTemplateFiles(cfg.paths.templates);
  const defaultTemplate = path.join(cfg.paths.templates, 'default-template', 'default-template.md');
  console.log(`run ${runId} | ${repo}@${commit.slice(0, 7)} | trust=${policy.trust} | ${files.length} file(s)`);

  const styles = loadStyles(cfg.paths.styles);
  const logger = {
    // Console line for the runner log + durable row in the FactStore for the audit trail.
    async log(e) {
      const { note, ...head } = e;
      console.log(`    [${e.node}] ${e.status} ${e.ms}ms ${JSON.stringify(note)}`);
      try { await d.facts.recordNodeLog(e); } catch (err) { console.warn(`node log not persisted: ${err.message}`); }
      return head;
    },
  };
  const escalateFn = (dec) => escalate(dec, cfg.alerts);
  const results = [];
  fs.mkdirSync('rejected', { recursive: true });

  // CONTEXT STAGE: the vector DB must hold the whole repository before any change is analysed.
  // If it cannot be loaded, code-mode changes fall back (ticket, nothing published) instead of the run crashing.
  let contextError = null;
  if (policy.mode === 'code' || policy.mode === 'both') {
    const t0 = Date.now();
    const base0 = { runId, repo, path: '*', commit, node: 'sync_context', at: new Date().toISOString() };
    try {
      const prev0 = G.revExists(sourceDir, before) ? before : '';
      const stats = await syncContext({
        repo, commit, before: prev0, full: process.env.FULL_SYNC === '1', git: G.accessors(sourceDir),
        codeStore: d.codeVectors, docStore: d.vectors, llm: d.llm, facts: d.facts, pages: policy.pages || [], scope: pagesScope(policy.pages), exclude: policy.exclude || [],
      });
      await logger.log({ ...base0, status: 'ok', ms: Date.now() - t0, note: stats });
    } catch (e) {
      contextError = e;
      await logger.log({ ...base0, status: 'error', ms: Date.now() - t0, note: { error: e.message } });
    }
  }

  const targetBase = policy.targetPath || path.join(cfg.paths.docsRoot, 'services', policy.serviceName);
  const changes = [];
  if (policy.mode !== 'code') {
    for (const file of files) {
      const full = path.join(sourceDir, file);
      changes.push({
        repo, filePath: file, commit, kind: 'docs',
        before: G.readAt(sourceDir, before, file),
        after: fs.existsSync(full) ? fs.readFileSync(full, 'utf-8') : null,
      });
    }
  }
  if (policy.mode === 'code' || policy.mode === 'both') {
    const prev = G.revExists(sourceDir, before) ? before : ''; // first commit / new branch => document everything in scope
    changes.push(...buildCodeChanges({
      repo, policy, commit, before: prev, full: process.env.FULL_SYNC === '1',
      ...G.accessors(sourceDir),
      readExistingPage: (page) => {
        const f = path.join(targetBase, page);
        return fs.existsSync(f) ? fs.readFileSync(f, 'utf-8').replace(/^---\n[\s\S]*?\n---\n+/, '') : '';
      },
    }));
  }
  console.log(`mode=${policy.mode || 'docs'} | ${changes.length} change unit(s)`);

  for (const change of changes) {
    const file = change.filePath;
    let decision;
    try {
      if (contextError && change.kind === 'code') throw new Error(`context sync failed: ${contextError.message}`);
      decision = await processChange(change, {
        styles,
        cfg, llm: d.llm, vectors: d.vectors, codeVectors: d.codeVectors, facts: d.facts, registry: d.registry, policy, instructions,
        templateFiles, defaultTemplate, runId, githubHost: process.env.GIT_HOST, logger, escalate: escalateFn,
      });
    } catch (e) {
      // Infrastructure failure (AI/Qdrant/Postgres down): fail safe, never publish.
      decision = { runId, repo, path: file, commit, outcome: 'fallback', reviewerAction: 'auto_rejected', rootCauseTag: 'pipeline_error', reason: e.message, attempts: [], metrics: {}, action: 'none', trail: [] };
      decision.ticket = await escalate(decision, cfg.alerts);
    }

    await applyDecision(decision, { d, repo, file, commit });

    await d.facts.recordDecision(decision);
    const { content, draft, trail, ...slim } = decision;
    slim.stages = trail.map((t) => `${t.node}:${t.status}`);
    results.push(slim);
    console.log(`  ${decision.outcome.padEnd(14)} ${file}  — ${decision.reason}${decision.rootCauseTag ? ` [${decision.rootCauseTag}]` : ''}`);
  }

  fs.writeFileSync('pipeline-results.json', JSON.stringify({ runId, repo, commit, trust: policy.trust, results }, null, 2));
  const count = (o) => results.filter((r) => r.outcome === o).length;
  console.log(`\npublished ${count('published')} | pending_review ${count('pending_review')} | refreshed ${count('refreshed')} | skipped ${count('skipped')} | fallback ${count('fallback')}`);
  await d.facts.close();
}

main().catch((e) => { console.error('fatal:', e); process.exit(1); });
