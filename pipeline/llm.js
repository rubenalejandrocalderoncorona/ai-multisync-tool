'use strict';
/**
 * Minimal OpenAI-compatible client (chat + embeddings) on built-in fetch.
 * Works with OpenAI, Azure-style gateways, vLLM, Ollama (/v1), etc.
 */

const RETRYABLE = new Set([408, 409, 425, 429, 500, 502, 503, 504]);

/** Seconds to wait: the server's own hint (Retry-After header or "try again in 25.8s" in the body), else exponential backoff. */
function retryDelayMs(res, bodyText, attempt) {
  const header = Number(res?.headers?.get?.('retry-after'));
  if (Number.isFinite(header) && header > 0) return Math.min(header * 1000, 90_000);
  const m = String(bodyText || '').match(/try again in ([\d.]+)\s*(ms|s)\b/i);
  if (m) return Math.min((m[2].toLowerCase() === 'ms' ? Number(m[1]) : Number(m[1]) * 1000) + 750, 90_000);
  return Math.min(1000 * 2 ** attempt, 30_000);
}

const realSleep = (ms) => new Promise((r) => setTimeout(r, ms));

class LLM {
  /**
   * @param {object} aiConfig  from loadConfig().ai. `tiers.{cheap,expensive}` pick the endpoint, key, model and
   *                           temperature per call. A flat config (no tiers) behaves as before: one provider.
   */
  constructor(aiConfig, fetchImpl = globalThis.fetch, sleep = realSleep) {
    this.cfg = aiConfig;
    this.fetch = fetchImpl;
    this.sleep = sleep;
    const flat = (model) => ({ baseUrl: aiConfig.baseUrl, chatPath: aiConfig.chatPath, apiKey: aiConfig.apiKey, model, temperature: 0, priceIn: 0, priceOut: 0 });
    this.tiers = aiConfig.tiers || { expensive: flat(aiConfig.model), cheap: flat(aiConfig.fastModel || aiConfig.model) };
    // Running totals per tier, so every stage can report what it cost.
    this.usage = { cheap: { calls: 0, in: 0, out: 0, usd: 0 }, expensive: { calls: 0, in: 0, out: 0, usd: 0 }, embed: { calls: 0, tokens: 0 } };
  }

  /** Copy of the running totals; subtract two snapshots to get one stage's spend. */
  snapshotUsage() { return JSON.parse(JSON.stringify(this.usage)); }

  /**
   * POST with retries on transient failures: rate limits (429), server errors and network errors.
   * A stage that loses a whole run to a one-minute rate limit is a reliability bug, not a content failure.
   */
  async _post(path, body, endpoint = this.cfg) {
    const max = this.cfg.maxRetries ?? 5;
    for (let attempt = 0; ; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), this.cfg.timeoutMs);
      let res; let text;
      try {
        res = await this.fetch(`${endpoint.baseUrl}${path}`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(endpoint.apiKey ? { Authorization: `Bearer ${endpoint.apiKey}` } : {}),
          },
          body: JSON.stringify(body),
          signal: ctrl.signal,
        });
        text = await res.text();
      } catch (err) {
        // network error / timeout: retry, the request never reached a verdict
        if (attempt >= max) throw err;
        const wait = retryDelayMs(null, '', attempt);
        console.warn(`llm: ${err.message}; retry ${attempt + 1}/${max} in ${(wait / 1000).toFixed(1)}s`);
        await this.sleep(wait);
        continue;
      } finally {
        clearTimeout(timer);
      }
      if (res.ok) return JSON.parse(text);
      if (RETRYABLE.has(res.status) && attempt < max) {
        const wait = retryDelayMs(res, text, attempt);
        console.warn(`llm: HTTP ${res.status}; retry ${attempt + 1}/${max} in ${(wait / 1000).toFixed(1)}s`);
        await this.sleep(wait);
        continue;
      }
      throw new Error(`AI API ${res.status} (${endpoint.model || 'embeddings'}): ${text.slice(0, 300)}`);
    }
  }

  /** Which tier a call uses. `fast: true` is the historic spelling of tier "cheap". */
  tierOf(opts = {}) { return opts.tier === 'cheap' || opts.fast ? 'cheap' : 'expensive'; }

  /** @returns {Promise<string>} assistant message content */
  async chat(messages, opts = {}) {
    const tier = this.tierOf(opts);
    const ep = this.tiers[tier];
    const body = { model: ep.model, messages };
    const temperature = opts.temperature !== undefined ? opts.temperature : ep.temperature;
    if (temperature !== null && temperature !== undefined) body.temperature = temperature; // some models reject any explicit value
    const data = await this._post(ep.chatPath, body, ep);
    const u = data.usage || {};
    const t = this.usage[tier];
    t.calls += 1; t.in += u.prompt_tokens || 0; t.out += u.completion_tokens || 0;
    t.usd += ((u.prompt_tokens || 0) * (ep.priceIn || 0) + (u.completion_tokens || 0) * (ep.priceOut || 0)) / 1e6;
    return (data.choices?.[0]?.message?.content || '').trim();
  }

  async chatJson(messages, opts) {
    const raw = await this.chat(messages, opts);
    return parseJson(raw);
  }

  /** @returns {Promise<number[][]>} one vector per input, in order. Always the primary (OpenAI) endpoint. */
  async embed(texts) {
    if (texts.length === 0) return [];
    const data = await this._post(this.cfg.embedPath, { model: this.cfg.embedModel, input: texts }, { ...this.cfg, model: null });
    this.usage.embed.calls += 1; this.usage.embed.tokens += data.usage?.total_tokens || 0;
    return data.data.sort((a, b) => a.index - b.index).map((d) => d.embedding);
  }
}

function parseJson(raw) {
  const clean = raw.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '').trim();
  try {
    return JSON.parse(clean);
  } catch {
    const m = clean.match(/\{[\s\S]*\}/);
    if (m) return JSON.parse(m[0]);
    throw new Error(`Model did not return JSON: ${raw.slice(0, 200)}`);
  }
}

function cosine(a, b) {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return na && nb ? dot / (Math.sqrt(na) * Math.sqrt(nb)) : 0;
}

module.exports = { LLM, parseJson, cosine, retryDelayMs };
