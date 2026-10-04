'use strict';
/**
 * Deterministic completeness check, no LLM. For documentation types where completeness IS the point
 * (a schema reference, a configuration reference), the names declared in the code are extracted and the draft must mention
 * each of them. The LLM judge cannot do this reliably: it lists the facts it notices, so "recall 1.0" only means
 * "complete against the facts it listed".
 *
 * A style opts in:  "coverage": { "kinds": ["prisma_model", "prisma_enum"], "min": 0.9 }
 */

// Each extractor returns the declared names found in a code snapshot.
const EXTRACTORS = {
  prisma_model: (t) => [...t.matchAll(/^model\s+([A-Za-z_]\w*)\s*\{/gm)].map((m) => m[1]),
  prisma_enum: (t) => [...t.matchAll(/^enum\s+([A-Za-z_]\w*)\s*\{/gm)].map((m) => m[1]),
  sql_table: (t) => [...t.matchAll(/create\s+table\s+(?:if\s+not\s+exists\s+)?["`]?([A-Za-z_]\w*)["`]?/gi)].map((m) => m[1]),
  env_var: (t) => [...t.matchAll(/\b(?:process\.env|import\.meta\.env|os\.environ(?:\.get)?\(?)[.["']*([A-Z][A-Z0-9_]{2,})/g)].map((m) => m[1]),
  http_route: (t) => [...t.matchAll(/\b(?:app|router)\.(?:get|post|put|patch|delete)\(\s*["'`](\/[^"'`]*)["'`]/g)].map((m) => m[1]),
  graphql_type: (t) => [...t.matchAll(/^(?:type|input|enum|interface)\s+([A-Za-z_]\w*)/gm)].map((m) => m[1]),
};

const wholeWord = (text, name) => new RegExp(`(^|[^A-Za-z0-9_])${name.replace(/[.*+?^${}()|[\]\\\/]/g, '\\$&')}($|[^A-Za-z0-9_])`).test(text);

/**
 * @param {string} draft   the page text
 * @param {string} code    the code snapshot the page is based on
 * @param {{kinds:string[], min?:number}|undefined} spec  the style's coverage rule
 * @returns {null | { ok: boolean, kinds: object, missing: string[], ratio: number }}
 */
function checkCoverage(draft, code, spec) {
  if (!spec?.kinds?.length) return null;
  const min = spec.min ?? 0.9;
  const kinds = {};
  const missing = [];
  const seen = new Set(); // a name declared once counts once, even if two extractors match it (prisma enum vs graphql enum)
  let found = 0;
  let total = 0;
  for (const kind of spec.kinds) {
    const fn = EXTRACTORS[kind];
    if (!fn) continue;
    const names = [...new Set(fn(code))].filter((n) => !seen.has(n));
    names.forEach((n) => seen.add(n));
    const miss = names.filter((n) => !wholeWord(draft, n));
    kinds[kind] = { total: names.length, found: names.length - miss.length };
    found += names.length - miss.length;
    total += names.length;
    missing.push(...miss.map((n) => `${kind}:${n}`));
  }
  if (!total) return null; // nothing declared in scope: nothing to enforce
  const ratio = found / total;
  return { ok: ratio >= min, kinds, missing, ratio, min };
}

module.exports = { checkCoverage, EXTRACTORS };
