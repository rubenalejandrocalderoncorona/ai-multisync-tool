#!/usr/bin/env node
/**
 * Generate Job Summary
 * Produces the GitHub Actions step summary for the documentation sync workflow.
 * Reads sync-summary.json written by sync_docs.js and analysis_results.json
 * written by analyze_docs.js.
 */

const fs = require('fs');

let summary;
try {
  summary = JSON.parse(fs.readFileSync('sync-summary.json', 'utf-8'));
} catch (e) {
  console.error('❌ Could not read sync-summary.json:', e.message);
  process.exit(1);
}

let folderSpecSource = 'unknown';
try {
  const analysis = JSON.parse(fs.readFileSync('analysis_results.json', 'utf-8'));
  folderSpecSource = analysis.folderSpecSource || 'unknown';
} catch { /* non-fatal */ }

const sourceRepo    = process.env.SOURCE_REPO  || summary.repository_name || 'unknown';
const sourceRef     = process.env.SOURCE_REF   || summary.branch          || 'unknown';
const sourceSha     = process.env.SOURCE_SHA   || 'unknown';
const targetBranch  = process.env.TARGET_BRANCH || summary.branch         || 'unknown';

const synced           = summary.synced_files  || [];
const deleted          = summary.deleted_files || [];
const skipped          = summary.skipped_files || [];
const instructionsFile = summary.instructions_file || null;

const aiMode      = process.env.INTERNAL_AI_API_KEY ? 'full-ai' : 'no-ai';
const anyPolished = synced.some(f => f.polished);

function line(text = '') { process.stdout.write(text + '\n'); }

line('# 🚀 Documentation Sync Summary');
line();

line('## 📋 Pipeline Metadata');
line();
line('| Field | Value |');
line('| :---- | :---- |');
line(`| **Source Branch** | \`${sourceRef}\` |`);
line(`| **Repository** | \`${sourceRepo}\` |`);
line(`| **Commit** | \`${sourceSha}\` |`);
line(`| **Target Branch** | \`${targetBranch}\` |`);
if (instructionsFile) {
  line(`| **Instructions File** | \`${instructionsFile}\` |`);
}
line();

// Instruction adherence
const polishedFiles = synced.filter(f => f.polished && f.constraintsApplied && f.constraintsApplied.length > 0);
if (polishedFiles.length > 0) {
  line('## 📋 Instruction Adherence');
  line();
  for (const f of polishedFiles) {
    line(`### Constraints Applied to \`${f.source}\``);
    line();
    for (const constraint of f.constraintsApplied) {
      line(`> - ${constraint}`);
    }
    line();
  }
}

// Pipeline features
line('## ⚙️ Pipeline Features Applied');
line();
line('| Feature | Status | Details |');
line('| :------ | :----- | :------ |');

const aiStatus = aiMode === 'full-ai' ? '✅ `full-ai` (API key present)' : '⚠️ `no-ai` (API key missing)';
line(`| 🤖 AI Mode | ${aiStatus} | Controls polish, template selection, language rewriting |`);

if (instructionsFile) {
  const polishStatus = anyPolished
    ? `✅ Applied | ${polishedFiles.length} file(s) rewritten`
    : '— No changes needed | Files already conform to standards';
  line(`| ✍️ Language Polish | ${polishStatus} |`);
} else {
  line('| ✍️ Language Polish | — Default instructions | DocumentationInstructions.instructions.md applied |');
}

const folderSpecLabels = {
  'service-instructions': '✅ Service-specific `## Folder Structure` section',
  'default-instructions': '⚠️ Default instructions (no service spec defined)',
  'ai-default':           '🤖 AI best-practice defaults (no spec found)'
};
line(`| 📂 Folder Routing | ${folderSpecLabels[folderSpecSource] || folderSpecSource} | AI assigns each file to the correct subfolder |`);

const templatedFiles = synced.filter(f => f.templateApplied);
if (templatedFiles.length > 0) {
  line(`| 📐 Template Applied | ✅ ${templatedFiles.length} file(s) | AI-selected template restructured content |`);
} else {
  line('| 📐 Template Applied | — Default | No specific template matched; default template used |');
}

line(`| 📄 Files Synced | ${synced.length > 0 ? '✅' : '—'} ${synced.length} file(s) | Namespaced under \`docs/services/${summary.repository_name}/\` |`);

if (deleted.length > 0) line(`| 🗑️ Files Deleted | ✅ ${deleted.length} file(s) | Removed from central repo |`);
if (skipped.length > 0) line(`| ⏭️ Files Skipped | ℹ️ ${skipped.length} file(s) | AI determined not relevant for sync |`);
line();

// Synced files table
if (synced.length > 0) {
  line(`## ✅ Synced Files (${synced.length})`);
  line();
  line('| Source File | Target Path | Subfolder | Template | Polish |');
  line('| :---------- | :---------- | :-------- | :------- | :----- |');
  for (const f of synced) {
    const subfolderCell = f.subfolder ? `\`${f.subfolder}\`` : '— (root)';
    const templateCell  = f.templateApplied ? `\`${f.templateApplied}\`` : '— (default)';
    const polishCell    = f.polished ? `✅ (${f.constraintsApplied.length})` : '—';
    line(`| \`${f.source}\` | \`${f.target}\` | ${subfolderCell} | ${templateCell} | ${polishCell} |`);
  }
  line();
}

if (deleted.length > 0) {
  line(`## 🗑️ Deleted Files (${deleted.length})`);
  line();
  for (const f of deleted) line(`- \`${f.source}\` → \`${f.target}\``);
  line();
}

if (skipped.length > 0) {
  line(`## ⏭️ Skipped Files (${skipped.length})`);
  line();
  for (const f of skipped) line(`- \`${f.source}\` — ${f.reason}`);
  line();
}

line('---');
line(`*Automated sync completed at ${new Date().toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC')}*`);
