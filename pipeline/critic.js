'use strict';
/**
 * The critic is the *role*; LLM-as-judge is the *technique* it uses for the expensive check.
 *
 * One judge call returns per-claim verdicts plus per-fact coverage plus quality/style scores.
 * Metrics are then computed in code so thresholds stay deterministic and auditable:
 *   precision = supported claims / claims in draft        (hallucination guard)
 *   recall    = source facts covered / source facts       (completeness guard)
 */

const P = require('./prompts');

/**
 * @param {object} a
 * @param {'docs'|'code'} [a.mode]  docs: source is a doc file. code: source is a code snapshot.
 */
async function judge(llm, { mode = 'docs', source, draft, knownFacts = [], styleText = '', existing = '', changedFiles = [] }) {
  const prompt = P.loadPrompt(mode === 'code' ? 'judge-code' : 'judge-docs');
  const user = mode === 'code'
    ? `CODE:\n${source}\n\nCHANGED_FILES: ${changedFiles.join(', ') || '(unknown)'}\n\nEXISTING_PAGE:\n${existing || '(none)'}\n\nDRAFT:\n${draft}\n\nKNOWN_FACTS:\n${knownFacts.join('\n') || '(none)'}\n\nSTYLE:\n${styleText || '(none)'}`
    : `SOURCE:\n${source}\n\nDRAFT:\n${draft}\n\nKNOWN_FACTS:\n${knownFacts.join('\n') || '(none)'}\n\nSTYLE:\n${styleText || '(none)'}`;
  const r = await llm.chatJson([{ role: 'system', content: prompt.text }, { role: 'user', content: user }]);
  const claims = Array.isArray(r.claims) ? r.claims : [];
  const facts = Array.isArray(r.facts) ? r.facts : [];
  const supported = claims.filter((c) => c.supported).length;
  const covered = facts.filter((f) => f.covered).length;
  return {
    claims,
    precision: claims.length ? supported / claims.length : 1,
    recall: facts.length ? covered / facts.length : 1,
    style: clamp01(r.style),
    quality: clamp01(r.quality),
    unsupported: claims.filter((c) => !c.supported).map((c) => c.text),
    missing: facts.filter((f) => !f.covered).map((f) => f.text),
    notes: Array.isArray(r.notes) ? r.notes.filter(Boolean) : [],
    promptId: prompt.id,
  };
}

const clamp01 = (n) => Math.max(0, Math.min(1, Number(n) || 0));

/**
 * Map metrics + thresholds to the first failing check. Order = severity:
 * hallucination first, because shipping a false claim is the worst outcome.
 * @returns {null | {tag:string, feedback:string[]}}
 */
function evaluate(m, t) {
  if (m.precision < t.precisionMin) {
    return { tag: 'hallucinated_claim', feedback: m.unsupported.map((c) => `Unsupported claim, remove or correct: "${c}"`) };
  }
  if (m.recall < t.recallMin) {
    return { tag: 'missing_claim', feedback: m.missing.map((f) => `Missing fact from source: "${f}"`) };
  }
  if (m.style < t.styleMin) {
    return { tag: 'style_mismatch', feedback: [`Style score ${m.style.toFixed(2)} < ${t.styleMin}`, ...m.notes] };
  }
  if (m.quality < t.judgeMin) {
    return { tag: 'judge_low_confidence', feedback: [`Quality score ${m.quality.toFixed(2)} < ${t.judgeMin}`, ...m.notes] };
  }
  return null;
}

module.exports = { judge, evaluate };
