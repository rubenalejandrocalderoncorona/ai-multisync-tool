'use strict';
/**
 * FEATURE_REGISTRY: maps a registered "contract point" symbol to the other
 * repositories that must ship before the feature counts as available.
 *
 * Example (config/feature-registry.json):
 *   { "alert_channels_enum": { "owner": "org/backend", "requires": ["org/frontend"] } }
 *
 * The cross-repo check is gated by the registry: unregistered symbols cost nothing.
 */

/**
 * @returns {Promise<{ registered: string[], incomplete: {symbol:string, missing:string[]}[], complete: boolean }>}
 */
async function crossRepoCheck({ symbols, repo, registry, factstore }) {
  const registered = [];
  const incomplete = [];
  for (const symbol of symbols) {
    const entry = registry[symbol];
    if (!entry || (entry.owner && entry.owner !== repo)) continue;
    registered.push(symbol);
    const missing = [];
    for (const dep of entry.requires || []) {
      // eslint-disable-next-line no-await-in-loop
      if (!(await factstore.repoDocumentsSymbol(dep, symbol))) missing.push(dep);
    }
    if (missing.length) incomplete.push({ symbol, missing });
  }
  return { registered, incomplete, complete: incomplete.length === 0 };
}

module.exports = { crossRepoCheck };
