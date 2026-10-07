# Draft evaluations (hallucination scores in Phoenix)

A nightly job scores every page draft the pipeline wrote in the last 24 hours and attaches the score to the draft's span in Phoenix, so a low
score is visible next to the trace that produced it. It is **decoupled from generation**: it runs hours after the runs, only *reads* spans from
Phoenix and only *writes* evaluations back. It never blocks, retries or changes a run, and touches no repo, Qdrant or FactStore table.

## How it works

1. **The `draft` span.** `write_draft` opens a span named `draft` (under `node:write_draft`) around the model call that writes the page. One per attempt. It carries:

   | attribute | content |
   |---|---|
   | `input.value` | `## Fact sheet` + `## Doc plan` the draft was asked to follow |
   | `output.value` | the draft text |
   | `retrieval.documents.<i>.document.content / .id / .score` | the retrieved chunks the draft was written from (up to 12, 1500 characters each) |
   | `multisync.change_unit_id`, `.repo`, `.page`, `.commit` | which change unit it is: `<repo>@<commit>:<page>`, the same id `review_outcomes` uses |
   | `multisync.attempt`, `.tier`, `.mode` | which attempt, which model tier, docs or code mode |

   Long values are cut (12,000 characters for text, with a note saying how much was dropped). Nothing from the environment is ever put on a span.
2. **The evaluator** (`python -m multisync.evals.run_draft_evals`) reads the spans named `draft` of the last 24 hours, builds `input` (fact sheet + doc plan),
   `output` (the draft) and `context` (the retrieved chunks), and runs Phoenix's `FaithfulnessEvaluator` over them. The judge is the pipeline's own
   cheap tier (DeepSeek `deepseek-v4-pro`, `AI_CHEAP_MODEL`), called through `multisync/llm.py`, keys from the environment.
3. **The result** is logged to Phoenix as the span annotation `draft_faithfulness` on each original span id: score `1.0` / label `faithful`, or score `0.0` /
   label `unfaithful` (a hallucination: the draft states something its chunks do not support), plus the judge's explanation. Re-running overwrites the same
   annotation on the same spans (Phoenix keys it on name + span), so the job is idempotent.
4. **`multisync evals flag-low-scores`** lists the drafts whose logged score is below a threshold.

Why `FaithfulnessEvaluator` and not `HallucinationEvaluator`: in arize-phoenix-evals 3.x `HallucinationEvaluator` judges a reply against the *conversation*
(there is no reference input) and scores `1.0` for hallucinated, so a "low score is bad" threshold would be backwards. `FaithfulnessEvaluator` takes the
reference chunks and scores `1.0` for faithful. The faithfulness score *is* the hallucination check against the retrieved documents.

The scores are binary (0.0 or 1.0), so any threshold between them (the default 0.7) flags exactly the unfaithful drafts.

## Run it by hand

```bash
pip install -r requirements-evals.txt            # Phoenix client + evals + pandas; not part of the runtime image
export PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006   # e.g. through: kubectl -n multirepo port-forward svc/phoenix 6006:6006
export PHOENIX_API_KEY=...                       # a Phoenix system key (the one the Jobs trace with)
export DEEPSEEK_API_KEY=...                      # the judge (cheap tier)

python -m multisync.evals.run_draft_evals --dry-run          # read spans, build the inputs, call no model, log nothing
python -m multisync.evals.run_draft_evals                    # score the last 24h (costs model calls)
python -m multisync.evals.run_draft_evals --hours 6 --max-spans 20

multisync evals flag-low-scores --threshold 0.7              # list drafts below it, read-only
multisync evals flag-low-scores --threshold 0.7 --ticket     # also open one cAImanDesk ticket per change unit (never without --ticket)
```

`--max-spans` (default 500) caps what reaches the model. No draft spans in the window: it says so and exits 0. It exits 1 only when Phoenix cannot be
reached, or every judge call failed.

`flag-low-scores` prints, for each draft below the threshold, the change unit id, repo, page, attempt, span id, a link and the judge's reason. The link is
the review PR when `review_outcomes` has one (set `FACTSTORE_DATABASE_URL` to look it up); a PR that is still open has no row yet, and the link is then the source commit.
A page that needed several attempts has several draft spans; each is listed, and `--ticket` opens one ticket for the change unit (title
`[docs-eval] <repo>@<commit> <page>`, using the `multisync/tickets.py` conventions: no duplicates, a repeat adds a note).

## The nightly CronJob

`infra/k8s/draft-evals-cronjob.yaml`: `multisync-draft-evals` in `multirepo`, `30 2 * * *`, `concurrencyPolicy: Forbid`, 512 MiB, read-only root filesystem.
It uses `multisync-config` and, from `multisync-secrets`, `DEEPSEEK_API_KEY` and `PHOENIX_API_KEY`.

**Image.** The runtime image stays as it was (it also runs the stdlib webhook receiver). The Phoenix/pandas stack (about 240 MB uncompressed) lives behind the
`evals` extra (`pip install .[evals]`, or `requirements-evals.txt`) and the Dockerfile target `evals`, published as `<pipeline image>:<tag>-evals`. The manifest
says `image: __PIPELINE_IMAGE__-evals`, so the one placeholder and the one tag still drive both. Heavy libraries are imported lazily: the normal CLI and the tests
(without the extra) never load them.

Deploy: merge, let CI publish `<tag>` and `<tag>-evals`, run the *Deploy to cluster* workflow with that tag (it renders `infra/k8s` and applies it), then:

```bash
kubectl -n multirepo get cronjob multisync-draft-evals
kubectl -n multirepo create job --from=cronjob/multisync-draft-evals draft-evals-manual   # run it now
kubectl -n multirepo logs job/draft-evals-manual
```

## See the scores in Phoenix

Open the project `multirepo-agent-docs`, filter the spans with `name == "draft"`. The `draft_faithfulness` column shows the score; click a span for the label and
the judge's explanation, next to the `retrieval.documents` it was judged against. To see only the hallucinated ones, filter with
`evals["draft_faithfulness"].score < 0.7`.

## Clean up

* Stop the schedule: `kubectl -n multirepo delete cronjob multisync-draft-evals` (and remove it from `infra/k8s/kustomization.yaml`, or the next deploy brings it back).
* Remove the scores: delete the annotation `draft_faithfulness` from the spans in the Phoenix UI (ids of the annotations:
  `client.spans.get_span_annotations_dataframe(..., include_annotation_names=["draft_faithfulness"])`). The spans themselves are untouched;
  the annotation name is the only thing this feature adds.
* The extra `draft` span is harmless to remove: delete the `with tracing.span("draft", ...)` block in `pipeline.n_write_draft`; nothing else reads it.

## Verified against

arize-phoenix 20.19.0 (the server) with arize-phoenix-client 3.5.0 and arize-phoenix-evals 3.9.0. In that line `px.Client`, `SpanEvaluations`, `log_evaluations`,
`run_evals` and `HallucinationEvaluator` as a reference-grounded evaluator no longer exist; the code uses `phoenix.client.Client().spans.get_spans_dataframe`,
`log_span_annotations_dataframe`, `get_span_annotations_dataframe`, `phoenix.evals.evaluate_dataframe` and `phoenix.evals.metrics.FaithfulnessEvaluator`.
