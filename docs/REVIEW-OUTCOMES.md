# Review outcomes and the readiness report

Every generated draft is reviewed by a human through a pull request. This records what the reviewer did, so that one day auto-approval can be
considered for a segment that has proven reliable. **Nothing auto-approves.** The table is written and read for reporting only; no approval or
routing code reads it.

## What is logged

When a docs-sync pull request is closed, one row per reviewed draft (one per page) is written to `multisync.review_outcomes`:

| Column | Meaning |
|---|---|
| `change_unit_id` | `<source repo>@<source commit>:<page path>` |
| `repo` | the source repository |
| `diff_classification` | `public_interface` (a public signature, route, schema field or env var changed, a cross-repo contract point is involved, or other repos document the symbol) or `internal` |
| `model_tier_used` | `cheap` or `expensive`: the tier of the draft that went to review (after any escalation) |
| `similarity_score` | the lowest chunk similarity to the existing page |
| `judge_score_precision`, `_recall`, `_style`, `_quality` | the judge's scores for the accepted draft |
| `symbol_coverage_pct` | share of the required public symbols the draft names (null when none were required) |
| `outcome` | `draft_with_noedition` (merged, byte-identical to what the pipeline generated), `draft_with_edition` (merged, but a reviewer changed the page first), `draft_rejected` (closed without merging) |
| `reviewed_by`, `reviewed_at` | who closed or merged the pull request, and when |
| `policy_version` | `v1-<hash>` of the thresholds, router and judge settings and model names the draft was produced under |
| `auto_approval_eligible` | always `false` |
| `pr_url` | the pull request; `(pr_url, change_unit_id)` is unique, so a redelivered webhook cannot add a second row |

The features are computed during the run and stored in the decision (`decisions.metrics`); the closing job finds them through the pull request body
(source repo, commit, pages). "Edited" means the page file at the pull request's tip differs from the page at its first commit (the pipeline's).

## Where the hook is

The receiver already starts an `index` Job when a docs-sync pull request is merged into `qa`. That Job now ends with `record_review` (logging only; a
failure is printed and ignored, and runs after the indexing, which is unchanged). A docs-sync pull request closed **without** merging used to be
ignored by the receiver; it now starts a `review` Job whose only task is the same logging. Everything else about approval, indexing, deploys and tickets
is untouched.

## The readiness report (read-only)

```bash
multisync metrics review-readiness --segment public_interface:expensive
multisync metrics review-readiness --segment internal:cheap --threshold 0.9 --min-samples 50 --policy-version v1-1a2b3c4d
```

For the segment it reports the counts, the no-edit rate, the **Wilson score interval** (95% by default, `--confidence` sets z), and whether the
**lower bound** of the no-edit rate clears the threshold (default 0.85, `REVIEW_READINESS_THRESHOLD`) with at least the minimum samples (default 30,
`REVIEW_READINESS_MIN_SAMPLES`). Exit code 0 = ready, 1 = not ready, 2 = usage. `--json` for machines. Outcomes from different policy versions are
mixed unless `--policy-version` is given; compare like with like.

The command needs a database connection (`FACTSTORE_DATABASE_URL`; on the VPS through an ssh tunnel to `postgres-0`). Installed with `pip install -e .`
it is `multisync`; without installing, `python -m multisync.cli.main metrics review-readiness ...`.
