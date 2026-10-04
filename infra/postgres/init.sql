-- FactStore schema. Idempotent: safe to run on every start (compose init, k8s Job, scripts/healthcheck.js).
-- Everything lives in its own schema so the app can share a database with other workloads safely.
CREATE SCHEMA IF NOT EXISTS multisync;
SET search_path TO multisync;

CREATE TABLE IF NOT EXISTS claims (
  id         BIGSERIAL PRIMARY KEY,
  repo       TEXT        NOT NULL,
  path       TEXT        NOT NULL,
  commit     TEXT        NOT NULL,
  claim      TEXT        NOT NULL,
  supported  BOOLEAN     NOT NULL,
  approved   BOOLEAN     NOT NULL DEFAULT FALSE,
  evidence   TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claims_repo_path_idx ON claims (repo, path);
CREATE INDEX IF NOT EXISTS claims_approved_idx  ON claims (repo) WHERE approved;

CREATE TABLE IF NOT EXISTS decisions (
  id              BIGSERIAL PRIMARY KEY,
  run_id          TEXT        NOT NULL,
  repo            TEXT        NOT NULL,
  path            TEXT        NOT NULL,
  commit          TEXT        NOT NULL,
  outcome         TEXT        NOT NULL,  -- skipped | refreshed | published | pending_review | fallback
  reviewer_action TEXT        NOT NULL,  -- none | auto_published | needs_review | auto_rejected
  root_cause_tag  TEXT,
  reason          TEXT,
  metrics         JSONB       NOT NULL DEFAULT '{}'::jsonb,
  attempts        JSONB       NOT NULL DEFAULT '[]'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS decisions_repo_outcome_idx ON decisions (repo, outcome);

-- One row per LangGraph node execution: the per-stage audit trail of every run.
CREATE TABLE IF NOT EXISTS node_logs (
  id         BIGSERIAL PRIMARY KEY,
  run_id     TEXT        NOT NULL,
  repo       TEXT        NOT NULL,
  path       TEXT        NOT NULL,
  commit     TEXT        NOT NULL,
  node       TEXT        NOT NULL,   -- prefilter | cross_repo | similarity | gar | write_draft | judge | polish_draft | widen | publish | fallback
  status     TEXT        NOT NULL,   -- ok | skip | stop | fallback | deleted | error
  ms         INTEGER     NOT NULL,
  note       JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS node_logs_run_idx ON node_logs (run_id);

-- What has been indexed into the vector store for each source repo. Drives incremental vs full re-index.
CREATE TABLE IF NOT EXISTS context_state (
  repo       TEXT PRIMARY KEY,
  commit     TEXT        NOT NULL,   -- last commit fully reflected in the code index
  files      INTEGER     NOT NULL,
  chunks     INTEGER     NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
