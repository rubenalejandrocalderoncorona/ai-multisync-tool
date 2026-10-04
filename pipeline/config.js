'use strict';
/**
 * Central configuration. Everything is environment-driven (12-factor) so the
 * same image runs under docker compose today and Kubernetes later.
 */
const fs = require('fs');
const path = require('path');

const num = (v, d) => (v === undefined || v === '' || Number.isNaN(Number(v)) ? d : Number(v));

function loadConfig(env = process.env) {
  const host = env.AI_API_HOST || 'api.openai.com';
  return {
    ai: {
      apiKey: env.INTERNAL_AI_API_KEY || env.AI_API_KEY || '',
      // AI_API_BASE_URL wins (e.g. http://ollama:11434); otherwise https://<host>
      baseUrl: (env.AI_API_BASE_URL || `https://${host}`).replace(/\/$/, ''),
      chatPath: env.AI_API_PATH || '/v1/chat/completions',
      embedPath: env.AI_EMBED_PATH || '/v1/embeddings',
      model: env.AI_MODEL || 'gpt-4o',
      // A cheaper model for GAR / claim extraction; falls back to the main model.
      fastModel: env.AI_FAST_MODEL || env.AI_MODEL || 'gpt-4o-mini',
      embedModel: env.AI_EMBED_MODEL || 'text-embedding-3-small',
      embedDim: num(env.AI_EMBED_DIM, 1536),
      timeoutMs: num(env.AI_TIMEOUT_MS, 120000),
      maxRetries: num(env.AI_MAX_RETRIES, 5),
    },
    qdrant: {
      // qdrant | memory (memory: local rehearsal and tests only; nothing persists)
      driver: env.VECTOR_DRIVER || 'qdrant',
      url: (env.QDRANT_URL || 'http://localhost:6333').replace(/\/$/, ''),
      apiKey: env.QDRANT_API_KEY || '',
      collection: env.QDRANT_COLLECTION || 'docs_chunks',          // semantic context: approved pages, source docs, page briefs
      codeCollection: env.QDRANT_CODE_COLLECTION || 'code_context', // code context: every in-scope source file, chunked
    },
    factstore: {
      // postgres | memory (memory is for tests and the no-infra demo only)
      driver: env.FACTSTORE_DRIVER || (env.FACTSTORE_DATABASE_URL ? 'postgres' : 'memory'),
      databaseUrl: env.FACTSTORE_DATABASE_URL || '',
    },
    thresholds: {
      minDiffLines: num(env.MIN_DIFF_LINES, 3),
      similarityHigh: num(env.SIMILARITY_HIGH, 0.92),
      precisionMin: num(env.PRECISION_MIN, 0.9),
      recallMin: num(env.RECALL_MIN, 0.85),
      coreRecallMin: num(env.CORE_RECALL_MIN, 1),
      styleMin: num(env.STYLE_MIN, 0.7),
      judgeMin: num(env.JUDGE_MIN, 0.75),
      maxIterations: num(env.MAX_ITERATIONS, 3),
      topK: num(env.TOP_K, 5),
      topKWidened: num(env.TOP_K_WIDENED, 12),
      codeTopK: num(env.CODE_TOP_K, 12),
      codeTopKWidened: num(env.CODE_TOP_K_WIDENED, 30),
      contextBudgetChars: num(env.CONTEXT_BUDGET_CHARS, 30000),
    },
    paths: {
      reposConfig: env.REPOS_CONFIG || 'config/repos.json',
      featureRegistry: env.FEATURE_REGISTRY || 'config/feature-registry.json',
      docsRoot: env.DOCS_ROOT || 'src/content/docs',
      instructions: env.INSTRUCTIONS_FILE || '.github/instructions/DocumentationInstructions.instructions.md',
      templates: env.TEMPLATES_PATH || 'docs/templates',
      styles: env.DOC_STYLES || '',
    },
    alerts: {
      ticketProvider: env.TICKET_PROVIDER || 'caimandesk', // caimandesk | github | jira | none
      deskBaseUrl: (env.CAIMANDESK_URL || 'https://tickets.caimanlabs.com.mx').replace(/\/$/, ''),
      deskToken: env.CAIMANDESK_API_TOKEN || '',
      deskProjectId: env.CAIMANDESK_PROJECT_ID || '',
      slackWebhook: env.SLACK_WEBHOOK_URL || '',
      githubToken: env.GITHUB_TOKEN || env.DOCS_SYNC_PAT || '',
      githubRepo: env.GITHUB_REPOSITORY || '',
      jiraBaseUrl: env.JIRA_BASE_URL || '',
      jiraEmail: env.JIRA_EMAIL || '',
      jiraToken: env.JIRA_API_TOKEN || '',
      jiraProject: env.JIRA_PROJECT_KEY || '',
    },
  };
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(path.resolve(file), 'utf-8'));
  } catch {
    return fallback;
  }
}

/**
 * Per-repo policy: trust level (auto|review), docs folder, style guide and glossary.
 * Unknown repos get the safest defaults (review, no auto-publish).
 */
function repoPolicy(reposConfig, repoFullName) {
  const defaults = reposConfig.defaults || {};
  const specific = (reposConfig.repos || {})[repoFullName] || {};
  return {
    trust: 'review',
    serviceName: repoFullName.split('/').pop(),
    styleGuide: '',
    glossary: {},
    ...defaults,
    ...specific,
  };
}

module.exports = { loadConfig, repoPolicy, readJson };
