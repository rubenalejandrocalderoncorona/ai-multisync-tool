'use strict';
const { structuralChange, diffLineCount } = require('./structure');

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
