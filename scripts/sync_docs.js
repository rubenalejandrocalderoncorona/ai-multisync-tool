#!/usr/bin/env node
/**
 * Documentation Sync Script
 * Reads analysis_results.json written by analyze_docs.js, then for each file
 * marked should_sync: applies AI template selection, template restructuring,
 * AI language polish, and frontmatter injection before writing to the target path.
 *
 * Reads from environment:
 *   INTERNAL_AI_API_KEY  — API key for the AI provider
 *   AI_API_HOST          — hostname of the AI API (default: api.openai.com)
 *   AI_API_PATH          — path for chat completions (default: /v1/chat/completions)
 *   AI_MODEL             — model name (default: gpt-4o)
 *   INSTRUCTIONS_FILE    — optional path to a custom instructions file
 *   TEMPLATES_PATH       — optional service-specific template subdirectory
 *   SOURCE_REPO          — full source repo path (org/repo)
 *   SOURCE_REF           — branch/SHA used for backlink URLs
 *   BRANCH_NAME          — branch name for backlink URLs
 *   SERVICE_NAME         — override the service folder name
 *   TARGET_PATH          — full explicit destination path override
 *
 * Writes: sync-summary.json
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

const BRANCH_NAME = process.env.BRANCH_NAME || process.env.SOURCE_REF || 'unknown';
const sourceRef   = process.env.SOURCE_REF || BRANCH_NAME;
const instructionsFilePath = process.env.INSTRUCTIONS_FILE || '';
const templatesPath        = process.env.TEMPLATES_PATH || '';
const apiKey               = process.env.INTERNAL_AI_API_KEY || '';

// AI API configuration
const AI_API_HOST = process.env.AI_API_HOST || 'api.openai.com';
const AI_API_PATH = process.env.AI_API_PATH || '/v1/chat/completions';
const AI_MODEL    = process.env.AI_MODEL    || 'gpt-4o';

const analysisData  = JSON.parse(fs.readFileSync('analysis_results.json', 'utf-8'));
const sourceRepo    = process.env.SOURCE_REPO || 'unknown-repo';
const sourceRepoName = sourceRepo.split('/').pop();

console.log(`📦 Processing files for repository: ${sourceRepoName}`);
console.log(`🎯 Target branch: ${BRANCH_NAME}`);
console.log(`🤖 AI model: ${AI_MODEL} @ ${AI_API_HOST}`);

// ─── Load instructions file ───────────────────────────────────────────────────
// Always loads DocumentationInstructions.instructions.md as the baseline.
// INSTRUCTIONS_FILE overrides the default when set and the file exists.
const DEFAULT_INSTRUCTIONS_PATH = '.github/instructions/DocumentationInstructions.instructions.md';
let instructionsContent = '';
const resolvedInstructionsPath = instructionsFilePath
  ? path.join(process.cwd(), instructionsFilePath)
  : path.join(process.cwd(), DEFAULT_INSTRUCTIONS_PATH);

if (instructionsFilePath && !fs.existsSync(resolvedInstructionsPath)) {
  console.warn(`⚠️  Instructions file not found: ${resolvedInstructionsPath} — falling back to default`);
}

const finalInstructionsPath = (instructionsFilePath && fs.existsSync(resolvedInstructionsPath))
  ? resolvedInstructionsPath
  : path.join(process.cwd(), DEFAULT_INSTRUCTIONS_PATH);

if (fs.existsSync(finalInstructionsPath)) {
  instructionsContent = fs.readFileSync(finalInstructionsPath, 'utf-8');
  console.log(`📋 Instructions loaded: ${finalInstructionsPath}`);
} else {
  console.warn(`⚠️  Default instructions file not found: ${finalInstructionsPath} — template/polish will run without standards`);
}

// ─── AI helper ────────────────────────────────────────────────────────────────
async function callAI(messages) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify({ model: AI_MODEL, messages });
    const options = {
      hostname: AI_API_HOST,
      path: AI_API_PATH,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`,
        'Content-Length': Buffer.byteLength(data),
        'accept': 'application/json'
      }
    };
    const req = https.request(options, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(body)); }
        catch (e) { reject(new Error(`Failed to parse AI response: ${e.message}`)); }
      });
    });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

// ─── AI language polish ───────────────────────────────────────────────────────
async function polishDocument(filePath, content) {
  if (!instructionsContent || !apiKey) return null;

  const systemPrompt = `You are a senior technical writer. You will receive a markdown document and a set of documentation standards.

Apply the standards to improve the document: fix spelling errors, grammar mistakes, formatting inconsistencies, and structure issues.

STRICT RULES:
- Preserve ALL technical content: commands, code blocks, URLs, configuration values, parameter names, and system identifiers exactly as written.
- Do NOT add, remove, or change any technical facts or procedures.
- Only improve: spelling, grammar, tone, heading structure, formatting, and section organization as required by the standards.
- Return ONLY a JSON object with two fields:
  1. "content": the full improved markdown document as a string
  2. "constraints_applied": an array of short strings describing each specific change made

Documentation Standards:
${instructionsContent}`;

  const userPrompt = `Filename: ${path.basename(filePath)}\nFull path: ${filePath}\n\n${content}`;

  try {
    const response = await callAI([
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt }
    ]);
    const raw = response.choices[0].message.content;
    const clean = raw.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '').trim();
    const parsed = JSON.parse(clean);
    return {
      polishedContent: parsed.content,
      constraintsApplied: Array.isArray(parsed.constraints_applied) ? parsed.constraints_applied : []
    };
  } catch (error) {
    console.warn(`⚠️  AI polish failed for ${filePath}: ${error.message}`);
    return null;
  }
}

// ─── Template selection ───────────────────────────────────────────────────────
const DEFAULT_TEMPLATE_PATH = 'docs/templates/default-template/default-template.md';
const SHARED_TEMPLATES_DIR  = 'docs/templates';

function findTemplateFiles(dir) {
  const results = [];
  if (!fs.existsSync(dir)) return results;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...findTemplateFiles(fullPath));
    } else if (/\.(md|mdx)$/.test(entry.name) && entry.name !== '_category_.json') {
      results.push(fullPath);
    }
  }
  return results;
}

function buildTemplateCatalogue(templateFiles) {
  return templateFiles.map(filePath => {
    try {
      const raw = fs.readFileSync(filePath, 'utf-8');
      const stripped = raw.replace(/^---[\s\S]*?---\n+/, '');
      const firstLine = stripped.split('\n').find(l => l.trim().length > 0) || '';
      return `- ${filePath}: ${firstLine.replace(/^#+\s*/, '').trim()}`;
    } catch {
      return `- ${filePath}`;
    }
  }).join('\n');
}

async function selectTemplate(filePath, content, templateFiles) {
  if (!apiKey || templateFiles.length === 0) return null;

  const catalogue = buildTemplateCatalogue(templateFiles);
  const systemPrompt = `You are a technical documentation specialist. Given a documentation file and a list of available templates, identify the single best-fitting template.

