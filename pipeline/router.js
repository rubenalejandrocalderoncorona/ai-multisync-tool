'use strict';
/**
 * Model router. Decides, BEFORE any model call and without one, which tier drafts a change.
 *
 *   cheap      the change touches only internals (a rename, a log message, a comment, a function body, a private helper)
 *   expensive  the change touches a public interface: an exported signature, a route or RPC procedure, a schema model,
 *              an environment variable or config key, a symbol in the cross-repo registry, or a symbol that other
 *              repos' documents mention (the code -> docs coupling kept in the FactStore)
 *
 * Why this signal: wrong documentation of a public contract costs far more than wrong documentation of an internal
 * detail, and the same coupling data is what retrieval needs anyway, so no second classifier is built.
 *
 * Not routed here: GAR rewrites and template/folder routing (always cheap), retrieval (embeddings and cosine only,
 * no LLM at all), and the judge (its own setting).
 */
const { diffPublicSymbols, publicSymbols } = require('./symbols');

/**
 * @param {object} a
 * @param {object} a.change     { kind: 'docs'|'code', repo, filePath, before, after, changedFiles }
 * @param {object} a.registry   cross-repo feature registry (symbol -> { owner, requires })
 * @param {object} [a.facts]    FactStore (docRefs), optional: without it the coupling signal is skipped
 * @param {string} [a.force]    'cheap' | 'expensive' | 'auto'
 * @returns {Promise<{ tier: 'cheap'|'expensive', reasons: string[], signals: object }>}
 */
async function routeChange({ change, registry = {}, facts, force = 'auto' }) {
  const signals = { publicChanged: [], total: 0, registry: [], referencedBy: 0 };
  const reasons = [];

  if (force === 'cheap' || force === 'expensive') return { tier: force, reasons: [`forced by ROUTER_FORCE=${force}`], signals };

  const isCode = change.kind === 'code';
  const after = change.after || '';

  // 1. Cross-repo registry: a registered contract point appears in the change.
  signals.registry = Object.keys(registry).filter((sym) => after.includes(sym));
  if (signals.registry.length) reasons.push(`cross-repo contract point: ${signals.registry.slice(0, 4).join(', ')}`);

  if (isCode) {
    // 2. The public interface, before vs after.
    const d = diffPublicSymbols(change.before || '', after);
    signals.total = d.total;
    signals.publicChanged = d.names;
    if (!change.before) {
      // A page written from scratch (first run, forced full sync): everything public in scope is new.
      if (d.total > 0) reasons.push(`new page over ${d.total} public symbol(s)`);
    } else if (d.touched) {
      const parts = [];
      if (d.added.length) parts.push(`${d.added.length} added`);
      if (d.removed.length) parts.push(`${d.removed.length} removed`);
      if (d.changed.length) parts.push(`${d.changed.length} changed`);
      reasons.push(`public interface touched (${parts.join(', ')}): ${d.names.slice(0, 5).join(', ')}${d.names.length > 5 ? ', ...' : ''}`);
    }

    // 3. Coupling: do other repos' documents (or the docs site) mention what changed?
    if (facts?.docRefs && d.names.length && change.before) {
      const refs = await facts.docRefs(d.names.slice(0, 200), { excludeRepo: change.repo });
      signals.referencedBy = new Set(refs.map((r) => `${r.doc_repo}:${r.doc_path}`)).size;
      if (refs.length) reasons.push(`referenced by ${signals.referencedBy} document(s) elsewhere: ${[...new Set(refs.map((r) => r.symbol))].slice(0, 4).join(', ')}`);
    }
  }

  if (reasons.length) return { tier: 'expensive', reasons, signals };
  return { tier: 'cheap', reasons: [isCode ? 'internals only: no public interface changed' : 'documentation restructure with no cross-repo contract'], signals };
}

module.exports = { routeChange };
