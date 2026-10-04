'use strict';
/**
 * Deterministic draft verification. No model call: this is the cheap gate in front of the judge (and the reason a weak
 * draft from the cheap tier is caught before it costs a judge call or reaches a human).
 *
 *   1. mentions   the draft names the public symbols the change touched
 *   2. front matter  the page composes valid front matter (title and description) and the draft did not add its own
 *   3. length     not empty, not truncated (unbalanced code fence, cut mid-sentence), not wildly shorter or longer than the existing page
 */
const W = require('./writer');

const SANE = { minChars: 200, maxChars: 120_000, minRatioOfExisting: 0.25, maxRatioOfExisting: 4, mentionRatio: 0.6, maxNamesChecked: 40 };

const mentioned = (text, name) => new RegExp(`(^|[^A-Za-z0-9_$])${name.replace(/[.*+?^${}()|[\]\\\/]/g, '\\$&')}($|[^A-Za-z0-9_$])`).test(text);

/**
 * @param {{draft:string, names?:string[], existing?:string, filePath:string, title?:string, description?:string, thresholds?:object}} a
 * @returns {{ ok: boolean, reasons: string[], metrics: object }}
 */
function verifyDraft({ draft, names = [], existing = '', filePath, title, description, thresholds = {} }) {
  const t = { ...SANE, ...thresholds };
  const reasons = [];
  const metrics = {};
  const text = draft || '';

  // 1. mentions
  const check = [...new Set(names)].sort().slice(0, t.maxNamesChecked);
  if (check.length) {
    const missing = check.filter((n) => !mentioned(text, n));
    metrics.mentioned = `${check.length - missing.length}/${check.length}`;
    if ((check.length - missing.length) / check.length < t.mentionRatio) {
      reasons.push(`The draft does not mention the symbols this change touched. Name each of them: ${missing.slice(0, 12).join(', ')}${missing.length > 12 ? ', ...' : ''}`);
    }
  }

  // 2. front matter
  if (/^\s*---\s*\n/.test(text)) reasons.push('The draft starts with its own front matter. Output only the page body: front matter is added automatically.');
  else {
    const page = W.withFrontmatter(text, { filePath, sourceUrl: 'x', commit: 'x', title, description });
    const fm = (page.match(/^---\n([\s\S]*?)\n---/) || [])[1] || '';
    const ttl = (fm.match(/^title:\s*"(.*)"$/m) || [])[1];
    const desc = (fm.match(/^description:\s*"(.*)"$/m) || [])[1];
    metrics.frontMatter = ttl && desc ? 'ok' : 'incomplete';
    if (!ttl) reasons.push('The page has no usable title.');
    if (!desc) reasons.push('The page has no usable description: start with a paragraph that says what the page is for.');
  }

  // 3. length and completeness
  metrics.chars = text.length;
  if (text.length < t.minChars) reasons.push(`The draft is only ${text.length} characters: far too short to be a page.`);
  if (text.length > t.maxChars) reasons.push(`The draft is ${text.length} characters: far too long. Keep it focused.`);
  if ((text.match(/^```/gm) || []).length % 2 === 1) reasons.push('A code fence is never closed: the draft looks truncated.');
  const last = text.trim().split('\n').pop() || '';
  if (text.length >= t.minChars && last && !/[.!?:)\]`|>*\-\d]$/.test(last.trim()) && !/^(#{1,6}\s|\||[-*]\s|\d+\.\s|```|<)/.test(last.trim())) reasons.push('The draft ends mid-sentence: it looks truncated.');
  if (existing && existing.length > 400) {
    metrics.vsExisting = Number((text.length / existing.length).toFixed(2));
    if (text.length < existing.length * t.minRatioOfExisting) reasons.push(`The draft is ${Math.round((text.length / existing.length) * 100)}% the size of the existing page: content was dropped.`);
    if (text.length > existing.length * t.maxRatioOfExisting) reasons.push(`The draft is ${(text.length / existing.length).toFixed(1)}x the size of the existing page: check for repetition or padding.`);
  }
  if (!/^#{2,3}\s+\S/m.test(text)) reasons.push('The draft has no section headings.');

  return { ok: reasons.length === 0, reasons, metrics };
}

module.exports = { verifyDraft, SANE };
