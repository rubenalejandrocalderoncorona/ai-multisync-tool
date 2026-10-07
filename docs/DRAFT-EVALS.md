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
   | `multisync.source.text` | the source snapshot the draft was written from (`### FILE: <path>` blocks), changed files first, clipped to `DRAFT_SOURCE_CHARS` (default 30,000), with a note of what was dropped. **Omitted when `PHOENIX_CAPTURE_CONTENT=0`** |
   | `multisync.source.sha256`, `.chars`, `.files`, `.changed_files` | hash and size of the whole snapshot, the file paths in it, the files this commit changed. Always recorded (no content) |
   | `multisync.change_unit_id`, `.repo`, `.page`, `.commit` | which change unit it is: `<repo>@<commit>:<page>`, the same id `review_outcomes` uses |
   | `multisync.attempt`, `.tier`, `.mode` | which attempt, which model tier, docs or code mode |

   Long values are cut (12,000 characters for text, with a note saying how much was dropped). Nothing from the environment is ever put on a span.
2. **The evaluator** (`python -m multisync.evals.run_draft_evals`) reads the spans named `draft` of the last 24 hours and gives the judge the same evidence the
   writer had: the source snapshot (`multisync.source.text`), the retrieved chunks, and the fact sheet + doc plan. The judge is the pipeline's own cheap tier
   (DeepSeek `deepseek-v4-pro`, `AI_CHEAP_MODEL`), called through `multisync/llm.py`, keys from the environment. It uses our own prompt
   (`draft_evals.PROMPT`), not the Phoenix default template.
3. **The score** is the share of supported claims, a float in [0, 1]. The judge counts the checkable claims *about the component the page documents*
   (`multisync.page` of `multisync.repo`) and lists the unsupported ones as short quotes; wording and style are ignored; a claim is supported if any
   source file, chunk or fact supports it, and unsupported only if contradicted or absent from all evidence. `score = (claims - unsupported) / claims`.
   Label: `faithful` >= 0.9, `partial` >= 0.7, `unfaithful` below. The annotation is `draft_faithfulness` on each original span id; its metadata holds
   `claims`, `unsupported`, `quotes` (up to 10), `evidence` (`source+chunks` or `chunks_only`), `evidence_chars`, `notes`, `judge`, `change_unit_id`.
   Re-running overwrites the same annotation (Phoenix keys it on name + span), so the job is idempotent.
   The judge must answer strict JSON. Fenced JSON is accepted; a bare label (`unfaithful`) is mapped to 0.0 / 0.8 / 1.0 with the note
   `judge_replied_with_label_only` (no retry); anything else is re-asked once and then the span is left unscored.
4. **`multisync evals flag-low-scores`** lists the drafts whose logged score is below a threshold.

### Why the source is recorded on the span (evidence parity)

The first production run scored 14 of 15 drafts `unfaithful`. The judge only saw the retrieved chunks (up to 12 x 1500 characters), while the writer saw the
whole snapshot (up to ~60 KB) plus the fact sheet: true statements looked unsupported, and chunks about one component (the webhook receiver, standard
library only) were held against a page about another (LangGraph, httpx). Two designs were possible: (a) record the source on the span, (b) re-fetch the
files at `multisync.commit` through the GitHub API in the evaluator. We chose (a): it needs no GitHub credential or rate budget in the nightly Job, works
for private repos and force-pushed commits, and the judge sees exactly what the writer saw, even if the repo moved on. It does not bloat Phoenix: at
most 30,000 characters per draft span (`DRAFT_SOURCE_CHARS`), changed files first, plus a hash and paths. Content capture off
(`PHOENIX_CAPTURE_CONTENT=0`) records no source text, like the other content attributes; the evaluator then judges on the chunks and the annotation says
`evidence: chunks_only`.

### What the score does and does not mean

