'use strict';
/**
 * Code-driven mode: turn a commit's code changes into "change units", one per documentation page
 * the repo declares (config/repos.json -> repos[repo].pages). Pure functions; git access is injected
 * so the logic is testable without a repository.
 *
 * A page declares which files document it:
 *   { "path": "overview.md", "kind": "README / project overview", "scope": ["**"], "exclude": [] }
 *   { "path": "api.md",      "kind": "API documentation",         "scope": ["src/routes/**", "openapi.yaml"] }
 */

const DEFAULT_EXCLUDE = [
  '**/node_modules/**', '**/vendor/**', '**/dist/**', '**/build/**', '**/.next/**', '**/.astro/**', '**/coverage/**',
  '**/__pycache__/**', '**/*.min.js', '**/*.map', '**/*.lock', '**/package-lock.json', '**/pnpm-lock.yaml', '**/yarn.lock',
  '**/go.sum', '**/*.png', '**/*.jpg', '**/*.jpeg', '**/*.gif', '**/*.svg', '**/*.ico', '**/*.woff*', '**/*.pdf', '**/*.zip',
  '**/*.test.*', '**/*.spec.*', '**/test/**', '**/tests/**', '**/__tests__/**', '**/*_test.go',
  '.github/**', 'docs/**', 'documentation/**', '**/*.snap', '**/.env*', '**/*.pem', '**/*.key',
];
const SECRET_LINE = /(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key)\s*[:=]\s*['"]?[A-Za-z0-9_\-/+=]{16,}/i;

/** Minimal glob: ** any depth, * within a segment, ? one char. */
function globToRegExp(glob) {
  let re = '';
  for (let i = 0; i < glob.length; i++) {
    const ch = glob[i];
    if (ch === '*') {
      if (glob[i + 1] === '*') {
        re += glob[i + 2] === '/' ? '(?:.*/)?' : '.*';
        i += glob[i + 2] === '/' ? 2 : 1;
      } else re += '[^/]*';
    } else if (ch === '?') re += '[^/]';
    else re += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${re}$`);
}
const matchesAny = (file, globs) => globs.some((g) => globToRegExp(g).test(file));

function selectFiles(files, page = {}) {
  const scope = page.scope?.length ? page.scope : ['**'];
  const exclude = [...DEFAULT_EXCLUDE, ...(page.exclude || [])];
  return files.filter((f) => matchesAny(f, scope) && !matchesAny(f, exclude));
}

/** Drop lines that look like embedded secrets so they never reach a model or a log. */
const scrub = (text) => text.split('\n').map((l) => (SECRET_LINE.test(l) ? '[line removed: looks like a secret]' : l)).join('\n');

/**
 * Concatenate files into one snapshot. Changed files come first so they survive the size cap.
 * Binary-looking content is skipped; each file and the whole snapshot are capped.
 */
function snapshot(readFile, files, { changed = [], maxChars = 60000, maxFileChars = 12000 } = {}) {
  const order = [...files.filter((f) => changed.includes(f)), ...files.filter((f) => !changed.includes(f))];
  let out = '';
  const included = [];
  for (const f of order) {
    const raw = readFile(f);
    if (raw == null || raw.includes('\u0000')) continue;
    const body = scrub(raw.length > maxFileChars ? `${raw.slice(0, maxFileChars)}\n[truncated]` : raw);
    const block = `### FILE: ${f}\n${body}\n\n`;
    if (out.length + block.length > maxChars) break;
    out += block;
    included.push(f);
  }
  return { text: out, files: included };
}

/** Union of what the declared pages are allowed to read; no pages (or a page without scope) means everything. */
const pagesScope = (pages = []) => (pages.length && pages.every((p) => p.scope?.length) ? [...new Set(pages.flatMap((p) => p.scope))] : ['**']);

/**
 * @param {object} a
 * @param {string} a.repo
 * @param {object} a.policy        repo policy (mode, pages, style, exclude)
 * @param {string} a.commit        commit being documented
 * @param {string} [a.before]      previous commit ('' = first run / full sync)
 * @param {(rev:string)=>string[]} a.listFiles
 * @param {(rev:string,file:string)=>string|null} a.readAt
 * @param {(before:string,after:string)=>string[]} a.changedBetween
 * @param {(page:string)=>string} a.readExistingPage  body of the currently published page ('' if none)
 * @param {boolean} [a.full]       treat every scoped file as changed
 * @returns {object[]} change units (kind: 'code'); pages whose files did not change are omitted
 */
function buildCodeChanges({ repo, policy, commit, before, listFiles, readAt, changedBetween, readExistingPage, full = false }) {
  const pages = policy.pages?.length ? policy.pages : [{ path: 'overview.md', kind: policy.style }];
  const afterFiles = listFiles(commit);
  const beforeFiles = before ? listFiles(before) : [];
  const changedAll = full || !before ? afterFiles : changedBetween(before, commit);
  const units = [];
  for (const page of pages) {
    const scoped = selectFiles(afterFiles, { ...page, exclude: [...(page.exclude || []), ...(policy.exclude || [])] });
    const changed = scoped.filter((f) => changedAll.includes(f));
    const removed = selectFiles(beforeFiles, { ...page, exclude: [...(page.exclude || []), ...(policy.exclude || [])] }).filter((f) => !afterFiles.includes(f) && changedAll.includes(f));
    if (!changed.length && !removed.length) continue;
    const after = snapshot((f) => readAt(commit, f), scoped, { changed });
    const prev = before ? snapshot((f) => readAt(before, f), selectFiles(beforeFiles, { ...page, exclude: [...(page.exclude || []), ...(policy.exclude || [])] }), { changed: changed.concat(removed) }) : { text: '' };
    units.push({
      kind: 'code', repo, filePath: page.path, styleKey: page.kind || policy.style || null, commit,
      before: prev.text || null, after: after.text, existing: readExistingPage(page.path) || '',
      brief: page.brief || '', title: page.title || '', repoMap: scoped.slice(0, 400), snapshotFiles: after.files,
      changedFiles: [...changed, ...removed.map((f) => `${f} (removed)`)],
    });
  }
  return units;
}

module.exports = { pagesScope, globToRegExp, selectFiles, snapshot, buildCodeChanges, scrub, DEFAULT_EXCLUDE };
