'use strict';
/**
 * Prompt and style loading. Prompts are files (prompts/*.md) so they can be reviewed and edited
 * without code changes; the style library (config/doc-styles.json) is a key-value store of
 * writer personas + judge rubrics keyed by documentation type.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const DEFAULT_DIR = path.join(__dirname, '..', 'prompts');
const DEFAULT_STYLES = path.join(__dirname, '..', 'config', 'doc-styles.json');
const cache = new Map();

/** @returns {{ text: string, id: string }} id = `<name>@<sha8>` for the run logs */
function loadPrompt(name, dir = process.env.PROMPTS_DIR || DEFAULT_DIR) {
  let file = path.join(dir, `${name}.md`);
  if (!fs.existsSync(file)) file = path.join(DEFAULT_DIR, `${name}.md`); // partial override dirs are fine
  if (!cache.has(file)) {
    const text = fs.readFileSync(file, 'utf-8').trim();
    cache.set(file, { text, id: `${name}@${crypto.createHash('sha256').update(text).digest('hex').slice(0, 8)}` });
  }
  return cache.get(file);
}

const fill = (text, vars) => text.replace(/\{\{(\w+)\}\}/g, (_, k) => (vars[k] === undefined ? '' : vars[k]));

/** Custom library first (DOC_STYLES), then the bundled one, then empty. */
function loadStyles(file = process.env.DOC_STYLES) {
  for (const f of [file, DEFAULT_STYLES]) {
    if (!f) continue;
    try { return JSON.parse(fs.readFileSync(f, 'utf-8')); } catch { /* try next */ }
  }
  return { defaultStyle: null, styles: {} };
}

/**
 * Resolve a style key to { key, prompt, rubric }. Unknown or missing keys fall back to
 * `defaultStyle`, then to an empty style, and say so via `fallback` so the logs show it.
 */
function resolveStyle(library, key) {
  const styles = library.styles || {};
  if (key && styles[key]) return { key, ...styles[key], fallback: false };
  const dk = library.defaultStyle;
  if (dk && styles[dk]) return { key: dk, ...styles[dk], fallback: !!key };
  return { key: key || null, prompt: '', rubric: [], fallback: !!key };
}

/** The STYLE block handed to the judge and the polish pass. */
function styleText(style, policy = {}) {
  const glossary = Object.entries(policy.glossary || {}).map(([k, v]) => `- ${k}: ${v}`).join('\n');
  return [
    style.key && `Style: ${style.key}`,
    style.rubric?.length && `Rubric:\n${style.rubric.map((r) => `- ${r}`).join('\n')}`,
    policy.styleGuide && `Repo style guide:\n${policy.styleGuide}`,
    glossary && `Glossary (use these terms):\n${glossary}`,
  ].filter(Boolean).join('\n\n');
}

/** The section skeleton for a style, as text for the writer and planner. Empty when the style has none. */
function outlineText(style) {
  return style?.outline?.length ? `OUTLINE (follow this order; delete any section you have no facts for):\n${style.outline.map((o) => `## ${o}`).join('\n')}` : '';
}

module.exports = { loadPrompt, fill, loadStyles, resolveStyle, styleText, outlineText };
