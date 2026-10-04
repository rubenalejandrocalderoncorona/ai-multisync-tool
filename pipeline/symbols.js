'use strict';
/**
 * Public-interface extraction. A "public symbol" is something another piece of code, another repo or a reader of the
 * documentation can depend on: an exported function or type, an HTTP route, an RPC procedure, a schema model, an
 * environment variable or config key. Internals (a private function, a log message, a variable name) are NOT symbols.
 *
 * The router uses this to decide how much a change is worth spending on; the FactStore uses it to know which documents
 * mention which symbols. Lexical, language-agnostic, deliberately approximate: it errs towards finding too many symbols
 * (the expensive model is the safe side), never too few.
 */

const FILE_HEADER = /^### FILE: (.+)$/gm;

/** Split a code snapshot ("### FILE: path\n...") into { path, text } blocks. A bare string is one anonymous block. */
function splitSnapshot(snapshot) {
  const text = snapshot || '';
  const marks = [...text.matchAll(FILE_HEADER)];
  if (!marks.length) return text ? [{ path: '', text }] : [];
  return marks.map((m, i) => ({ path: m[1].trim(), text: text.slice(m.index + m[0].length, i + 1 < marks.length ? marks[i + 1].index : undefined) }));
}

const norm = (sig) => String(sig || '').replace(/\s+/g, ' ').trim();
const add = (out, kind, name, sig) => { if (name) out.push({ kind, name, sig: norm(sig) }); };
const matches = (text, re) => [...text.matchAll(re)];

