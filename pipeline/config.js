'use strict';
/**
 * Central configuration. Everything is environment-driven (12-factor) so the
 * same image runs under docker compose today and Kubernetes later.
 */
const fs = require('fs');
const path = require('path');

const num = (v, d) => (v === undefined || v === '' || Number.isNaN(Number(v)) ? d : Number(v));

/**
 * Two model tiers behind one client (see pipeline/router.js for who goes where):
 *   cheap      bounded or low-risk work: GAR rewrites, template/folder routing, drafts for internal-only changes
 *   expensive  drafts that touch a public interface, and the judge
 * Embeddings are always the expensive provider's (OpenAI): DeepSeek has no embeddings API.
 * Without DEEPSEEK_API_KEY the cheap tier falls back to a small model on the primary provider, so a single key still works.
 */
function aiConfig(env, host) {
  const primaryBase = (env.AI_API_BASE_URL || `https://${host}`).replace(/\/$/, '');
  const primaryKey = env.INTERNAL_AI_API_KEY || env.AI_API_KEY || '';
  const dsKey = env.DEEPSEEK_API_KEY || '';
  const num2 = (v, d) => (v === undefined || v === '' || Number.isNaN(Number(v)) ? d : Number(v));
  const expensiveModel = env.AI_EXPENSIVE_MODEL || env.AI_MODEL || 'gpt-5.6-terra';
  const tiers = {
    expensive: {
      name: 'expensive', baseUrl: primaryBase, chatPath: env.AI_API_PATH || '/v1/chat/completions', apiKey: primaryKey, model: expensiveModel,
      // gpt-5.x rejects temperature 0 ("only the default (1) is supported"), so it is omitted for those models.
      temperature: /^gpt-5/i.test(expensiveModel) || /^o\d/i.test(expensiveModel) ? null : 0,
      priceIn: num2(env.AI_EXPENSIVE_PRICE_IN, 2.0), priceOut: num2(env.AI_EXPENSIVE_PRICE_OUT, 12.0), // USD per 1M tokens (estimate)
    },
    cheap: dsKey
      ? { name: 'cheap', baseUrl: (env.AI_CHEAP_BASE_URL || 'https://api.deepseek.com').replace(/\/$/, ''), chatPath: env.AI_CHEAP_PATH || '/chat/completions', apiKey: dsKey, model: env.AI_CHEAP_MODEL || 'deepseek-v4-pro', temperature: 0, priceIn: num2(env.AI_CHEAP_PRICE_IN, 0.66), priceOut: num2(env.AI_CHEAP_PRICE_OUT, 1.98) }
      : { name: 'cheap', baseUrl: primaryBase, chatPath: env.AI_API_PATH || '/v1/chat/completions', apiKey: primaryKey, model: env.AI_FAST_MODEL || 'gpt-4o-mini', temperature: 0, priceIn: num2(env.AI_CHEAP_PRICE_IN, 0.15), priceOut: num2(env.AI_CHEAP_PRICE_OUT, 0.6) },
  };
  return {
    apiKey: primaryKey,
    baseUrl: primaryBase,
    chatPath: tiers.expensive.chatPath,
    embedPath: env.AI_EMBED_PATH || '/v1/embeddings',
    model: expensiveModel,
    fastModel: tiers.cheap.model,
    embedModel: env.AI_EMBED_MODEL || 'text-embedding-3-small',
    embedDim: num2(env.AI_EMBED_DIM, 1536),
    timeoutMs: num2(env.AI_TIMEOUT_MS, 120000),
    maxRetries: num2(env.AI_MAX_RETRIES, 5),
    tiers,
    // Which tier judges drafts: expensive (default, accuracy) | cheap | follow (same tier as the draft)
    judgeTier: env.ROUTER_JUDGE || 'expensive',
    // auto | cheap | expensive : force every routed stage to one tier (for tests and cost experiments)
    routerForce: env.ROUTER_FORCE || 'auto',
  };
}

function loadConfig(env = process.env) {
  const host = env.AI_API_HOST || 'api.openai.com';
  return {
    ai: aiConfig(env, host),
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
      contextMinScore: num(env.CONTEXT_MIN_SCORE, 0.45), // retrieved chunks below this cosine score are dropped
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
      // The base can be cluster-internal (http://caiman-tickets.caimanlabs-operations.svc.cluster.local) for the in-cluster
      // runner; ticket links always use the public URL.
      deskPublicUrl: (env.CAIMANDESK_PUBLIC_URL || env.CAIMANDESK_URL || 'https://tickets.caimanlabs.com.mx').replace(/\/$/, ''),
      // mcp (Vikunja's built-in MCP, the default) | rest (Vikunja API). Both use the same API token.
      deskTransport: env.CAIMANDESK_TRANSPORT || 'mcp',
      deskMcpUrl: env.CAIMANDESK_MCP_URL || `${(env.CAIMANDESK_URL || 'https://tickets.caimanlabs.com.mx').replace(/\/$/, '')}/api/v2/mcp`,
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
