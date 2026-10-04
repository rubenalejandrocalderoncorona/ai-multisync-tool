#!/usr/bin/env node
'use strict';
/**
 * Dry run for onboarding a repository. NO network calls, nothing is written: it shows what the repo's entry in
 * config/repos.json would load into the vector database and what each page would be based on, so a wrong scope,
 * a sensitive file or a surprising cost is caught BEFORE anything is embedded or sent to a model.
 *
 *   node scripts/onboard_check.js --repo owner/name --dir /path/to/checkout [--ref <sha>]
 *
 * Exit code 1 when there are blocking problems (no config entry, a page that matches no files, sensitive files in scope).
 */
const { repoPolicy, readJson, loadConfig } = require('../pipeline/config');
const { selectFiles, snapshot, scrub, globToRegExp, pagesScope } = require('../pipeline/codesource');
const { chunkCode, chunkMarkdown } = require('../pipeline/chunker');
const { DOC_FILE } = require('../pipeline/context');
const { loadStyles, resolveStyle } = require('../pipeline/prompts');
const { checkCoverage } = require('../pipeline/coverage');

// Classification markers in document-type files (notes, exports, fixtures). Ordinary code that uses the word "restricted" is not flagged.
const SENSITIVE_CONTENT = /(classification\s*:[^\n]*\b(restricted|confidential|secret)\b|internal use only|do not distribute|strictly confidential|confidential and proprietary)/i;
const DOCLIKE = /\.(md|mdx|txt|csv|json|ya?ml)$/i;
// Secret-type FILES and folders, not ordinary code that merely mentions credentials (features/credentials/data.ts is fine).
const SENSITIVE_PATH = /(^|\/)(\.env(\..+)?$|[^/]*\.(pem|key|p12|pfx|keystore)$|id_(rsa|ed25519|ecdsa)$|credentials\.(json|ya?ml)$|secrets?\.(json|ya?ml)$|secrets?\/)/i;
const EMBED_USD_PER_MTOK = 0.02; // text-embedding-3-small
const matchesAny = (f, globs) => globs.some((g) => globToRegExp(g).test(f));

/**
 * @param {{repo:string, policy:object, git:{listFiles:Function,readAt:Function}, commit:string, styles?:object}} a
 */
function planOnboarding({ repo, policy, git, commit, styles = { styles: {} } }) {
  const warnings = [];
  const blockers = [];
  const all = git.listFiles(commit);
  const exclude = policy.exclude || [];
  const scope = pagesScope(policy.pages);
  const allowed = selectFiles(all, { scope, exclude });
  const codeFiles = allowed.filter((f) => !DOC_FILE.test(f));
  const docFiles = all.filter((f) => (DOC_FILE.test(f) || matchesAny(f, policy.docs || [])) && !matchesAny(f, exclude));

  let codeChars = 0; let codeChunks = 0; let skipped = 0; let secretFiles = [];
  const sensitive = new Set(all.filter((f) => SENSITIVE_PATH.test(f) && allowed.includes(f)));
  for (const f of codeFiles) {
    const raw = git.readAt(commit, f);
    if (raw == null || raw.includes('\u0000') || Buffer.byteLength(raw) > 200_000) { skipped++; continue; }
    const clean = scrub(raw);
    if (clean !== raw) secretFiles.push(f);
    if (DOCLIKE.test(f) && SENSITIVE_CONTENT.test(raw)) sensitive.add(f);
    codeChars += clean.length;
    codeChunks += chunkCode(f, clean).length;
  }
  let docChars = 0; let docChunks = 0;
  for (const f of docFiles) {
    const raw = git.readAt(commit, f);
    if (raw == null) continue;
    if (SENSITIVE_CONTENT.test(raw)) sensitive.add(f);
    docChars += raw.length;
    docChunks += chunkMarkdown(raw).length;
  }

  const pages = (policy.pages?.length ? policy.pages : [{ path: 'overview.md', kind: policy.style }]).map((page) => {
    const files = selectFiles(all, { ...page, exclude: [...(page.exclude || []), ...exclude] });
    const snap = snapshot((f) => git.readAt(commit, f), files, { changed: files });
    const style = resolveStyle(styles, page.kind || policy.style);
    const row = {
      path: page.path, style: style.key, styleFallback: style.fallback, scopeFiles: files.length,
      snapshotFiles: snap.files.length, snapshotChars: snap.text.length, truncated: snap.files.length < files.length,
      hasBrief: !!page.brief, coverage: null,
    };
    if (style.coverage) { const c = checkCoverage('', snap.text, style.coverage); row.coverage = c ? Object.fromEntries(Object.entries(c.kinds).map(([k, v]) => [k, v.total])) : {}; }
    if (!files.length) blockers.push(`page ${page.path}: its scope matches NO files (scope: ${(page.scope || ['**']).join(', ')})`);
    else if (row.truncated) warnings.push(`page ${page.path}: ${files.length - snap.files.length} of ${files.length} in-scope files do not fit the model's snapshot (they are still reachable through retrieval). Narrow the scope or split the page.`);
    if (row.styleFallback) warnings.push(`page ${page.path}: style "${page.kind}" is not in config/doc-styles.json; the default style would be used`);
    if (!row.hasBrief) warnings.push(`page ${page.path}: no "brief". A brief tells the planner what the page is for and is searched as semantic context`);
    if (style.coverage && row.coverage && Object.values(row.coverage).every((n) => n === 0)) warnings.push(`page ${page.path}: style "${style.key}" enforces coverage but no declared names were found in scope (is the scope right?)`);
    return row;
  });

  if (new Set(pages.map((p) => p.path)).size !== pages.length) blockers.push('two pages share the same path');
  if ([...sensitive].length) blockers.push(`${sensitive.size} in-scope file(s) look sensitive (a secret-type path, or a "restricted/confidential" marker). Exclude them or confirm they may be sent to the model: ${[...sensitive].slice(0, 6).join(', ')}${sensitive.size > 6 ? ', ...' : ''}`);
  if (secretFiles.length) warnings.push(`${secretFiles.length} file(s) contain lines that look like secrets; those lines are removed before anything is embedded (${secretFiles.slice(0, 4).join(', ')}${secretFiles.length > 4 ? ', ...' : ''})`);
  if (!(policy.docs || []).length && !docFiles.length) warnings.push('no existing documentation found (no README or docs/ and no `docs` globs): the semantic context will only hold the page briefs');
  if (policy.trust === 'auto') warnings.push('trust is "auto": pages publish without a human. Start with "review".');
  if (!policy.glossary || !Object.keys(policy.glossary).length) warnings.push('no glossary: set the product name and any upstream names that must not appear');

  const tokens = Math.round((codeChars + docChars) / 4);
  return {
    repo, commit, mode: policy.mode || 'docs', trust: policy.trust,
    filesInRepo: all.length, excludedOrOutOfScope: all.length - allowed.length - docFiles.filter((f) => !allowed.includes(f)).length,
    code: { files: codeFiles.length - skipped, skipped, chunks: codeChunks, chars: codeChars },
    semantic: { files: docFiles.length, chunks: docChunks + pages.filter((p) => p.hasBrief).length },
    embeddings: { tokens, usd: Number((tokens / 1e6 * EMBED_USD_PER_MTOK).toFixed(4)) },
    pages, warnings, blockers,
  };
}

