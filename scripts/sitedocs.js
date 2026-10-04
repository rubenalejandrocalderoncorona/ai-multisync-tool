'use strict';
/** Read the pages of a documentation site checkout (Starlight layout) for semantic-context indexing. */
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { globToRegExp } = require('../pipeline/codesource');

// Pages the pipeline itself writes are indexed per source repo (kind approved), not as site context.
const DEFAULT_EXCLUDE = ['src/content/docs/services/**', 'src/content/docs/projects/**'];

function siteFiles(dir, exclude = DEFAULT_EXCLUDE) {
  const root = path.join(dir, 'src/content/docs');
  if (!fs.existsSync(root)) return [];
  const out = [];
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (/\.(md|mdx)$/.test(e.name)) out.push(path.relative(dir, p).split(path.sep).join('/'));
    }
  };
  walk(root);
  const ex = exclude.map(globToRegExp);
  return out.filter((f) => !ex.some((re) => re.test(f))).sort();
}

function siteCommit(dir) {
  try { return execFileSync('git', ['-C', dir, 'rev-parse', 'HEAD'], { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] }).trim(); } catch { return 'unversioned'; }
}

const readSiteFile = (dir) => (f) => { try { return fs.readFileSync(path.join(dir, f), 'utf-8'); } catch { return null; } };

module.exports = { siteFiles, siteCommit, readSiteFile, DEFAULT_EXCLUDE };