Available templates:
${catalogue}

Rules:
- Reply with ONLY the exact file path of the best-matching template (e.g. docs/templates/sop-template/sop-template.md).
- If none of the templates clearly fits, reply with the single word: DEFAULT
- Do not add any explanation.`;

  const userPrompt = `Filename: ${path.basename(filePath)}\n\nContent (first 3000 chars):\n${content.slice(0, 3000)}`;

  try {
    const response = await callAI([
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt }
    ]);
    const choice = (response.choices[0].message.content || '').trim();
    if (choice === 'DEFAULT' || !choice) return null;
    if (templateFiles.includes(choice)) return choice;
    const normalized = choice.replace(/^\.\//, '');
    return templateFiles.find(f => f === normalized || f.endsWith(normalized)) || null;
  } catch (err) {
    console.warn(`⚠️  Template selection AI call failed: ${err.message}`);
    return null;
  }
}

async function applyTemplate(filePath, content, templateFilePath) {
  if (!apiKey) return content;

  let templateContent = '';
  try {
    templateContent = fs.readFileSync(templateFilePath, 'utf-8');
  } catch {
    console.warn(`⚠️  Could not read template file: ${templateFilePath}`);
    return content;
  }

  const systemPrompt = `You are a senior technical writer. You will receive a documentation file and a template.

