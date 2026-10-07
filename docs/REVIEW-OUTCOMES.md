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


# Audit sampling: is "unedited" the same as "correct"?

`draft_with_noedition` only means the reviewer changed nothing. A rubber-stamped merge looks identical to a carefully verified one, so as trust in the
pipeline grows, review rigor can drop while the metric above keeps looking fine. Audit sampling measures that gap. **Detection and reporting only:** no
approval, routing or auto-pass logic reads any of this.

**Sampling.** Right after a `draft_with_noedition` row is logged, it is sampled with probability `AUDIT_SAMPLE_RATE` (default `0.10`, set in the
`multisync-config` ConfigMap; clamped to 0 to 1). Edited and rejected drafts are never sampled, and a redelivered webhook cannot sample twice. A sampled
row gets `audit_sampled = true`; the rows with no verdict yet are the **audit queue**. When ticketing is configured, each sampled row also opens a
`[docs-audit] #<id> <repo>@<sha> <page>` task in cAImanDesk.

**The second reviewer.** The auditor reads the published page and confirms its factual statements against the code at the source commit, independently.
To avoid anchoring, neither the queue nor the ticket shows the first reviewer, the outcome, the scores or the pull request.

```bash
multisync audit list                         # open audits: id, page, repo and commit only
multisync audit show 12                      # the page and the code commit to check it against
multisync audit submit 12 --reviewer <login> --accurate yes|no --notes "route /x is POST, not GET"
```
A submission by the original reviewer (any capitalization) is refused, by the CLI and by a database constraint (`audit_reviewer` must differ from
`reviewed_by`). An audit is final, and only a sampled row can have one. New columns: `audit_sampled`, `audit_reviewer`, `audit_verified_accurate`,
`audit_notes`, `audited_at`.

**The gap report.**

```bash
multisync metrics audit-gap --segment public_interface:expensive            # --max-gap 10  --min-audits 5  --policy-version ...  --json
```
It reports, for the segment, the raw no-edit rate (the same counts as `review-readiness`), how many audits are sampled, finished and pending, the share
of finished audits confirmed accurate (with a 95% Wilson interval), and the gap in percentage points. It **warns when the audited accuracy is more than
`--max-gap` points (default 10, `AUDIT_MAX_GAP_PP`) below the raw rate** (exit code 1): that gap is the rubber-stamp signal.

Two choices to know about:
- `pct_confirmed_accurate` is computed over audits **that have a verdict**. A sampled row nobody has audited yet is unknown, not inaccurate, so it is shown
  as pending instead of dragging the percentage down.
- A warning needs at least `--min-audits` verdicts (default 5, `AUDIT_MIN_SAMPLES`); with two audits a single miss is a 50-point swing by chance. Below
  that the report says how many more are needed instead of raising an alarm.


# Graduation alert and drift alert

Two **alerts**, nothing more. Both are sent by `record_review` (when a docs PR is closed) and the drift one also by `multisync audit submit`. They are
remembered in the table `segment_alerts` (`kind` graduation|drift, segment, `policy_version`, `payload`; unique per kind, segment and policy version), so each fires
**once**: the marker is inserted first (`ON CONFLICT DO NOTHING`) and only the writer whose insert happened notifies, so a redelivered webhook or two Jobs
at once cannot double-send. A failing Slack or ticket is logged and does not remove the marker. Nothing here can fail the Job.

**Graduation alert.** For each segment (`diff_classification:model_tier_used`) and policy version of the rows just recorded: `n >= REVIEW_READINESS_MIN_SAMPLES`
(30) and Wilson lower bound `>= REVIEW_READINESS_THRESHOLD` (0.85), the same `readiness` as `metrics review-readiness`. Sent to Slack (`SLACK_WEBHOOK_URL`) and as
one INFORMATIONAL cAImanDesk ticket (`[docs-info] Segment graduation: ...`, lowest priority) with the counts, interval, thresholds, policy version, first
and last `reviewed_at`, reviewers and the PR links. The text says a human *may consider* enabling auto-approval; nothing is enabled.
Demo/synthetic data never pages anyone: segments with a `demo-` policy version or a `synthetic://` PR URL are skipped unless `GRADUATION_ALERT_SYNTHETIC=1`.

**Audit spot-check.** The sampling draw uses the operating system's CSPRNG (`secrets.SystemRandom`). When a draft is newly sampled, the PR gets one comment
(once per PR): "Audit Spot-Check, N% Quality Sample, a second reviewer must confirm this page against the code" (N from `AUDIT_SAMPLE_RATE`), and Slack gets a line
only if a webhook is configured. Edited and rejected drafts are never sampled.

**Drift alert.** After a verdict is submitted, and after `record_review` writes rows, if the segment has at least `AUDIT_MIN_SAMPLES` (5) audits with a verdict
and the audited accuracy (confirmed accurate / audited, `pct_confirmed_accurate` of `audit_gap`) is **below `AUDIT_ACCURACY_ALERT` (0.80)**, one Slack alert
"Drift Alert" is sent that recommends suspending any auto-approval for the segment until a human re-validates. It relates to the audit-gap warning as follows:
the gap warning compares audited accuracy with the *raw no-edit rate* in percentage points (`AUDIT_MAX_GAP_PP`, default 10), so it flags rubber-stamping even when
accuracy is still high; the drift alert looks at the audited accuracy *alone* against an absolute floor. Same inputs, same minimum number of audits, two views.

**What is NOT automated.** No approval, merge or pass is ever decided by these alerts. The approval flag column is never read or written by them (a test
enforces this in the new code), nothing is enabled when a segment graduates, nothing is suspended when drift fires, and the CLI stays read-only. A human reads
the alert and decides. If no Slack webhook or ticketing is configured, the marker is still written and the alert is only logged.

# Replaced drafts

A repository that changes often (this tool is one) would otherwise pile up one open review pull request per push. When a sync opens a new review
pull request, the job closes the older open docs-sync pull requests of **the same source repository** that the new one replaces
(`multisync.cli.supersede`). A pull request is closed only if the new one contains every page it contained, so no page is silently dropped; any other
stays open. Each closure adds a comment ("Superseded by #N") and a hidden marker to the old body, and the review-outcome log ignores marked pull
requests: a draft that was replaced says nothing about its quality, so it is never counted as `draft_rejected`.