function print(r) {
  const n = (x) => x.toLocaleString('en-US');
  console.log(`\nOnboarding preview: ${r.repo} @ ${String(r.commit).slice(0, 7)}   mode=${r.mode}  trust=${r.trust}\n`);
  console.log(`  repo files               ${n(r.filesInRepo)}`);
  console.log(`  -> code context          ${n(r.code.files)} files, ${n(r.code.chunks)} chunks${r.code.skipped ? ` (${r.code.skipped} skipped: binary or over 200 KB)` : ''}`);
  console.log(`  -> semantic context      ${n(r.semantic.files)} doc files, ${n(r.semantic.chunks)} chunks (incl. page briefs)`);
  console.log(`  -> not indexed           ${n(r.excludedOrOutOfScope)} files (out of scope, excluded, tests, lockfiles, generated)`);
  console.log(`  embedding estimate       ~${n(r.embeddings.tokens)} tokens, about $${r.embeddings.usd} (text-embedding-3-small)\n`);
  console.table(r.pages.map((p) => ({ page: p.path, style: p.style, 'files in scope': p.scopeFiles, 'sent to model': p.snapshotFiles, chars: p.snapshotChars, brief: p.hasBrief ? 'yes' : 'NO', 'must cover': p.coverage ? JSON.stringify(p.coverage) : '-' })));
  for (const w of r.warnings) console.log(`  ! ${w}`);
  for (const b of r.blockers) console.log(`  ✘ ${b}`);
  console.log(r.blockers.length ? '\nBlocked: fix the items marked ✘ before onboarding.' : '\nNo blockers. Next: run the Bootstrap Context workflow (or scripts/bootstrap_context.js), then check with scripts/context_search.js --status.');
}

if (require.main === module) {
  const arg = (k) => { const i = process.argv.indexOf(`--${k}`); return i > -1 ? process.argv[i + 1] : undefined; };
  const repo = arg('repo'); const dir = arg('dir');
  if (!repo || !dir) { console.error('usage: onboard_check.js --repo owner/name --dir <checkout> [--ref sha]'); process.exit(2); }
  const G = require('./gitutil');
  const cfg = loadConfig();
  const reposConfig = readJson(cfg.paths.reposConfig, { defaults: {}, repos: {} });
  if (!(reposConfig.repos || {})[repo]) {
    console.error(`✘ ${repo} has no entry in ${cfg.paths.reposConfig}. Add one first (see examples/calendarscheduler/repos.entry.json).`);
    process.exit(1);
  }
  const git = G.accessors(dir);
  const result = planOnboarding({ repo, policy: repoPolicy(reposConfig, repo), git, commit: arg('ref') || G.head(dir), styles: loadStyles(cfg.paths.styles) });
  print(result);
  process.exit(result.blockers.length ? 1 : 0);
}

module.exports = { planOnboarding };