Restructure the documentation file to follow the template's section headings and layout.

STRICT RULES:
- Preserve ALL technical content: commands, code blocks, URLs, configuration values, parameter names, API details, and system identifiers exactly as written.
- Do NOT invent, add, or remove technical facts.
- Fill in template sections using only the information present in the source document.
- Return ONLY the restructured markdown document as a plain string — no JSON wrapper, no code fences.

Documentation Standards to apply:
${instructionsContent}`;

  const userPrompt = `Template to follow:\n${templateContent}\n\n---\n\nDocument to restructure (filename: ${path.basename(filePath)}):\n${content}`;

  try {
    const response = await callAI([
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt }
    ]);
    const result = (response.choices[0].message.content || '').trim();
    if (result.length > 50) {
      console.log(`   📐 Template applied: ${path.basename(templateFilePath)}`);
      return result;
    }
  } catch (err) {
    console.warn(`⚠️  Template application AI call failed for ${filePath}: ${err.message}`);
  }
  return content;
}

// ─── Path helper ──────────────────────────────────────────────────────────────
function getTargetPath(sourceFile, repoName, targetSubfolder) {
  const normalizedFile = sourceFile.replace(/\.txt$/, '.md');
  const relativePath = normalizedFile.replace(/^(docs\/|documentation\/)/, '');
  const targetPathOverride = process.env.TARGET_PATH || '';
  const serviceName = process.env.SERVICE_NAME || repoName;

  let targetPath;
  if (targetPathOverride) {
    targetPath = path.join(targetPathOverride, relativePath);
  } else if (targetSubfolder) {
    targetPath = path.join('docs', 'services', serviceName, targetSubfolder, relativePath);
  } else {
    targetPath = path.join('docs', 'services', serviceName, relativePath);
  }

  return {
    path: targetPath,
    category: serviceName,
    subfolder: targetSubfolder || null,
    fileName: path.basename(normalizedFile)
  };
}

// ─── Category JSON helper ─────────────────────────────────────────────────────
function ensureCategoryJson(folderPath, subfolderSlug) {
  const categoryFile = path.join(folderPath, '_category_.json');
  if (fs.existsSync(categoryFile)) return;

  const label = subfolderSlug
    .replace(/-/g, ' ')
    .replace(/^./, c => c.toUpperCase());

  fs.writeFileSync(categoryFile, JSON.stringify({ label, link: { type: 'generated-index' } }, null, 2) + '\n');
  console.log(`   📁 Created _category_.json: ${categoryFile}`);
}

// ─── Frontmatter injection ────────────────────────────────────────────────────
function injectFrontmatter(content, sourceFile) {
  const now = new Date().toISOString();
  // Build source URL — works for both github.com and GitHub Enterprise
  const githubHost = process.env.GITHUB_HOST || 'github.com';
  const sourceUrl = `https://${githubHost}/${sourceRepo}/blob/${sourceRef}/${sourceFile}`;
  const fileName = path.basename(sourceFile, path.extname(sourceFile));
  const sidebarLabel = fileName.replace(/[_-]/g, ' ').replace(/^./, c => c.toUpperCase());

  const cleanContent = content.replace(/^---\n[\s\S]*?\n---\n\n?/, '');

  return `---
source: ${sourceUrl}
last_synced: ${now}
automated: true
sidebar_label: ${sidebarLabel}
---

${cleanContent}`;
}

