'use strict';
/** Thin git accessors over a checkout directory, shared by the runner and the bootstrap command. */
const { execFileSync } = require('child_process');

const git = (dir, ...args) => execFileSync('git', ['-C', dir, ...args], { encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });

const revExists = (dir, rev) => { try { git(dir, 'cat-file', '-e', `${rev}^{commit}`); return true; } catch { return false; } };
const listFiles = (dir, rev) => git(dir, 'ls-tree', '-r', '--name-only', rev).split('\n').filter(Boolean);
const readAt = (dir, rev, file) => { try { return git(dir, 'show', `${rev}:${file}`); } catch { return null; } };
const changedBetween = (dir, a, b) => git(dir, 'diff', '--name-only', a, b).split('\n').filter(Boolean);
const head = (dir) => git(dir, 'rev-parse', 'HEAD').trim();

/** The accessors in the shape pipeline/context.js and pipeline/codesource.js expect. */
const accessors = (dir) => ({
  listFiles: (rev) => listFiles(dir, rev),
  readAt: (rev, f) => readAt(dir, rev, f),
  changedBetween: (a, b) => changedBetween(dir, a, b),
});

module.exports = { git, revExists, listFiles, readAt, changedBetween, head, accessors };
