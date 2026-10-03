'use strict';
/** Shared wiring for the CLI scripts: builds real dependencies from the environment. */
const { loadConfig, readJson } = require('../pipeline/config');
const { LLM } = require('../pipeline/llm');
const { QdrantStore, MemoryVectorStore } = require('../pipeline/vectorstore');
const { createFactStore } = require('../pipeline/factstore');
const { indexApproved } = require('../pipeline/vectorstore');
const fs = require('fs');
const path = require('path');

function buildDeps(env = process.env) {
  const cfg = loadConfig(env);
  const llm = new LLM(cfg.ai);
  const vectors = cfg.qdrant.driver === 'memory' ? new MemoryVectorStore() : new QdrantStore(cfg.qdrant, cfg.ai.embedDim);
  const facts = createFactStore(cfg.factstore);
  return {
    cfg, llm, vectors, facts,
    reposConfig: readJson(cfg.paths.reposConfig, { defaults: {}, repos: {} }),
    registry: readJson(cfg.paths.featureRegistry, {}),
  };
}

/**
 * Side effects of a decision, shared by the CI runner and the demo:
 *   write/delete the page, index only what policy has already approved, keep rejected drafts out of the site.
 */
async function applyDecision(decision, { d, repo, file, commit }) {
  if (decision.action === 'write') {
    fs.mkdirSync(path.dirname(decision.targetPath), { recursive: true });
    fs.writeFileSync(decision.targetPath, decision.content);
  } else if (decision.action === 'delete' && fs.existsSync(decision.targetPath)) {
    fs.unlinkSync(decision.targetPath);
  }

  // Auto-trust repos are approved by policy: index now. Review repos index after the PR merges.
  if (decision.outcome === 'published') {
    if (decision.action === 'write') {
      await indexApproved({ store: d.vectors, llm: d.llm, repo, filePath: file, content: decision.content, commit });
      await d.facts.approveClaims(repo, file, commit);
    } else {
      await d.vectors.deleteByPath(repo, file);
    }
  }

  // A fallback never touches the site; keep the draft for the human who picks up the ticket.
  if (decision.outcome === 'fallback' && decision.draft) {
    fs.mkdirSync('rejected', { recursive: true });
    fs.writeFileSync(path.join('rejected', file.replace(/[\\/]/g, '__')), decision.draft);
  }
}

module.exports = { buildDeps, applyDecision };
