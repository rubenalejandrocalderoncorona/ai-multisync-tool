'use strict';
const { loadConfig } = require('../pipeline/config');
const { MemoryVectorStore } = require('../pipeline/vectorstore');
const { MemoryFactStore } = require('../pipeline/factstore');

/** Deterministic bag-of-words embedding: similar text -> high cosine, no network. */
function embedText(text, dim = 64) {
  const v = new Array(dim).fill(0);
  for (const w of text.toLowerCase().match(/[a-z0-9_]+/g) || []) {
    let h = 0;
    for (const ch of w) h = (h * 31 + ch.charCodeAt(0)) % dim;
    v[h] += 1;
  }
  return v;
}

/**
 * Scripted LLM. `judges` is a queue of judge results; the last one repeats.
 * Records every call so tests can assert on cost (e.g. "no LLM call was made").
 */
const GOOD_DRAFT = '## Overview\n\nThe alert service exposes an alert API on port 8080 and supports three alert channels. Alert and Silence are the public operations, and the Port setting controls where the service listens.\n\n## Configuration\n\nSet `ALERT_PORT` to change the port. Use Silence with an alert id to mute a single alert.\n';

function fakeLLM({ judges = [], draft = GOOD_DRAFT } = {}) {
  const calls = { chat: 0, judge: 0, embed: 0, analyze: 0, plan: 0, drafts: [], analyzeInputs: [], planInputs: [], log: [] };
  const tierOf = (o = {}) => (o.tier === 'cheap' || o.fast ? 'cheap' : 'expensive');
  let j = 0;
  return {
    calls,
    async embed(texts) { calls.embed++; return texts.map((t) => embedText(t)); },
    async chat(messages, opts) {
      calls.chat++;
      const sys = messages[0].content;
      const kind = sys.includes('ONE short paragraph') ? 'gar' : sys.includes('single best template') ? 'template' : sys.includes('Classify the document') ? 'folder' : sys.includes('improve spelling') ? 'polish' : 'draft';
      calls.log.push({ kind, tier: tierOf(opts) });
      if (sys.includes('ONE short paragraph')) return 'The service exposes an alert API on port 8080.';
      if (sys.includes('single best template')) return 'DEFAULT';
      if (sys.includes('Classify the document')) return 'features';
      if (sys.includes('improve spelling')) return `${messages[1].content}\n`;
      calls.drafts.push(messages[1].content);
      return typeof draft === 'function' ? draft(messages, opts, calls) : draft;
    },
    async chatJson(messages, opts) {
      const sys = messages?.[0]?.content || '';
      calls.log.push({ kind: sys.includes('HYPOTHETICAL documentation') ? 'gar-facts' : sys.includes('code analyst') ? 'analyze' : sys.includes('documentation planner') ? 'plan' : 'judge', tier: tierOf(opts) });
      if (sys.includes('HYPOTHETICAL documentation')) { calls.garFacts = (calls.garFacts || 0) + 1; return GAR_FACTS; }
      if (sys.includes('You are a code analyst')) { calls.analyze++; calls.analyzeInputs.push(messages[1].content); return CODE_FACTS; }
      if (sys.includes('You are a documentation planner')) { calls.plan++; calls.planInputs.push(messages[1].content); return PLAN; }
      calls.judge++;
      const r = judges[Math.min(j, judges.length - 1)];
      j++;
      return r;
    },
  };
}

const CODE_FACTS = {
  summary: 'Alert service; the commit adds silencing and a configurable port.',
  facts: [
    { id: 'F1', text: 'The service listens on port 8081', evidence: 'src/a.go:5', kind: 'config', status: 'changed' },
    { id: 'F2', text: 'Alerts can be silenced by id', evidence: 'src/a.go:3', kind: 'behavior', status: 'added' },
    { id: 'F3', text: 'no evidence given', evidence: '', kind: 'other', status: 'added' },
  ],
  unclear: ['How silences expire'],
};
const GAR_FACTS = { paragraphs: ['The service listens on a configurable port and is configured through environment variables.', 'Alerts can be silenced by id through the silence endpoint.', ''] };
const PLAN = {
  audience: 'Engineers integrating with the alert service', purpose: 'Explain delivery and configuration',
  sections: [{ heading: 'Overview', action: 'update', must_cover: ['F1', 'F2', 'F99'], notes: '' }, { heading: 'Configuration', action: 'add', must_cover: ['F1'], notes: '' }],
  terminology: [{ term: 'silence', use: 'silence' }], out_of_scope: ['billing'], gaps: ['How silences expire'],
};
const passJudge = { claims: [{ text: 'port is 8080', supported: true }], facts: [{ text: 'port 8080', covered: true }], style: 0.9, quality: 0.9, notes: [] };
const hallucinationJudge = { claims: [{ text: 'supports gRPC', supported: false }, { text: 'port is 8080', supported: true }], facts: [{ text: 'port 8080', covered: true }], style: 0.9, quality: 0.9, notes: [] };

function makeDeps(overrides = {}) {
  const cfg = loadConfig({ INTERNAL_AI_API_KEY: 'test', MIN_DIFF_LINES: '3', MAX_ITERATIONS: '2', CONTEXT_MIN_SCORE: '0', ...overrides.env });
  return {
    cfg,
    llm: overrides.llm || fakeLLM({ judges: [passJudge] }),
    vectors: overrides.vectors || new MemoryVectorStore(),
    codeVectors: overrides.codeVectors || new MemoryVectorStore(),
    facts: overrides.facts || new MemoryFactStore(),
    registry: overrides.registry || {},
    policy: { trust: 'review', serviceName: 'svc', styleGuide: '', glossary: {}, ...overrides.policy },
    instructions: '',
    templateFiles: [],
    defaultTemplate: 'docs/templates/default-template/default-template.md',
    runId: 'test-run',
  };
}

const DOC_V1 = '## Overview\n\nAlert API.\n\n- email\n- slack\n';
const DOC_V2 = '## Overview\n\nAlert API on port 8080.\n\n- email\n- slack\n- pagerduty\n\n## Configuration\n\nSet `ALERT_PORT`.\n';

module.exports = { GOOD_DRAFT, GAR_FACTS, CODE_FACTS, PLAN, fakeLLM, makeDeps, passJudge, hallucinationJudge, embedText, DOC_V1, DOC_V2 };
