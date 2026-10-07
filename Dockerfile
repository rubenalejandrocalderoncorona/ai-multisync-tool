# The pipeline image: the Python package, the webhook receiver (python -m multisync.webhook) and the Job entrypoint.
# One image for both, so there is a single thing to build and import into the cluster.
# A second target, `evals`, adds the Phoenix/pandas stack for the nightly draft evaluator (docs/DRAFT-EVALS.md). It is the same image plus ~150 MB, so the
# default target (the last stage, `pipeline`) is built exactly as before and never carries it:  docker build --target evals -t ...-evals .
FROM python:3.12-alpine AS base
# git, gh and jq: the on-demand Job clones the repos and opens the review PR itself (scripts/job-entrypoint.sh).
RUN apk add --no-cache bash git jq github-cli openssl \
 && adduser -D -u 1000 app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY multisync ./multisync
COPY scripts ./scripts
COPY infra/postgres ./infra/postgres
COPY config ./config
COPY prompts ./prompts
COPY docs/templates ./docs/templates
COPY .github/instructions ./.github/instructions
RUN chmod +x scripts/job-entrypoint.sh
USER app
CMD ["python", "-m", "multisync.cli.healthcheck"]

FROM base AS evals
USER root
COPY requirements-evals.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt -r requirements-evals.txt
USER app
CMD ["python", "-m", "multisync.evals.run_draft_evals"]

# Last stage = the default `docker build` target.
FROM base AS pipeline
