-- FactStore schema. Idempotent: safe to run on every start (compose init, k8s Job, multisync/cli/healthcheck.py).
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

-- Code -> docs coupling: the public symbols each repo defines, and which documents mention them.
-- The model router reads this to know whether a change touches something another document depends on.
CREATE TABLE IF NOT EXISTS symbols (
  repo       TEXT        NOT NULL,
  path       TEXT        NOT NULL,
  kind       TEXT        NOT NULL,   -- export | route | rpc | model | config
  name       TEXT        NOT NULL,
  sig_hash   TEXT        NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (repo, path, kind, name)
);
CREATE INDEX IF NOT EXISTS symbols_name_idx ON symbols (name);

CREATE TABLE IF NOT EXISTS doc_refs (
  symbol   TEXT NOT NULL,
  doc_repo TEXT NOT NULL,            -- source repo of the doc, or 'site:<central repo>' for the documentation site itself
  doc_path TEXT NOT NULL,
  kind     TEXT NOT NULL,            -- source_doc | site_doc | approved
  PRIMARY KEY (symbol, doc_repo, doc_path)
);
CREATE INDEX IF NOT EXISTS doc_refs_doc_idx ON doc_refs (doc_repo, doc_path);

-- Facts about a repository as a whole ("mainly written in Go", "a dashboard for git repositories"), not about one page.
-- Every fact records what it was extracted from and when, so staleness can be detected by hashing the source again:
--   source_path          the file it came from ('README.md', 'go.mod', 'Makefile') or an aggregate: '@source-files', '@workflows', '@entrypoints'
--   source_hash          sha256 of that source's content at extraction time ('' = unknown, treated as stale)
--   extracted_at         when
--   verification_method   deterministic (read from the repo, no model) | llm_quote_grounded (model output whose verbatim quote was found in the source)
--   flag / flag_detail    contradicts_deterministic_source when a README-derived fact disagrees with a deterministic one
CREATE TABLE IF NOT EXISTS repo_facts (
  id                   BIGSERIAL PRIMARY KEY,
  repo                 TEXT        NOT NULL,
  category             TEXT        NOT NULL,   -- language | stack | build | ci | api | config | purpose | feature | architecture | usage
  fact                 TEXT        NOT NULL,
  evidence             TEXT        NOT NULL,
  source               TEXT        NOT NULL,   -- deterministic | llm
  source_path          TEXT        NOT NULL DEFAULT '',
  source_hash          TEXT        NOT NULL DEFAULT '',
  extracted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  verification_method  TEXT        NOT NULL DEFAULT 'deterministic' CHECK (verification_method IN ('deterministic','llm_quote_grounded')),
  flag                 TEXT,
  flag_detail          TEXT,
  commit               TEXT,
  UNIQUE (repo, fact)
);
-- Upgrade path for databases that have the first version of this table.
ALTER TABLE repo_facts ADD COLUMN IF NOT EXISTS source_path TEXT NOT NULL DEFAULT '';
ALTER TABLE repo_facts ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE repo_facts ADD COLUMN IF NOT EXISTS verification_method TEXT NOT NULL DEFAULT 'deterministic';
ALTER TABLE repo_facts ADD COLUMN IF NOT EXISTS flag TEXT;
ALTER TABLE repo_facts ADD COLUMN IF NOT EXISTS flag_detail TEXT;
UPDATE repo_facts SET source_hash = '' WHERE source_hash IS NULL;
ALTER TABLE repo_facts ALTER COLUMN source_hash SET DEFAULT '';
ALTER TABLE repo_facts ALTER COLUMN source_hash SET NOT NULL;
UPDATE repo_facts SET verification_method = 'llm_quote_grounded' WHERE source = 'llm' AND verification_method = 'deterministic';
DO $$ BEGIN
  ALTER TABLE repo_facts ADD CONSTRAINT repo_facts_verification_method_chk CHECK (verification_method IN ('deterministic','llm_quote_grounded'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS repo_facts_repo_idx ON repo_facts (repo, source);

-- What a human did with each generated draft. Logging only: nothing reads this to approve anything. Once enough labelled examples exist per
-- segment (diff_classification x model_tier_used), `multisync metrics review-readiness` reports whether a segment is reliable enough to consider
-- auto-approval; auto_approval_eligible stays false until a future, separate change decides otherwise.
-- One row per reviewed draft (a page) per pull request; the pair (pr_url, change_unit_id) is unique so a redelivered webhook cannot add a second row.
CREATE TABLE IF NOT EXISTS review_outcomes (
  id                     BIGSERIAL PRIMARY KEY,
  change_unit_id         TEXT        NOT NULL,   -- '<source repo>@<source commit>:<page path>'
  repo                   TEXT        NOT NULL,   -- the source repository
  diff_classification    TEXT        NOT NULL CHECK (diff_classification IN ('internal','public_interface')),
  model_tier_used        TEXT        NOT NULL CHECK (model_tier_used IN ('cheap','expensive')),
  similarity_score       DOUBLE PRECISION,
  judge_score_precision  DOUBLE PRECISION,
  judge_score_recall     DOUBLE PRECISION,
  judge_score_style      DOUBLE PRECISION,
  judge_score_quality    DOUBLE PRECISION,
  symbol_coverage_pct    DOUBLE PRECISION,       -- share of the changed public symbols the draft named (null when none were required)
  outcome                TEXT        NOT NULL CHECK (outcome IN ('draft_with_noedition','draft_with_edition','draft_rejected')),
  reviewed_by            TEXT,
  reviewed_at            TIMESTAMPTZ,
  policy_version         TEXT        NOT NULL,
  auto_approval_eligible BOOLEAN     NOT NULL DEFAULT FALSE,
  pr_url                 TEXT        NOT NULL,
  recorded_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (pr_url, change_unit_id)
);
CREATE INDEX IF NOT EXISTS review_outcomes_segment_idx ON review_outcomes (diff_classification, model_tier_used, policy_version);

-- Audit sampling: a share of the drafts a reviewer merged unchanged is checked again by a SECOND person, who confirms the page's facts against the
-- code without seeing the first reviewer's outcome. "Unchanged" does not prove "correct"; the gap between the two is the rubber-stamp signal.
-- Detection and reporting only; nothing reads these columns to approve or route anything.
ALTER TABLE review_outcomes ADD COLUMN IF NOT EXISTS audit_sampled          BOOLEAN     NOT NULL DEFAULT FALSE;
ALTER TABLE review_outcomes ADD COLUMN IF NOT EXISTS audit_reviewer         TEXT;
ALTER TABLE review_outcomes ADD COLUMN IF NOT EXISTS audit_verified_accurate BOOLEAN;
ALTER TABLE review_outcomes ADD COLUMN IF NOT EXISTS audit_notes            TEXT;
ALTER TABLE review_outcomes ADD COLUMN IF NOT EXISTS audited_at             TIMESTAMPTZ;
DO $$ BEGIN
  ALTER TABLE review_outcomes ADD CONSTRAINT review_outcomes_audit_other_reviewer
    CHECK (audit_reviewer IS NULL OR reviewed_by IS NULL OR lower(audit_reviewer) <> lower(reviewed_by));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE review_outcomes ADD CONSTRAINT review_outcomes_audit_only_if_sampled
    CHECK (audit_sampled OR (audit_reviewer IS NULL AND audit_verified_accurate IS NULL AND audit_notes IS NULL AND audited_at IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE review_outcomes ADD CONSTRAINT review_outcomes_audit_verdict_complete
    CHECK (audit_verified_accurate IS NULL OR (audit_reviewer IS NOT NULL AND audited_at IS NOT NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- The audit queue is the set of sampled rows without a verdict.
CREATE INDEX IF NOT EXISTS review_outcomes_audit_idx ON review_outcomes (diff_classification, model_tier_used) WHERE audit_sampled;

-- Once-only markers for the segment alerts (graduation: a segment is statistically validated; drift: audited accuracy fell). ALERTING ONLY: the row
-- says "a human was told"; nothing reads it to approve, suspend or route anything. The insert comes first and the alert is sent only by the writer
-- whose insert happened, so a redelivered webhook or two concurrent Jobs notify once. New table; no existing table is changed.
CREATE TABLE IF NOT EXISTS segment_alerts (
  id                  BIGSERIAL PRIMARY KEY,
  kind                TEXT        NOT NULL CHECK (kind IN ('graduation','drift')),
  diff_classification TEXT        NOT NULL,
  model_tier_used     TEXT        NOT NULL,
  policy_version      TEXT        NOT NULL,
  payload             JSONB       NOT NULL DEFAULT '{}'::jsonb,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (kind, diff_classification, model_tier_used, policy_version)
);
