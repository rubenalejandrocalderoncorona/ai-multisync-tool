'use strict';
/**
 * Runs the real CLI (scripts/run_pipeline.js) as a subprocess against a real git repository and a
 * local OpenAI-compatible HTTP server. Verifies the contract the GitHub workflow depends on:
 * git plumbing, mode selection, files written, pipeline-results.json, node logs on stdout.
 */
const test = require('node:test');
const assert = require('node:assert');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawn } = require('node:child_process');
const { passJudge } = require('./helpers');
const { startFakeOpenAI } = require('./fake-openai');

const run = (cmd, args, opts) => new Promise((resolve) => {
  const p = spawn(cmd, args, opts);
  let out = '';
  p.stdout.on('data', (d) => (out += d));
  p.stderr.on('data', (d) => (out += d));
  p.on('close', (code) => resolve({ code, out }));
});

test('CLI code mode end to end: real git repo -> graph -> page on disk -> results + logs', async () => {
  const { server, hits, url } = await startFakeOpenAI();
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'msync-'));
  const src = path.join(work, 'source-repo');
  const sh = (...a) => execFileSync('git', ['-C', src, ...a], { encoding: 'utf-8' });
  try {
    fs.mkdirSync(path.join(src, 'src'), { recursive: true });
    execFileSync('git', ['init', '-q', '-b', 'main', src]);
    sh('config', 'user.email', 't@t'); sh('config', 'user.name', 't');
    fs.writeFileSync(path.join(src, 'src/a.go'), 'package main\nfunc Alert() {}\n');
    fs.writeFileSync(path.join(src, 'src/a_test.go'), 'package main\nfunc TestAlert() {}\n');
    sh('add', '-A'); sh('commit', '-qm', 'one');
    const c1 = sh('rev-parse', 'HEAD').trim();
    fs.writeFileSync(path.join(src, 'src/a.go'), 'package main\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n');
    sh('add', '-A'); sh('commit', '-qm', 'two');
    const c2 = sh('rev-parse', 'HEAD').trim();

    fs.writeFileSync(path.join(work, 'repos.json'), JSON.stringify({
      repos: { 'o/proj': { mode: 'code', trust: 'auto', serviceName: 'proj', pages: [{ path: 'api.md', kind: 'API documentation', scope: ['src/**'] }] } },
    }));
    const env = {
      ...process.env, AI_API_BASE_URL: url, INTERNAL_AI_API_KEY: 'x', VECTOR_DRIVER: 'memory', FACTSTORE_DRIVER: 'memory',
      REPOS_CONFIG: path.join(work, 'repos.json'), FEATURE_REGISTRY: path.join(work, 'none.json'),
      DOCS_ROOT: 'site/docs', INSTRUCTIONS_FILE: path.join(__dirname, '../.github/instructions/DocumentationInstructions.instructions.md'),
      TEMPLATES_PATH: path.join(__dirname, '../docs/templates'), SOURCE_REPO: 'o/proj', SOURCE_SHA: c2, SOURCE_BEFORE: c1,
      SOURCE_DIR: src, CHANGED_FILES: '', TICKET_PROVIDER: 'none',
    };
    const r = await run('node', [path.join(__dirname, '../scripts/run_pipeline.js')], { cwd: work, env });
    assert.strictEqual(r.code, 0, r.out);
    assert.match(r.out, /mode=code \| 1 change unit/);
    for (const node of ['sync_context', 'prefilter', 'cross_repo', 'similarity', 'code_context', 'gar', 'semantic_context', 'write_draft', 'judge', 'publish']) assert.match(r.out, new RegExp(`\\[${node}\\]`));
    assert.match(r.out, /\[sync_context\] ok \d+ms \{"mode":"full","reason":"no previous commit"|\[sync_context\] ok \d+ms \{"mode":"full"/);

    const results = JSON.parse(fs.readFileSync(path.join(work, 'pipeline-results.json'), 'utf-8'));
    assert.strictEqual(results.results.length, 1);
    const d = results.results[0];
    assert.strictEqual(d.outcome, 'published');
    assert.strictEqual(d.mode, 'code');
    assert.strictEqual(d.style, 'API documentation');
    assert.deepStrictEqual(d.stages.map((s) => s.split(':')[0]), ['prefilter', 'cross_repo', 'similarity', 'code_context', 'gar', 'semantic_context', 'write_draft', 'judge', 'publish']);

    const page = fs.readFileSync(path.join(work, d.targetPath), 'utf-8');
    assert.match(d.targetPath, /^site\/docs\/services\/proj\/api\.md$/);
    assert.match(page, /^---\ntitle: /);
    assert.match(page, /doc_key: api\.md/);
    assert.ok(!page.includes('TestAlert'), 'test files never reach the model or the page');
    assert.ok(hits.judge >= 1 && hits.embed >= 1);
    assert.strictEqual(hits.analyze, 1, 'code context stage ran once');
    assert.strictEqual(hits.plan, 1, 'semantic context stage ran once');
  } finally {
    server.close();
    fs.rmSync(work, { recursive: true, force: true });
  }
});

