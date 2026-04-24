#!/usr/bin/env node
/**
 * Documentation Analysis Script
 * Uses AI to determine if changed files require documentation sync,
 * and which subfolder each file belongs in within the service directory.
 *
 * Reads from environment:
 *   CHANGED_FILES        — newline-separated list of changed file paths
 *   CODE_DIFF            — raw git diff (optional, used as context)
 *   AI_SUMMARY           — pre-approval signal from source repo (skips per-file analysis)
 *   INTERNAL_AI_API_KEY  — API key for the AI provider
 *   AI_API_HOST          — hostname of the AI API (default: api.openai.com)
 *   AI_API_PATH          — path for chat completions (default: /v1/chat/completions)
 *   AI_MODEL             — model name (default: gpt-4o)
 *   INSTRUCTIONS_FILE    — optional path to a custom instructions file
 *
 * Writes: analysis_results.json
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

const changedFiles = (process.env.CHANGED_FILES || '')
  .split('\n')
  .filter(f => f.trim());
const codeDiff             = process.env.CODE_DIFF;
const aiSummaryFromTrigger = process.env.AI_SUMMARY;
const apiKey               = process.env.INTERNAL_AI_API_KEY;
const instructionsFilePath = process.env.INSTRUCTIONS_FILE || '';

// AI API configuration — defaults to OpenAI, override via env vars for other providers
const AI_API_HOST = process.env.AI_API_HOST || 'api.openai.com';
const AI_API_PATH = process.env.AI_API_PATH || '/v1/chat/completions';
const AI_MODEL    = process.env.AI_MODEL    || 'gpt-4o';

console.log(`📋 Changed files: ${changedFiles.length}`);
console.log(`🧠 AI Summary from source: ${aiSummaryFromTrigger ? 'Yes' : 'No'}`);
console.log(`🤖 AI model: ${AI_MODEL} @ ${AI_API_HOST}`);

if (!apiKey) {
  console.error('❌ ERROR: INTERNAL_AI_API_KEY is missing!');
  process.exit(1);
}

// ─── Load instructions (for folder structure spec) ────────────────────────────
let instructionsContent = '';
if (instructionsFilePath) {
  const absPath = path.join(process.cwd(), instructionsFilePath);
  if (fs.existsSync(absPath)) {
    instructionsContent = fs.readFileSync(absPath, 'utf-8');
    console.log(`📋 Instructions loaded: ${instructionsFilePath}`);
  } else {
    console.warn(`⚠️  Instructions file not found: ${absPath}`);
  }
}

// Extract the ## Folder Structure section from the instructions file (if any).
function extractFolderStructureSpec(instructions) {
  const match = instructions.match(/##\s+Folder Structure\s*\n([\s\S]*?)(?=\n##\s|\s*$)/i);
  return match ? match[1].trim() : null;
}

const folderStructureSpec = extractFolderStructureSpec(instructionsContent);

// Load default folder structure spec from the default instructions file
let defaultInstructionsContent = '';
const DEFAULT_INSTRUCTIONS_PATH = '.github/instructions/DocumentationInstructions.instructions.md';
const absDefaultPath = path.join(process.cwd(), DEFAULT_INSTRUCTIONS_PATH);
if (!folderStructureSpec && fs.existsSync(absDefaultPath)) {
  defaultInstructionsContent = fs.readFileSync(absDefaultPath, 'utf-8');
  console.log(`📋 Default instructions loaded: ${DEFAULT_INSTRUCTIONS_PATH}`);
}
const defaultFolderSpec = folderStructureSpec
  ? null
  : extractFolderStructureSpec(defaultInstructionsContent);

const activeFolderSpec = folderStructureSpec || defaultFolderSpec || null;

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
        catch (e) { reject(new Error(`Failed to parse AI response: ${e.message}\nBody: ${body.slice(0, 200)}`)); }
      });
    });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

// ─── Should-sync analysis ─────────────────────────────────────────────────────
async function analyzeWithAI(filePath, content) {
  const systemPrompt = `You are a senior technical documentation expert. Analyze the documentation file and determine if it should be synced to the central documentation repository.

CRITERIA FOR SYNCING:
1. [YES] New Features: Any new functionality, API endpoints, configuration options, or user-facing capabilities.
2. [YES] Significant Logic Changes: Changes that alter how the system behaves or how users interact with it.
3. [YES] Configuration Changes: New environment variables, settings, or deployment requirements.
4. [NO] Bug Fixes/Refactoring: Minor fixes, typos, internal refactoring without behavior change.
5. [NO] Internal Tests/CI: Changes to test files or CI configurations.
6. [NO] Code Comments: Changes only to code comments without functional changes.

Respond ONLY with valid JSON:
{"should_sync": boolean, "reason": "string", "priority": "high"|"medium"|"low"}`;

  const userPrompt = `Analyze file: ${filePath}\n\nContent Preview:\n${content.substring(0, 3000)}`;

  try {
    const response = await callAI([
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt }
    ]);
    const raw = response.choices[0].message.content;
    const clean = raw.replace(/```json\n?|```\n?/g, '').trim();
    return JSON.parse(clean);
  } catch (error) {
    console.warn(`⚠️  AI analysis failed for ${filePath}: ${error.message}`);
    return { should_sync: true, reason: `AI analysis failed: ${error.message}`, priority: 'low' };
  }
}

// ─── Folder classification ────────────────────────────────────────────────────
async function classifyFolder(filePath, content) {
  if (!apiKey) return null;

  const specSection = activeFolderSpec
    ? `The team has defined the following folder structure for this service. You MUST place each file into one of these categories (use the folder slug exactly as shown):\n\n${activeFolderSpec}`
    : `No folder structure has been defined for this service. Use documentation best practices to classify the file into one of these standard categories:
- how-to-guides       — step-by-step task instructions
- configuration-field-reference — config fields, parameters, and options
- features            — feature descriptions and capabilities
- setup-guides        — installation, deployment, onboarding
- reference           — API reference, CLI reference, data dictionaries
- concepts            — conceptual explanations, architecture overviews
- tutorials           — end-to-end learning walkthroughs
- troubleshooting     — error resolution, FAQs, debugging guides

If the file is a top-level overview or introduction for the entire service, reply with the single word: ROOT`;

  const systemPrompt = `You are a technical documentation architect. Given a documentation file, determine which subfolder it belongs in within the service documentation directory.

${specSection}

Rules:
- Reply with ONLY the exact folder slug (e.g. "how-to-guides", "configuration-field-reference").
- Use lowercase-with-hyphens. Do not invent new categories not listed above.
- If the file is a top-level service overview or index, reply with: ROOT
- Do not add any explanation.`;

  const userPrompt = `Filename: ${path.basename(filePath)}\n\nContent (first 2000 chars):\n${content.slice(0, 2000)}`;

  try {
    const response = await callAI([
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt }
    ]);
    const choice = (response.choices[0].message.content || '').trim().toLowerCase();
    if (choice === 'root' || !choice) return null;
    return choice.replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '') || null;
  } catch (err) {
    console.warn(`⚠️  Folder classification failed for ${filePath}: ${err.message}`);
    return null;
  }
}

// ─── Main ─────────────────────────────────────────────────────────────────────
async function processFiles() {
  const results = [];
  const hasSourceRepoAISummary = aiSummaryFromTrigger && aiSummaryFromTrigger.trim() !== '';

  console.log('\n🔍 Analyzing files...\n');

  for (const file of changedFiles) {
    const fullPath = path.join(process.cwd(), 'source-repo', file);

    if (!fs.existsSync(fullPath)) {
      console.log(`🗑️  ${file} - DELETED`);
      results.push({
        file,
        status: 'deleted',
        analysis: {
          should_sync: true,
          reason: 'File was removed from source repository',
          priority: 'medium',
          target_subfolder: null
        }
      });
      continue;
    }

    const content = fs.readFileSync(fullPath, 'utf-8');
    let analysis;

    if (hasSourceRepoAISummary) {
      console.log(`✅ ${file} - PRE-APPROVED by source repo`);
      analysis = {
        should_sync: true,
        reason: 'Pre-approved by source repository AI',
        priority: 'high'
      };
    } else {
      console.log(`🧠 ${file} - Analyzing...`);
      analysis = await analyzeWithAI(file, content);
      console.log(`   ${analysis.should_sync ? '✅' : '⏭️'} ${analysis.reason}`);
    }

    let target_subfolder = null;
    if (analysis.should_sync) {
      console.log(`📂 ${file} - Classifying folder...`);
      target_subfolder = await classifyFolder(file, content);
      console.log(`   📁 Target subfolder: ${target_subfolder || '(root)'}`);
    }

    analysis.target_subfolder = target_subfolder;
    results.push({ file, analysis, status: 'synced' });
  }

  const output = {
    results,
    aiGeneratedSummary: aiSummaryFromTrigger || 'Analysis completed by central repository',
    folderSpecSource: folderStructureSpec
      ? 'service-instructions'
      : (defaultFolderSpec ? 'default-instructions' : 'ai-default')
  };

  fs.writeFileSync('analysis_results.json', JSON.stringify(output, null, 2));

  console.log('\n📊 Analysis Summary:');
  console.log(`   Total files:  ${results.length}`);
  console.log(`   Should sync:  ${results.filter(r => r.analysis.should_sync).length}`);
  console.log(`   Skip:         ${results.filter(r => !r.analysis.should_sync).length}`);
  console.log(`   Folder spec:  ${output.folderSpecSource}`);
  console.log('\n💾 Results saved to analysis_results.json');
}

processFiles().catch(error => {
  console.error('❌ Fatal error:', error);
  process.exit(1);
});
