'use strict';
/**
 * Generation side: GAR, template selection, drafting, polish, folder classification,
 * and Starlight frontmatter. All LLM calls go through the injected `llm`.
 */
const fs = require('fs');
const path = require('path');

const P = require('./prompts');

/** GAR: describe the change as a hypothetical doc paragraph; its embedding drives retrieval. Never indexed. */
async function generateHypothetical(llm, { filePath, before, after, mode = 'docs', changedFiles = [] }) {
  const user = mode === 'code'
    ? `Changed files: ${changedFiles.join(', ') || '(unknown)'}\n\nCODE AFTER:\n${after.slice(0, 6000)}`
    : `File: ${filePath}\n\nBEFORE:\n${(before || '(new file)').slice(0, 2500)}\n\nAFTER:\n${after.slice(0, 2500)}`;
  return llm.chat([{ role: 'system', content: P.loadPrompt('gar').text }, { role: 'user', content: user }], { fast: true });
}

/**
 * GAR from the fact sheet: several hypothetical documentation paragraphs (one per topic) written AFTER the
 * code analysis, so they describe what the docs should say about verified facts rather than guess from raw code.
 * Each paragraph becomes one retrieval query against the documentation index. Never published, never indexed.
 * A failure here only degrades retrieval, so it returns [] instead of failing the run.
 */
async function generateGarFromFacts(llm, { sheet, brief }) {
  const prompt = P.loadPrompt('gar-facts');
  try {
    const r = await llm.chatJson([
      { role: 'system', content: prompt.text },
      { role: 'user', content: `FACT_SHEET:\n${JSON.stringify(sheet.facts.map((f) => ({ id: f.id, text: f.text, kind: f.kind, status: f.status })), null, 1)}\n\nPAGE_BRIEF:\n${brief || '(none)'}` },
    ], { fast: true });
    const paragraphs = (Array.isArray(r.paragraphs) ? r.paragraphs : []).filter((x) => typeof x === 'string' && x.trim()).slice(0, 4);
    return { paragraphs, promptId: prompt.id };
  } catch (e) {
    return { paragraphs: [], promptId: prompt.id, error: e.message };
  }
}

function findTemplateFiles(dir) {
  const out = [];
  if (!fs.existsSync(dir)) return out;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...findTemplateFiles(p));
    else if (/\.(md|mdx)$/.test(e.name)) out.push(p);
  }
  return out;
}

