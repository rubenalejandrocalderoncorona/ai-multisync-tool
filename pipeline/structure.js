'use strict';
/**
 * Zero-LLM structural analysis. No tokens are spent here.
 *
 * Two outputs matter to the pipeline:
 *   - `forced`  : the *shape* changed (a heading, code block, table row, list item,
 *                 function or array element was added/removed). Shape changes override
 *                 embedding similarity, because a one-item addition barely moves a vector.
 *   - `changed` : shape or fact-bearing tokens (numbers, URLs, inline code, env vars) changed.
 *                 Pure prose rewording with identical facts is not worth generation cost.
 *
 * This is a lightweight lexical signature, not a full parser; it is deliberately
 * language-agnostic so one implementation covers Markdown and common source files.
 */

const CODE_EXT = /\.(js|jsx|ts|tsx|mjs|cjs|py|go|java|rb|rs|yaml|yml|json)$/i;

function signature(content) {
  const text = content || '';
  const lines = text.split('\n');

  const headings = lines.filter((l) => /^#{1,6}\s+\S/.test(l)).map((l) => l.trim());
  const fences = (text.match(/^```/gm) || []).length / 2;
  const tableRows = lines.filter((l) => /^\s*\|.*\|\s*$/.test(l) && !/^\s*\|[\s:|-]+\|\s*$/.test(l)).length;
  const listItems = lines.filter((l) => /^\s*([-*+]|\d+\.)\s+\S/.test(l)).length;

  const functions = new Set();
  for (const re of [
    /\bfunction\s+([A-Za-z_$][\w$]*)/g,
    /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?[^=]*=>/g,
    /^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)/gm,
    /^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)/gm,
  ]) {
    for (const m of text.matchAll(re)) functions.add(m[1]);
  }

  // Array/list literal item counts keyed by the variable that holds them.
  const arrays = {};
  for (const m of text.matchAll(/\b([A-Za-z_$][\w$]*)\s*=\s*\[([^\]]*)\]/g)) {
    arrays[m[1]] = m[2].split(',').filter((s) => s.trim()).length;
  }

  const tokens = new Set([
    ...(text.match(/`[^`\n]+`/g) || []),
    ...(text.match(/https?:\/\/[^\s)>"']+/g) || []),
    ...(text.match(/\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/g) || []),
    ...(text.match(/\b\d+(?:\.\d+)*\b/g) || []),
  ]);

  return {
    headings,
    fences,
    tableRows,
    listItems,
    functions: [...functions].sort(),
    arrays,
    tokens: [...tokens].sort(),
  };
}

const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function structuralChange(before, after) {
  const a = signature(before);
  const b = signature(after);
  const shape = [];
  const facts = [];

  if (!eq(a.headings, b.headings)) shape.push('headings');
  if (a.fences !== b.fences) shape.push('code_blocks');
  if (a.tableRows !== b.tableRows) shape.push('table_rows');
  if (a.listItems !== b.listItems) shape.push('list_items');
  if (!eq(a.functions, b.functions)) shape.push('functions');
  if (!eq(a.arrays, b.arrays)) shape.push('array_literals');
  if (!eq(a.tokens, b.tokens)) facts.push('fact_tokens');

  return {
    changed: shape.length > 0 || facts.length > 0,
    forced: shape.length > 0,
    reasons: [...shape, ...facts],
    symbols: { added: b.functions.filter((f) => !a.functions.includes(f)), tokens: b.tokens },
  };
}

/** Added + removed non-blank lines, order-insensitive (multiset diff). */
function diffLineCount(before, after) {
  const bag = new Map();
  for (const l of (before || '').split('\n').map((s) => s.trim()).filter(Boolean)) {
    bag.set(l, (bag.get(l) || 0) + 1);
  }
  let added = 0;
  for (const l of (after || '').split('\n').map((s) => s.trim()).filter(Boolean)) {
    const n = bag.get(l) || 0;
    if (n > 0) bag.set(l, n - 1);
    else added++;
  }
  let removed = 0;
  for (const n of bag.values()) removed += n;
  return { added, removed, total: added + removed };
}

module.exports = { signature, structuralChange, diffLineCount, CODE_EXT };