/** @returns {{kind:string,name:string,sig:string}[]} */
function extractPublicSymbols(filePath, text) {
  const out = [];
  const ext = (filePath.split('.').pop() || '').toLowerCase();
  const isJs = /^(js|jsx|mjs|cjs|ts|tsx)$/.test(ext) || !filePath;
  const isGo = ext === 'go';
  const isPy = ext === 'py';

  if (isJs) {
    for (const m of matches(text, /^\s*export\s+(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*(\([^)]*\))?/gm)) add(out, 'export', m[1], m[0]);
    for (const m of matches(text, /^\s*export\s+(?:declare\s+)?(?:abstract\s+)?(?:const|let|var|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)([^\n=]*)/gm)) add(out, 'export', m[1], m[0]);
    for (const m of matches(text, /^\s*export\s*\{([^}]*)\}/gm)) for (const n of m[1].split(',')) add(out, 'export', n.trim().split(/\s+as\s+/).pop(), '');
    for (const m of matches(text, /module\.exports\s*=\s*\{([^}]*)\}/g)) for (const n of m[1].split(',')) add(out, 'export', n.trim().split(':')[0], '');
    for (const m of matches(text, /\bexports\.([A-Za-z_$][\w$]*)\s*=/g)) add(out, 'export', m[1], m[0]);
    // HTTP routes (Express/Koa/Fastify style) and Next.js route handlers
    for (const m of matches(text, /\b(?:app|router|server|api)\.(get|post|put|patch|delete|all)\(\s*['"`](\/[^'"`]*)['"`]/g)) add(out, 'route', `${m[1].toUpperCase()} ${m[2]}`, m[0]);
    if (/(^|\/)route\.(ts|js)$/.test(filePath)) for (const m of matches(text, /export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE)\b/g)) add(out, 'route', `${m[1]} ${filePath.replace(/.*\/app/, '').replace(/\/route\.(ts|js)$/, '') || '/'}`, m[0]);
    // tRPC / RPC procedures
    for (const m of matches(text, /^\s*([A-Za-z_$][\w$]*)\s*:\s*(?:[A-Za-z_$][\w$]*\.)*(?:publicProcedure|protectedProcedure|privateProcedure|authedProcedure|procedure|adminProcedure)\b/gm)) add(out, 'rpc', m[1], m[0]);
  }
  if (isGo) {
    for (const m of matches(text, /^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)\s*(\([^)]*\))?/gm)) add(out, 'export', m[1], m[0]);
    for (const m of matches(text, /^type\s+([A-Z]\w*)\s+(struct|interface)/gm)) add(out, 'export', m[1], m[0]);
  }
  if (isPy) {
    for (const m of matches(text, /^(?:async\s+)?def\s+([A-Za-z]\w*)\s*(\([^)]*\))?/gm)) add(out, 'export', m[1], m[0]);
    for (const m of matches(text, /^class\s+([A-Za-z]\w*)/gm)) add(out, 'export', m[1], m[0]);
    for (const m of matches(text, /@(?:app|router|bp|blueprint)\.(?:route|get|post|put|patch|delete)\(\s*['"](\/[^'"]*)['"]/g)) add(out, 'route', m[1], m[0]);
  }
  // Schemas and contracts, any extension
  for (const m of matches(text, /^model\s+([A-Za-z_]\w*)\s*\{/gm)) add(out, 'model', m[1], blockOf(text, m.index));
  for (const m of matches(text, /^enum\s+([A-Za-z_]\w*)\s*\{/gm)) add(out, 'model', m[1], blockOf(text, m.index));
  for (const m of matches(text, /create\s+table\s+(?:if\s+not\s+exists\s+)?["`]?([A-Za-z_]\w*)["`]?/gi)) add(out, 'model', m[1], '');
  for (const m of matches(text, /^(?:message|service|rpc)\s+([A-Za-z_]\w*)/gm)) add(out, 'model', m[1], blockOf(text, m.index));
  if (/openapi|swagger/i.test(filePath)) for (const m of matches(text, /^\s{2}(\/[\w/{}.-]+):/gm)) add(out, 'route', m[1], '');
  // Environment variables and config keys are a contract with operators
  for (const m of matches(text, /\b(?:process\.env|import\.meta\.env)\.([A-Z][A-Z0-9_]{2,})/g)) add(out, 'config', m[1], '');
  for (const m of matches(text, /\b(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\(\s*|getenv\(\s*|os\.Getenv\(\s*)["']([A-Z][A-Z0-9_]{2,})["']/g)) add(out, 'config', m[1], '');
  return out;
}

/** The body of a block starting at `index`, up to its closing brace (bounded): schema models change when a field does. */
function blockOf(text, index) {
  const open = text.indexOf('{', index);
  if (open < 0) return text.slice(index, index + 120);
  let depth = 0;
  for (let i = open; i < Math.min(text.length, open + 6000); i++) {
    if (text[i] === '{') depth++;
    else if (text[i] === '}' && --depth === 0) return text.slice(index, i + 1);
  }
  return text.slice(index, open + 400);
}

/** All public symbols of a snapshot, keyed `kind:name`, with a signature string for change detection. */
function publicSymbols(snapshot) {
  const map = new Map();
  for (const f of splitSnapshot(snapshot)) {
    for (const s of extractPublicSymbols(f.path, f.text)) {
      const key = `${s.kind}:${s.name}`;
      if (!map.has(key)) map.set(key, { ...s, key, paths: [f.path], sig: s.sig });
      else { const e = map.get(key); if (!e.paths.includes(f.path)) e.paths.push(f.path); e.sig = norm(`${e.sig} ${s.sig}`); }
    }
  }
  return map;
}

/**
 * What changed in the public interface between two snapshots.
 * @returns {{ added: string[], removed: string[], changed: string[], names: string[], touched: boolean }}
 */
function diffPublicSymbols(before, after) {
  const a = publicSymbols(before);
  const b = publicSymbols(after);
  const added = [...b.keys()].filter((k) => !a.has(k));
  const removed = [...a.keys()].filter((k) => !b.has(k));
  const changed = [...b.keys()].filter((k) => a.has(k) && a.get(k).sig !== b.get(k).sig);
  const bare = (k) => b.get(k)?.name ?? a.get(k)?.name;
  const names = [...new Set([...added, ...removed, ...changed].map(bare))].filter(Boolean);
  return { added, removed, changed, names, touched: added.length + removed.length + changed.length > 0, total: b.size };
}

/** Identifier-like tokens of a document: the candidates a doc may be referencing. */
function docTokens(text) {
  return new Set(String(text || '').match(/[A-Za-z_$][\w$]{2,}/g) || []);
}

module.exports = { extractPublicSymbols, publicSymbols, diffPublicSymbols, splitSnapshot, docTokens };
