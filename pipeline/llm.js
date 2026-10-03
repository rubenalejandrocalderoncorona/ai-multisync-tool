'use strict';
/**
 * Minimal OpenAI-compatible client (chat + embeddings) on built-in fetch.
 * Works with OpenAI, Azure-style gateways, vLLM, Ollama (/v1), etc.
 */

class LLM {
  constructor(aiConfig, fetchImpl = globalThis.fetch) {
    this.cfg = aiConfig;
    this.fetch = fetchImpl;
  }

  async _post(path, body) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.cfg.timeoutMs);
    try {
      const res = await this.fetch(`${this.cfg.baseUrl}${path}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(this.cfg.apiKey ? { Authorization: `Bearer ${this.cfg.apiKey}` } : {}),
        },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      const text = await res.text();
      if (!res.ok) throw new Error(`AI API ${res.status}: ${text.slice(0, 300)}`);
      return JSON.parse(text);
    } finally {
      clearTimeout(timer);
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

module.exports = { LLM, parseJson, cosine };