// ─── Main processing loop ─────────────────────────────────────────────────────
const synced = [];
const deleted = [];
const skipped = [];

async function processFiles() {
  for (const item of analysisData.results) {
    if (!item.analysis.should_sync) {
      skipped.push({ source: item.file, reason: item.analysis.reason });
      continue;
    }

    const targetInfo = getTargetPath(item.file, sourceRepoName, item.analysis.target_subfolder || null);
    const targetPath = targetInfo.path;

    if (item.status === 'deleted') {
      if (fs.existsSync(targetPath)) {
        fs.unlinkSync(targetPath);
        console.log(`🗑️  Deleted: ${targetPath}`);
        deleted.push({ source: item.file, target: targetPath, category: targetInfo.category });
      }
      continue;
    }

    const sourcePath = path.join('source-repo', item.file);
    if (!fs.existsSync(sourcePath)) {
      console.warn(`⚠️  Source file not found: ${sourcePath}`);
      continue;
    }

    fs.mkdirSync(path.dirname(targetPath), { recursive: true });
    if (targetInfo.subfolder) {
      ensureCategoryJson(path.dirname(targetPath), targetInfo.subfolder);
    }

    let content = fs.readFileSync(sourcePath, 'utf-8');
    let constraintsApplied = [];
    let polished = false;
    let templateApplied = null;

    if (apiKey) {
      const templateDir = templatesPath
        ? path.join(process.cwd(), templatesPath)
        : path.join(process.cwd(), SHARED_TEMPLATES_DIR);

      const templateFiles = findTemplateFiles(templateDir)
        .filter(f => !f.endsWith('default-template.md'));

      console.log(`🗂️  Selecting template for: ${item.file} (${templateFiles.length} candidates)`);

      const chosenTemplate = await selectTemplate(item.file, content, templateFiles);
      const templateToApply = chosenTemplate || path.join(process.cwd(), DEFAULT_TEMPLATE_PATH);
      templateApplied = path.basename(path.dirname(templateToApply));

      if (!chosenTemplate) {
        console.log(`   ↩️  No strong match — using default template`);
      }

      content = await applyTemplate(item.file, content, templateToApply);
    }

    if (instructionsContent && apiKey) {
      console.log(`✍️  Polishing: ${item.file}...`);
      const result = await polishDocument(item.file, content);
      if (result) {
        content = result.polishedContent;
        constraintsApplied = result.constraintsApplied;
        polished = true;
        console.log(`   ✅ Polish applied (${constraintsApplied.length} constraint(s))`);
      }
    }

    content = injectFrontmatter(content, item.file);
    fs.writeFileSync(targetPath, content);
    console.log(`✅ Synced: ${item.file} → ${targetPath}`);

    synced.push({
      source: item.file,
      target: targetPath,
      category: targetInfo.category,
      subfolder: targetInfo.subfolder,
      fileName: targetInfo.fileName,
      templateApplied,
      polished,
      constraintsApplied
    });
  }

  const summary = {
    repository_name: sourceRepoName,
    synced_files: synced,
    deleted_files: deleted,
    skipped_files: skipped,
    aiSummary: analysisData.aiGeneratedSummary || 'No AI summary provided',
    branch: BRANCH_NAME,
    instructions_file: instructionsFilePath || null
  };

  fs.writeFileSync('sync-summary.json', JSON.stringify(summary, null, 2));

  console.log('\n📊 Sync Summary:');
  console.log(`   ✅ Synced:   ${synced.length} files`);
  console.log(`   🗑️  Deleted:  ${deleted.length} files`);
  console.log(`   ⏭️  Skipped:  ${skipped.length} files`);
  console.log(`\n💾 Summary saved to sync-summary.json`);
}

processFiles().catch(error => {
  console.error('❌ Fatal error:', error);
  process.exit(1);
});