test('CLI fails safe: model unreachable becomes a pipeline_error fallback, nothing is written', async () => {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'msync-'));
  const src = path.join(work, 'source-repo');
  const sh = (...a) => execFileSync('git', ['-C', src, ...a], { encoding: 'utf-8' });
  try {
    fs.mkdirSync(path.join(src, 'src'), { recursive: true });
    execFileSync('git', ['init', '-q', '-b', 'main', src]);
    sh('config', 'user.email', 't@t'); sh('config', 'user.name', 't');
    fs.writeFileSync(path.join(src, 'src/a.go'), 'package main\nfunc A() {}\nfunc B() {}\nfunc C() {}\n');
    sh('add', '-A'); sh('commit', '-qm', 'one');
    const c = sh('rev-parse', 'HEAD').trim();
    fs.writeFileSync(path.join(work, 'repos.json'), JSON.stringify({ repos: { 'o/proj': { mode: 'code', serviceName: 'proj' } } }));
    const env = { ...process.env, AI_API_BASE_URL: 'http://127.0.0.1:9', INTERNAL_AI_API_KEY: 'x', AI_TIMEOUT_MS: '1500', AI_MAX_RETRIES: '0', VECTOR_DRIVER: 'memory', FACTSTORE_DRIVER: 'memory', REPOS_CONFIG: path.join(work, 'repos.json'), FEATURE_REGISTRY: path.join(work, 'n.json'), DOCS_ROOT: 'site/docs', SOURCE_REPO: 'o/proj', SOURCE_SHA: c, SOURCE_BEFORE: '0'.repeat(40), SOURCE_DIR: src, TICKET_PROVIDER: 'none', TEMPLATES_PATH: path.join(__dirname, '../docs/templates') };
    const r = await run('node', [path.join(__dirname, '../scripts/run_pipeline.js')], { cwd: work, env });
    assert.strictEqual(r.code, 0, r.out);
    const d = JSON.parse(fs.readFileSync(path.join(work, 'pipeline-results.json'), 'utf-8')).results[0];
    assert.strictEqual(d.outcome, 'fallback');
    assert.strictEqual(d.rootCauseTag, 'pipeline_error');
    assert.ok(!fs.existsSync(path.join(work, 'site')), 'no page is written on a pipeline error');
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
});

test('CLI: a protected target branch (main) forces review even for a trust=auto repo', async () => {
  const { server, url } = await startFakeOpenAI();
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'msync-'));
  const src = path.join(work, 'source-repo');
  const sh = (...a) => execFileSync('git', ['-C', src, ...a], { encoding: 'utf-8' });
  try {
    fs.mkdirSync(path.join(src, 'src'), { recursive: true });
    execFileSync('git', ['init', '-q', '-b', 'main', src]);
    sh('config', 'user.email', 't@t'); sh('config', 'user.name', 't');
    fs.writeFileSync(path.join(src, 'src/a.go'), 'package main\nfunc Alert() {}\n');
    sh('add', '-A'); sh('commit', '-qm', 'one'); const c1 = sh('rev-parse', 'HEAD').trim();
    fs.writeFileSync(path.join(src, 'src/a.go'), 'package main\nfunc Alert() {}\nfunc Silence(id string) {}\nvar channels = []string{"email", "slack", "sms"}\nconst Port = 8081\n');
    sh('add', '-A'); sh('commit', '-qm', 'two'); const c2 = sh('rev-parse', 'HEAD').trim();
    fs.writeFileSync(path.join(work, 'repos.json'), JSON.stringify({ repos: { 'o/proj': { mode: 'code', trust: 'auto', serviceName: 'proj', pages: [{ path: 'api.md', scope: ['src/**'] }] } } }));
    const env = { ...process.env, AI_API_BASE_URL: url, INTERNAL_AI_API_KEY: 'x', VECTOR_DRIVER: 'memory', FACTSTORE_DRIVER: 'memory', REPOS_CONFIG: path.join(work, 'repos.json'), FEATURE_REGISTRY: path.join(work, 'n.json'), DOCS_ROOT: 'site/docs', TEMPLATES_PATH: path.join(__dirname, '../docs/templates'), SOURCE_REPO: 'o/proj', SOURCE_SHA: c2, SOURCE_BEFORE: c1, SOURCE_DIR: src, TICKET_PROVIDER: 'none' };
    const direct = await run('node', [path.join(__dirname, '../scripts/run_pipeline.js')], { cwd: work, env: { ...env, TARGET_BRANCH: 'staging' } });
    assert.strictEqual(JSON.parse(fs.readFileSync(path.join(work, 'pipeline-results.json'), 'utf-8')).results[0].outcome, 'published', direct.out);
    const prot = await run('node', [path.join(__dirname, '../scripts/run_pipeline.js')], { cwd: work, env: { ...env, TARGET_BRANCH: 'main' } });
    assert.match(prot.out, /trust forced from auto to review/);
    assert.strictEqual(JSON.parse(fs.readFileSync(path.join(work, 'pipeline-results.json'), 'utf-8')).results[0].outcome, 'pending_review');
  } finally { server.close(); fs.rmSync(work, { recursive: true, force: true }); }
});
