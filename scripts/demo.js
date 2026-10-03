#!/usr/bin/env node
'use strict';
/**
 * End-to-end demo of the agentic workflow. Five scenarios run through the real LangGraph pipeline:
 *
 *   1 first-publish     new doc               -> prefilter > cross_repo > similarity > gar > write_draft > judge > publish
 *   2 near-duplicate    one number changed    -> prefilter > cross_repo > similarity  (cosine short-circuit, NO LLM)
 *   3 structural-change new section + items   -> full path again (shape change overrides similarity)
 *   4 cross-repo-block  registered feature    -> fallback + cAImanDesk ticket, zero LLM spend
 *   5 judge-fallback    impossible precision  -> judge loop > widen > fallback + cAImanDesk ticket
 *
 * Live (real LLM, Qdrant, Postgres, cAImanDesk):   node scripts/demo.js
 * Offline rehearsal (scripted LLM, in-memory):     node scripts/demo.js --offline
 *
 * Live needs the env vars from .env.example. Tickets need CAIMANDESK_API_TOKEN + CAIMANDESK_PROJECT_ID.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { processChange } = require('../pipeline/pipeline');
const { escalate } = require('../pipeline/fallback');
const { loadConfig } = require('../pipeline/config');
const { MemoryVectorStore } = require('../pipeline/vectorstore');
const { MemoryFactStore } = require('../pipeline/factstore');
const W = require('../pipeline/writer');
const { buildDeps, applyDecision } = require('./lib');

const OFFLINE = process.argv.includes('--offline');
const ROOT = path.resolve(__dirname, '..');
const REPO = 'demo/alerts-service';

const V1 = `# Alerts service

The alerts service delivers notifications when a monitored metric crosses a threshold.

## Delivery channels

- email
- slack

## Configuration

The service listens on port 8080. Set \`ALERT_PORT\` to change it.
`;
const V2 = V1.replace('port 8080', 'port 8081');
const V3 = `${V2}
- pagerduty

## Escalation

An unacknowledged alert escalates to the on-call engineer after 10 minutes.
Set \`ESCALATION_MINUTES\` to change the delay.
`;
const V4 = `${V3}
## Channel registry

Channels are declared in \`alert_channels_enum\`.
The frontend renders every declared channel in its settings page.
New channels appear in the UI without a frontend release.
`;

const SCENARIOS = [
  { name: 'first-publish', before: null, after: V1, expect: 'published' },
  { name: 'near-duplicate', before: V1, after: V2, expect: 'refreshed', env: { MIN_DIFF_LINES: '2' } },
  { name: 'structural-change', before: V2, after: V3, expect: 'published' },
  { name: 'cross-repo-block', before: V3, after: V4, expect: 'fallback' },
  { name: 'judge-fallback', before: V1, after: V3.replace('10 minutes', '15 minutes'), expect: 'fallback', env: { PRECISION_MIN: '1.01', MAX_ITERATIONS: '2' } },
];

const c = { dim: (s) => `\x1b[2m${s}\x1b[0m`, b: (s) => `\x1b[1m${s}\x1b[0m`, g: (s) => `\x1b[32m${s}\x1b[0m`, r: (s) => `\x1b[31m${s}\x1b[0m`, y: (s) => `\x1b[33m${s}\x1b[0m` };

function offlineDeps() {
  const { fakeLLM, passJudge, hallucinationJudge } = require('../test/helpers');
  return (scenario) => {
    const llm = fakeLLM({ judges: [scenario.name === 'judge-fallback' ? hallucinationJudge : passJudge] });
    // Like a faithful writer: the draft is the source text, so re-indexed chunks resemble later edits.
    const chat = llm.chat.bind(llm);
    llm.chat = async (messages, o) => {
      const user = messages[1]?.content || '';
      const m = messages[0].content.includes('markdown body of the page') && user.match(/SOURCE \([^)]*\):\n([\s\S]*?)(?:\n\nA reviewer rejected|$)/);
      return m ? m[1] : chat(messages, o);
    };
    return { llm };
  };
}

async function main() {
  const out = path.join(ROOT, 'demo-output');
  fs.rmSync(out, { recursive: true, force: true });
  fs.mkdirSync(out, { recursive: true });
  const instructions = fs.readFileSync(path.join(ROOT, '.github/instructions/DocumentationInstructions.instructions.md'), 'utf-8');
  const templates = path.join(ROOT, 'docs/templates');
  const base = OFFLINE
    ? { vectors: new MemoryVectorStore(), facts: new MemoryFactStore() }
    : buildDeps();
  if (!OFFLINE) {
    await base.facts.migrate();
    await base.vectors.ensureCollection();
    // Start every demo from a clean slate for this demo repo only.
    for (const f of ['docs/alerts.md']) await base.vectors.deleteByPath(REPO, f);
  }
  process.chdir(out);

  const registry = { alert_channels_enum: { owner: REPO, requires: ['demo/frontend'] } };
  const policy = { trust: 'auto', serviceName: 'alerts-service', styleGuide: 'Concise reference style.', glossary: {} };
  const runId = `demo-${crypto.randomBytes(3).toString('hex')}`;
  const commits = ['a1b2c3d', 'b2c3d4e', 'c3d4e5f', 'd4e5f6a', 'e5f6a7b'].map((x) => x.padEnd(40, '0'));
  const summary = [];

  console.log(c.b(`\nai-multisync-tool demo  ${OFFLINE ? c.y('[offline: scripted LLM, in-memory stores]') : c.g('[live]')}  run ${runId}\n`));

  for (const [i, sc] of SCENARIOS.entries()) {
    const cfg = loadConfig({ ...process.env, INTERNAL_AI_API_KEY: process.env.INTERNAL_AI_API_KEY || 'offline', SIMILARITY_HIGH: process.env.SIMILARITY_HIGH || '0.85', ...sc.env });
    const llm = OFFLINE ? offlineDeps()(sc).llm : base.llm;
    const deps = {
      cfg, llm, vectors: base.vectors, facts: base.facts, registry, policy, instructions,
      templateFiles: W.findTemplateFiles(templates), defaultTemplate: path.join(templates, 'default-template', 'default-template.md'),
      runId, githubHost: 'github.com',
      logger: {
        async log(e) {
          const tone = e.status === 'stop' || e.status === 'fallback' ? c.y : e.status === 'error' ? c.r : c.g;
          console.log(`   ${tone(e.status.padEnd(8))} ${e.node.padEnd(12)} ${c.dim(`${String(e.ms).padStart(5)}ms  ${JSON.stringify(e.note)}`)}`);
          try { await base.facts.recordNodeLog(e); } catch { /* demo continues without a persisted log */ }
        },
      },
      escalate: async (d) => {
        if (OFFLINE && !cfg.alerts.deskToken) return '(offline) cAImanDesk ticket would be created here';
        return escalate(d, cfg.alerts);
      },
    };
    console.log(c.b(`${i + 1}. ${sc.name}`) + c.dim(`   expect: ${sc.expect}`));
    const change = { repo: REPO, filePath: 'docs/alerts.md', before: sc.before, after: sc.after, commit: commits[i] };
    const decision = await processChange(change, deps);
    await applyDecision(decision, { d: { ...base, llm }, repo: REPO, file: change.filePath, commit: change.commit });
    await base.facts.recordDecision(decision);

    const ok = decision.outcome === sc.expect;
    console.log(`   ${ok ? c.g('✔') : c.r('✘')} ${c.b(decision.outcome)}  ${decision.reason}`);
    if (decision.rootCauseTag) console.log(`   ${c.dim('root cause:')} ${decision.rootCauseTag}   ${c.dim('reviewer action:')} ${decision.reviewerAction}`);
    if (decision.ticket) console.log(`   ${c.dim('ticket:')} ${decision.ticket}`);
    console.log();
    summary.push({ scenario: sc.name, expected: sc.expect, outcome: decision.outcome, ok, tag: decision.rootCauseTag || null, ticket: decision.ticket || null, stages: decision.trail.map((t) => t.node), ms: decision.trail.reduce((a, t) => a + t.ms, 0) });
  }

  fs.writeFileSync(path.join(out, 'demo-run.json'), JSON.stringify({ runId, offline: OFFLINE, summary }, null, 2));
  const passed = summary.filter((s) => s.ok).length;
  console.log(c.b(`${passed}/${summary.length} scenarios behaved as expected.`));
  console.log(c.dim(`Published page: demo-output/src/content/docs/services/alerts-service/   Decision log: demo-output/demo-run.json`));
  await base.facts.close?.();
  process.exit(passed === summary.length ? 0 : 1);
}

main().catch((e) => { console.error(c.r(`demo failed: ${e.stack || e.message}`)); process.exit(1); });