* It estimates how many of the page's claims about its own component trace to the evidence. It is not a proof of correctness and not a style score.
* The judge is a cheap model: expect noise of a claim or two; that is why the score is a share with a `partial` band, not a binary flip.
* `evidence: chunks_only` (old spans, or content capture off, or a snapshot over the budget whose relevant part was clipped) is a weaker score; `flag-low-scores`
  says so. A snapshot over the budget is clipped, so a claim supported only in the clipped part can still look unsupported: raise `DRAFT_SOURCE_CHARS` if that shows up.
* Claims the writer got from its own general knowledge are, correctly, unsupported.

## Run it by hand

```bash
pip install -r requirements-evals.txt            # Phoenix client + evals + pandas; not part of the runtime image
export PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006   # e.g. through: kubectl -n multirepo port-forward svc/phoenix 6006:6006
export PHOENIX_API_KEY=...                       # a Phoenix system key (the one the Jobs trace with)
export DEEPSEEK_API_KEY=...                      # the judge (cheap tier)

python -m multisync.evals.run_draft_evals --sample 10 --dry-run   # which spans, evidence size per span (source files / chunks / chars), no model call, nothing logged
python -m multisync.evals.run_draft_evals --sample 10 --explain   # score 10 drafts and print each score with its unsupported quotes; writes NOTHING (eyeball false positives)
python -m multisync.evals.run_draft_evals --dry-run          # all spans, no model call, log nothing
python -m multisync.evals.run_draft_evals                    # score the last 24h (costs model calls)
python -m multisync.evals.run_draft_evals --hours 6 --max-spans 20

multisync evals flag-low-scores --threshold 0.7              # list drafts below it (default 0.7, or DRAFT_EVALS_THRESHOLD), read-only
multisync evals flag-low-scores --threshold 0.7 --ticket     # also open one cAImanDesk ticket per change unit (never without --ticket)
```

`--max-spans` (default 500) caps what reaches the model. No draft spans in the window: it says so and exits 0. It exits 1 only when Phoenix cannot be
reached, or every judge call failed.

`flag-low-scores` prints, for each draft below the threshold, the change unit id, repo, page, attempt, span id, a link, how many claims were unsupported, the quotes,
the evidence kind (with a "weaker score" note for `chunks_only`) and the judge's reason. Run `--sample N --explain` first after any change to the prompt or the budget. The link is
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

Open the project `multirepo-agent-docs`, filter the spans with `name == "draft"`. The `draft_faithfulness` column shows the score; click a span for the label,
the quotes (annotation metadata) and the judge's explanation, next to the `retrieval.documents` and `multisync.source.text` it was judged against. To see only the
unfaithful ones, filter with `evals["draft_faithfulness"].score < 0.7`.

## Clean up

* Stop the schedule: `kubectl -n multirepo delete cronjob multisync-draft-evals` (and remove it from `infra/k8s/kustomization.yaml`, or the next deploy brings it back).
* Remove the scores: delete the annotation `draft_faithfulness` from the spans in the Phoenix UI (ids of the annotations:
  `client.spans.get_span_annotations_dataframe(..., include_annotation_names=["draft_faithfulness"])`). The spans themselves are untouched;
  the annotation name is the only thing this feature adds.
* The extra `draft` span is harmless to remove: delete the `with tracing.span("draft", ...)` block in `pipeline.n_write_draft`; nothing else reads it.

## Verified against

arize-phoenix 20.19.0 (the server) with arize-phoenix-client 3.5.0 and arize-phoenix-evals 3.9.0 (the evaluator itself no longer uses `phoenix.evals`: our prompt and parser are in `multisync/evals/draft_evals.py`). In that line `px.Client`, `SpanEvaluations`, `log_evaluations`,
`run_evals` and `HallucinationEvaluator` as a reference-grounded evaluator no longer exist; the code uses `phoenix.client.Client().spans.get_spans_dataframe`,
`log_span_annotations_dataframe` and `get_span_annotations_dataframe`.