async function selectTemplate(llm, { filePath, content, templateFiles, defaultTemplate }) {
  const candidates = templateFiles.filter((f) => !f.endsWith('default-template.md'));
  if (!candidates.length) return defaultTemplate;
  const catalogue = candidates.map((f) => {
    const first = fs.readFileSync(f, 'utf-8').replace(/^---[\s\S]*?---\n+/, '').split('\n').find((l) => l.trim()) || '';
    return `- ${f}: ${first.replace(/^#+\s*/, '')}`;
  }).join('\n');
  const choice = await llm.chat([
    { role: 'system', content: `Pick the single best template for the document.\n${catalogue}\nReply ONLY with the exact path, or DEFAULT.` },
    { role: 'user', content: `Filename: ${path.basename(filePath)}\n\n${content.slice(0, 3000)}` },
  ], { fast: true });
  return candidates.find((f) => f === choice || f.endsWith(choice.replace(/^\.\//, ''))) || defaultTemplate;
}

/**
 * Produce a draft. `feedback` carries the judge's findings on retry.
 * mode 'docs': source is a documentation file. mode 'code': source is a code snapshot and `existing` is the live page.
 */
async function draftDocument(llm, { mode = 'docs', filePath, source, existing = '', changedFiles = [], relatedCode = '', factSheet = '', plan = '', templatePath, context, policy, style, instructions, feedback }) {
  const template = fs.existsSync(templatePath) ? fs.readFileSync(templatePath, 'utf-8') : '';
  const ctx = context.map((c) => `[${c.heading}] ${c.text.slice(0, 600)}`).join('\n---\n');
  const fix = feedback?.length ? `\n\nA reviewer rejected the previous attempt. Fix exactly these problems:\n- ${feedback.join('\n- ')}` : '';
  const prompt = P.loadPrompt(mode === 'code' ? 'draft-code' : 'draft-docs');
  const system = [
    P.fill(prompt.text, { PERSONA: style?.prompt || 'You are a senior technical writer.' }),
    instructions && `Documentation standards:\n${instructions}`,
    P.styleText({ key: null, rubric: [] }, policy),
  ].filter(Boolean).join('\n\n');
  const user = mode === 'code'
    ? `TEMPLATE:\n${template}\n\nCONTEXT:\n${ctx || '(none)'}\n\nEXISTING_PAGE:\n${existing || '(none: write a new page)'}\n\nCHANGED_FILES: ${changedFiles.join(', ')}\n\nFACT_SHEET:\n${factSheet || '(none)'}\n\nPLAN:\n${plan || '(none)'}\n\nCODE:\n${source}\n\nRELATED_CODE:\n${relatedCode || '(none)'}${fix}`
    : `TEMPLATE:\n${template}\n\nCONTEXT:\n${ctx || '(none)'}\n\nSOURCE (${path.basename(filePath)}):\n${source}${fix}`;
  const out = await llm.chat([{ role: 'system', content: system }, { role: 'user', content: user }]);
  return { text: out.length > 50 ? out : (mode === 'code' ? existing || out : source), promptId: prompt.id };
}

/** Polish-only rewrite; forbidden from touching facts (failure mode #8). */
async function polishOnly(llm, { draft, policy, style, instructions }) {
  const prompt = P.loadPrompt('polish');
  const system = P.fill(prompt.text, { STYLE: [P.styleText(style || { rubric: [] }, policy), instructions].filter(Boolean).join('\n\n') });
  const out = await llm.chat([{ role: 'system', content: system }, { role: 'user', content: draft }]);
  return out.length > 50 ? out : draft;
}

async function classifyFolder(llm, { filePath, content, folderSpec }) {
  const spec = folderSpec
    ? `Use exactly one folder slug from:\n${folderSpec}`
    : 'Use one of: how-to-guides, configuration-field-reference, features, setup-guides, reference, concepts, tutorials, troubleshooting. Reply ROOT for a top-level overview.';
  const choice = (await llm.chat([
    { role: 'system', content: `Classify the document into a docs subfolder. ${spec}\nReply with ONLY the slug.` },
    { role: 'user', content: `Filename: ${path.basename(filePath)}\n\n${content.slice(0, 2000)}` },
  ], { fast: true })).toLowerCase();
  if (!choice || choice === 'root') return null;
  return choice.replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '') || null;
}

function extractFolderSpec(instructions) {
  const m = (instructions || '').match(/##\s+Folder Structure\s*\n([\s\S]*?)(?=\n##\s|\s*$)/i);
  return m ? m[1].trim() : null;
}

function titleFrom(content, filePath) {
  const h = content.match(/^#{1,2}\s+(.+)$/m);
  if (h) return h[1].replace(/[`*_]/g, '').trim();
  return path.basename(filePath, path.extname(filePath)).replace(/[_-]/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

function descriptionFrom(content) {
  const para = content.replace(/^#.*$/gm, '').split(/\n{2,}/).map((p) => p.trim())
    .find((p) => p && !/^[|`>:\-*\d]/.test(p)) || '';
  return para.replace(/\s+/g, ' ').slice(0, 160).replace(/"/g, '\\"');
}

/** Starlight-compatible frontmatter (title + description are what its schema expects). */
function withFrontmatter(content, { filePath, sourceUrl, commit, docKey, title: titleOverride, description: descOverride }) {
  const body = content.replace(/^---\n[\s\S]*?\n---\n+/, '');
  const title = (titleOverride || titleFrom(body, filePath)).replace(/"/g, '\\"');
  // Starlight renders `title` as the page H1; drop a duplicate leading H1.
  const cleaned = body.replace(/^#\s+.+\n+/, '');
  return `---
title: "${title}"
description: "${descOverride ? descOverride.replace(/\s+/g, ' ').slice(0, 160).replace(/"/g, '\\"') : descriptionFrom(cleaned)}"
source: ${sourceUrl}
doc_key: ${docKey || filePath}
commit: ${commit}
last_synced: ${new Date().toISOString()}
automated: true
---

${cleaned}`;
}

/**
 * The Change History table is generated by code, never by the model: a model asked for a date invents one.
 * Any table the model wrote is removed first.
 */
function withChangeHistory(content, { repo, commit, gaps = [], now = new Date() }) {
  const stripped = content.replace(/\n#{2,3}\s+Change History\b[\s\S]*?(?=\n#{2,3}\s+(?!Change History)\S|$)/i, '').replace(/\s+$/, '');
  const completeness = gaps.length ? `Partially filled: the source does not show ${gaps.slice(0, 3).join('; ').replace(/\|/g, '/')}` : 'Fully filled';
  const row = `| ${now.toISOString().slice(0, 10)} | ${commit.slice(0, 7)} | Documentation Bot | Generated from ${repo} at ${commit.slice(0, 7)} | ${completeness} |`;
  return `${stripped}\n\n## Change History\n\n| Date | Version | Author | Change Description | Completeness |\n|------|---------|--------|--------------------|--------------|\n${row}\n`;
}

module.exports = {
  generateGarFromFacts,
  withChangeHistory,
  generateHypothetical, findTemplateFiles, selectTemplate, draftDocument, polishOnly,
  classifyFolder, extractFolderSpec, withFrontmatter, titleFrom,
};
