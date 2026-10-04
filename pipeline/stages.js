'use strict';
/** The two LLM context stages that run before writing, code mode only. */
const P = require('./prompts');

const str = (v) => (typeof v === 'string' ? v : '');

/** Retry once on malformed JSON: a stage must not silently continue with nothing. */
async function jsonStage(llm, messages) {
  try { return await llm.chatJson(messages); } catch (first) {
    try { return await llm.chatJson([...messages, { role: 'user', content: 'Your previous reply was not valid JSON. Reply again with ONLY the JSON object.' }]); }
    catch { throw new Error(`stage returned invalid JSON twice: ${first.message}`); }
  }
}

/** STAGE 1: code context -> fact sheet. */
async function analyzeCode(llm, { page, styleKey, changedFiles, repoMap, code, related }) {
  const prompt = P.loadPrompt('analyze-code');
  const relatedText = related.map((c) => c.text).join('\n\n') || '(none retrieved)';
  const r = await jsonStage(llm, [
    { role: 'system', content: prompt.text },
    { role: 'user', content: `PAGE: ${page} (style: ${styleKey || 'unspecified'})\n\nCHANGED_FILES: ${changedFiles.join(', ') || '(none)'}\n\nREPO_MAP:\n${repoMap.join('\n') || '(unknown)'}\n\nCODE:\n${code}\n\nRELATED_CODE:\n${relatedText}` },
  ]);
  const facts = (Array.isArray(r.facts) ? r.facts : [])
    .filter((f) => f && str(f.text) && str(f.evidence))   // a fact without evidence is not allowed
    .map((f, i) => ({ id: str(f.id) || `F${i + 1}`, text: f.text, evidence: f.evidence, kind: str(f.kind) || 'other', status: str(f.status) || 'unchanged' }));
  return { sheet: { summary: str(r.summary), facts, unclear: Array.isArray(r.unclear) ? r.unclear.filter(Boolean) : [] }, promptId: prompt.id, dropped: (Array.isArray(r.facts) ? r.facts.length : 0) - facts.length };
}

/** STAGE 2: semantic context -> documentation plan. */
async function planDocs(llm, { sheet, brief, styleText, existing, related, template }) {
  const prompt = P.loadPrompt('plan-docs');
  const relatedDocs = related.map((c) => `[${c.kind}] ${c.heading || c.path}: ${c.text.slice(0, 700)}`).join('\n---\n') || '(none retrieved)';
  const r = await jsonStage(llm, [
    { role: 'system', content: prompt.text },
    { role: 'user', content: `FACT_SHEET:\n${JSON.stringify(sheet, null, 1)}\n\nPAGE_BRIEF:\n${brief || '(none)'}\n\nSTYLE:\n${styleText || '(none)'}\n\nEXISTING_PAGE:\n${existing || '(none)'}\n\nRELATED_DOCS:\n${relatedDocs}\n\nTEMPLATE:\n${template || '(none)'}` },
  ]);
  const ids = new Set(sheet.facts.map((f) => f.id));
  const sections = (Array.isArray(r.sections) ? r.sections : []).map((s) => ({
    heading: str(s.heading), action: str(s.action) || 'add', notes: str(s.notes),
    must_cover: (Array.isArray(s.must_cover) ? s.must_cover : []).filter((id) => ids.has(id)), // drop invented ids
  })).filter((s) => s.heading);
  return {
    plan: {
      audience: str(r.audience), purpose: str(r.purpose), sections,
      terminology: Array.isArray(r.terminology) ? r.terminology : [], out_of_scope: Array.isArray(r.out_of_scope) ? r.out_of_scope : [],
      gaps: Array.isArray(r.gaps) ? r.gaps.filter(Boolean) : [],
    },
    promptId: prompt.id,
  };
}

const sheetText = (sheet) => (sheet ? JSON.stringify(sheet, null, 1) : '');
const planText = (plan) => (plan ? JSON.stringify(plan, null, 1) : '');

module.exports = { analyzeCode, planDocs, sheetText, planText };
