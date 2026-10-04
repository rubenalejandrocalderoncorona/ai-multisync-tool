'use strict';
const { structuralChange, diffLineCount } = require('./structure');
const { diffPublicSymbols } = require('./symbols');

/**
 * Layer 1 of the cheap-to-expensive cascade. Returns a verdict without
 * touching the network.
 *
 * @returns {{ proceed: boolean, forced: boolean, reason: string, tag?: string, metrics: object }}
 */
function prefilter({ before, after, minDiffLines }) {
  const diff = diffLineCount(before, after);
  if (diff.total < minDiffLines) {
    return { proceed: false, forced: false, reason: `diff of ${diff.total} line(s) is below the ${minDiffLines}-line minimum`, tag: 'trivial_diff', metrics: { diff } };
  }
  const structure = structuralChange(before, after);
  // A changed public signature, route, schema field or config key is a real change even when no function was added
  // or removed (the structural check only sees names and counts).
  const pub = diffPublicSymbols(before, after);
  if (pub.touched && !structure.changed) {
    return { proceed: true, forced: true, reason: `public interface changed: ${pub.names.slice(0, 5).join(', ')}${pub.names.length > 5 ? ', ...' : ''}`, metrics: { diff, structure, publicInterface: pub.names.length } };
  }
  if (!structure.changed) {
    return { proceed: false, forced: false, reason: 'no structural or fact-bearing change (wording only)', tag: 'no_structural_change', metrics: { diff, structure } };
  }
  return {
    proceed: true,
    forced: structure.forced,
    reason: structure.forced ? `shape changed: ${structure.reasons.join(', ')}` : `fact tokens changed: ${structure.reasons.join(', ')}`,
    metrics: { diff, structure },
  };
}

module.exports = { prefilter };
