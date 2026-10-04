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
  constructor(aiConfig, fetchImpl = globalThis.fetch, sleep = realSleep) {
    this.cfg = aiConfig;
    this.fetch = fetchImpl;
    this.sleep = sleep;
  }

  /**
   * POST with retries on transient failures: rate limits (429), server errors and network errors.
   * A stage that loses a whole run to a one-minute rate limit is a reliability bug, not a content failure.
   */
  async _post(path, body) {
    const max = this.cfg.maxRetries ?? 5;
    for (let attempt = 0; ; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), this.cfg.timeoutMs);
      let res; let text;
      try {
        res = await this.fetch(`${this.cfg.baseUrl}${path}`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(this.cfg.apiKey ? { Authorization: `Bearer ${this.cfg.apiKey}` } : {}),
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
      throw new Error(`AI API ${res.status}: ${text.slice(0, 300)}`);
    }
  }

  /** @returns {Promise<string>} assistant message content */
  async chat(messages, { fast = false, temperature = 0 } = {}) {
    const data = await this._post(this.cfg.chatPath, {
      model: fast ? this.cfg.fastModel : this.cfg.model,
      messages,
      temperature,
    });
    return (data.choices?.[0]?.message?.content || '').trim();
  }

  async chatJson(messages, opts) {
    const raw = await this.chat(messages, opts);
    return parseJson(raw);
  }

  /** @returns {Promise<number[][]>} one vector per input, in order */
  async embed(texts) {
    if (texts.length === 0) return [];
    const data = await this._post(this.cfg.embedPath, {
      model: this.cfg.embedModel,
      input: texts,
    });
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
