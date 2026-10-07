"""The Phoenix side of the evaluator, imported lazily so the normal CLI and the tests stay light (pip install -r requirements-evals.txt, extra `evals`).
Verified against arize-phoenix 20.19.0 / arize-phoenix-client 3.5.0 / arize-phoenix-evals 3.9.0: the old px.Client, SpanEvaluations, log_evaluations and run_evals
no longer exist in that line. Their replacements are phoenix.client.Client().spans.*, evaluate_dataframe and the evaluators in phoenix.evals.metrics."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .draft_evals import EVAL_NAME, SPAN_ID, SPAN_NAME

COLUMNS = ["input.value", "output.value", "retrieval.documents", "multisync.change_unit_id", "multisync.repo", "multisync.commit", "multisync.page",
           "multisync.attempt", "multisync.tier"]


def make_client(env=None):
    """Read-only use: get_spans_dataframe / get_span_annotations_dataframe, and log_span_annotations_dataframe for the evaluations."""
    from phoenix.client import Client

    env = os.environ if env is None else env
    return Client(base_url=env.get("PHOENIX_COLLECTOR_ENDPOINT") or None, api_key=env.get("PHOENIX_API_KEY") or None)


def project_name(env=None) -> str:
    return (os.environ if env is None else env).get("PHOENIX_PROJECT_NAME") or "multirepo-agent-docs"


def draft_span_rows(client, project: str, hours: float, limit: int, now: datetime | None = None) -> list[dict]:
    """The draft spans of the last `hours`, as plain dict rows (the span id under `context.span_id`)."""
    from phoenix.client.types.spans import SpanQuery

    end = now or datetime.now(timezone.utc)
    query = SpanQuery().where(f"name == '{SPAN_NAME}'").select(*COLUMNS)
    df = client.spans.get_spans_dataframe(query=query, start_time=end - timedelta(hours=hours), end_time=end, limit=limit, project_identifier=project)
    return [] if df is None or df.empty else df.reset_index().to_dict("records")


def evaluate(rows: list[dict], judge):
    """Run the faithfulness evaluator over the built rows. Returns the rows with a `faithfulness_score` JSON column (None where the judge failed)."""
    import pandas as pd
    from phoenix.evals import evaluate_dataframe
    from phoenix.evals.metrics import FaithfulnessEvaluator

    evaluator = FaithfulnessEvaluator(llm=judge)
    scored = evaluate_dataframe(dataframe=pd.DataFrame(rows), evaluators=[evaluator], hide_tqdm_bar=True, exit_on_error=False, max_retries=2)
    return scored.astype(object).where(scored.notna(), None).to_dict("records"), f"{evaluator.name}_score"


def log_evaluations(client, annotations: list[dict]) -> int:
    """Attach the scores to the original span ids. Idempotent: Phoenix keys an annotation on (name, span, identifier), so a re-run overwrites."""
    import pandas as pd

    df = pd.DataFrame([{k: a[k] for k in ("span_id", "label", "score", "explanation", "metadata")} for a in annotations])
    return len(client.spans.log_span_annotations_dataframe(dataframe=df, annotation_name=EVAL_NAME, annotator_kind="LLM", sync=True))


def logged_scores(client, project: str, span_ids: list[str]) -> list[dict]:
    df = client.spans.get_span_annotations_dataframe(span_ids=span_ids, project_identifier=project, include_annotation_names=[EVAL_NAME])
    return [] if df is None or df.empty else df.reset_index().to_dict("records")


__all__ = ["make_client", "project_name", "draft_span_rows", "evaluate", "log_evaluations", "logged_scores", "SPAN_ID"]
