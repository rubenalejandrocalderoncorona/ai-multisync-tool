#!/usr/bin/env bash
# Entry point of the ephemeral Kubernetes Job (the same steps as .github/workflows/sync-docs-central.yml and the `index` job of
# sync-docs-approved.yml, without a standing runner). Configuration arrives as environment variables:
#   JOB_MODE=sync   SOURCE_REPO SOURCE_SHA SOURCE_BEFORE TARGET_BRANCH CHANGED_FILES CENTRAL_REPO
#   JOB_MODE=index  CENTRAL_REPO BASE_SHA MERGE_SHA  (+ PR_NUMBER PR_URL PR_MERGED REVIEWED_BY REVIEWED_AT: logs the review outcome)
#   JOB_MODE=review CENTRAL_REPO PR_NUMBER PR_URL PR_MERGED REVIEWED_BY REVIEWED_AT   (a review PR closed without merging: only logs the outcome)
#   JOB_MODE=revise CENTRAL_REPO TARGET_BRANCH PR_NUMBER PR_URL REVIEW_ID REVIEWED_BY HEAD_REF   (a reviewer requested changes on a bot-authored docs-sync PR)
# Secrets: DOCS_SYNC_PAT, AI_API_KEY, DEEPSEEK_API_KEY, FACTSTORE_DATABASE_URL, CAIMANDESK_API_TOKEN. Untrusted values are only ever
# read through variables, never interpolated into code.
set -euo pipefail

TOOL=/app
WORK=${WORK:-/work}
MODE=${JOB_MODE:-sync}
CENTRAL_REPO=${CENTRAL_REPO:?CENTRAL_REPO missing}
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

[[ "$CENTRAL_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "invalid central repo"
cd "$WORK"

step "preflight: Qdrant and the FactStore must be reachable"
INTERNAL_AI_API_KEY=${INTERNAL_AI_API_KEY:-${AI_API_KEY:-}}
export INTERNAL_AI_API_KEY
(cd "$TOOL" && python -m multisync.cli.healthcheck)

[ -n "${DOCS_SYNC_PAT:-}" ] || fail "DOCS_SYNC_PAT is not set in the multisync-secrets Secret (needed to clone the repos and open the review PR)"
export GH_TOKEN=$DOCS_SYNC_PAT GITHUB_TOKEN=$DOCS_SYNC_PAT
# Authenticate git over HTTPS without putting the token in a URL or in a file.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"
GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$DOCS_SYNC_PAT" | base64 | tr -d '\n')"
export GIT_CONFIG_VALUE_0
git config --global user.name "Documentation Bot"
git config --global user.email "docs-bot@users.noreply.github.com"

clone() { git clone --quiet "https://github.com/$1.git" "$2"; }

export AI_API_BASE_URL=${AI_API_BASE_URL:-https://api.openai.com}
export QDRANT_COLLECTION=${QDRANT_COLLECTION:-docs_chunks} QDRANT_CODE_COLLECTION=${QDRANT_CODE_COLLECTION:-code_context}

if [ "$MODE" = "index" ]; then
  : "${BASE_SHA:?}" "${MERGE_SHA:?}"
  [[ "$BASE_SHA" =~ ^[0-9a-f]{7,64}$ && "$MERGE_SHA" =~ ^[0-9a-f]{7,64}$ ]] || fail "invalid sha"
  step "index approved pages from $CENTRAL_REPO@${MERGE_SHA:0:7}"
  clone "$CENTRAL_REPO" central
  cd central && git checkout --quiet "$MERGE_SHA"
  mkdir -p /tmp/removed
  mapfile -t CHANGED < <(git diff --name-only --diff-filter=AM "$BASE_SHA" "$MERGE_SHA" -- 'src/content/docs/services/')
  mapfile -t REMOVED < <(git diff --name-only --diff-filter=D "$BASE_SHA" "$MERGE_SHA" -- 'src/content/docs/services/')
  for f in "${REMOVED[@]}"; do
    [ -n "$f" ] || continue
    mkdir -p "/tmp/removed/$(dirname "$f")"; git show "$BASE_SHA:$f" > "/tmp/removed/$f"
  done
  [ "${#CHANGED[@]}" -eq 0 ] || python -m multisync.cli.index_approved "${CHANGED[@]}"
  if [ "${#REMOVED[@]}" -gt 0 ]; then
    GONE=(); for f in "${REMOVED[@]}"; do GONE+=("/tmp/removed/$f"); done
    python -m multisync.cli.index_approved --delete "${GONE[@]}"
  fi
  echo "indexed ${#CHANGED[@]} page(s), removed ${#REMOVED[@]}"
  # Logging only: what the reviewer did with the draft. Never fails the job and never touches the approval above.
  if [ -n "${PR_NUMBER:-}" ]; then step "record the review outcome"; python -m multisync.cli.record_review || true; fi
  exit 0
fi

if [ "$MODE" = "review" ]; then
  step "record the review outcome of $CENTRAL_REPO#${PR_NUMBER:-?} (closed without merging)"
  python -m multisync.cli.record_review || true
  exit 0
fi

if [ "$MODE" = "revise" ]; then
  : "${PR_NUMBER:?}" "${REVIEW_ID:?}" "${HEAD_REF:?}" "${TARGET_BRANCH:?}"
  [[ "$PR_NUMBER" =~ ^[0-9]+$ && "$REVIEW_ID" =~ ^[0-9]+$ ]] || fail "invalid PR number or review id"
  [[ "$HEAD_REF" =~ ^docs-sync/[A-Za-z0-9_.-]+$ ]] || fail "invalid head ref"
  [[ "$TARGET_BRANCH" =~ ^[A-Za-z0-9_./-]+$ ]] || fail "invalid target branch"
  step "revise $CENTRAL_REPO#$PR_NUMBER after review $REVIEW_ID"
  PAT_HEADER=$GIT_CONFIG_VALUE_0
  clone "$CENTRAL_REPO" central
  cd central
  # The branch tip is the base to revise: the bot's first commit plus any human commits. History is never rewritten and there is no force push.
  git fetch --quiet origin "$HEAD_REF"
  git checkout --quiet -B "$HEAD_REF" FETCH_HEAD
  # Config, styles and prompts come from the target branch (as in sync mode); the page text comes from the branch tip.
  git fetch --quiet origin "$TARGET_BRANCH"
  git worktree add --quiet --detach "$WORK/central-base" "origin/$TARGET_BRANCH"

  # Comments and the push are done by the GitHub App bot (so the PR stays bot-authored); with only the PAT they would carry the owner's name.
  if [ -n "${BOT_APP_ID:-}" ] && [ -n "${BOT_APP_PRIVATE_KEY:-}" ] && BOT_TOKEN=$(python -m multisync.cli.app_token); then
    export GH_TOKEN=$BOT_TOKEN GITHUB_TOKEN=$BOT_TOKEN
    GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$BOT_TOKEN" | base64 | tr -d '\n')"
    export GIT_CONFIG_VALUE_0
    echo "revision will be pushed and commented by the GitHub App bot"
  else
    echo "WARNING: no bot token (BOT_APP_ID/BOT_APP_PRIVATE_KEY missing or invalid); the revision is pushed and commented with DOCS_SYNC_PAT" >&2
  fi

  step "guards"
  python -m multisync.cli.revise_pr --plan | tee revise-plan.json
  [ "$(jq -r '.proceed' revise-plan.json)" = "true" ] || { echo "nothing to do: $(jq -r '.reason' revise-plan.json)"; exit 0; }
  SOURCE_REPO=$(jq -r '.repo' revise-plan.json); SOURCE_SHA=$(jq -r '.sha' revise-plan.json)
  [[ "$SOURCE_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "invalid source repo"
  [[ "$SOURCE_SHA" =~ ^[0-9a-f]{7,40}$ ]] || fail "invalid source sha"

  step "clone $SOURCE_REPO@${SOURCE_SHA:0:7} (the commit the pages were drafted from)"
  GIT_CONFIG_VALUE_0=$PAT_HEADER clone "$SOURCE_REPO" source-repo
  (cd source-repo && git checkout --quiet "$SOURCE_SHA")

  step "redraft with the review as feedback"
  export SOURCE_REPO SOURCE_DIR="$PWD/source-repo" RUN_ID="revise-$REVIEW_ID"
  export SITE_REPO="$CENTRAL_REPO" SITE_DIR="$PWD" DOC_STYLES="$WORK/central-base/config/doc-styles.json"
  export REPOS_CONFIG="$WORK/central-base/config/repos.json" FEATURE_REGISTRY="$WORK/central-base/config/feature-registry.json"
  export INSTRUCTIONS_FILE="$TOOL/.github/instructions/DocumentationInstructions.instructions.md" TEMPLATES_PATH="$TOOL/docs/templates"
  [ ! -f "$WORK/central-base/prompts/judge-docs.md" ] || export PROMPTS_DIR="$WORK/central-base/prompts"
  python -m multisync.cli.revise_pr
  STATUS=$(jq -r '.status' revise-result.json)
  if [ "$STATUS" != "revised" ]; then echo "status: $STATUS (nothing is pushed)"; exit 0; fi

  step "commit and push (fast-forward only)"
  mapfile -t CHANGED < <(jq -r '.changed[]' revise-result.json)
  git add -- "${CHANGED[@]}"
  git diff --staged --quiet && { echo "revised pages are identical to the branch tip"; exit 0; }
  git commit -q -m "docs: revise after review $REVIEW_ID" -m "Requested by ${REVIEWED_BY:-a reviewer} on $CENTRAL_REPO#$PR_NUMBER"
  git push -q origin "HEAD:refs/heads/$HEAD_REF"
  HEAD_SHA=$(git rev-parse HEAD) python -m multisync.cli.revise_pr --post || echo "WARNING: pushed, but the revision comment could not be posted" >&2
  exit 0
fi

[ "$MODE" = "sync" ] || fail "unknown JOB_MODE"
: "${SOURCE_REPO:?}" "${SOURCE_SHA:?}" "${TARGET_BRANCH:?}"
[[ "$SOURCE_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "invalid source repo"
[[ "$SOURCE_SHA" =~ ^[A-Za-z0-9_./-]+$ ]] || fail "invalid source ref"
[[ "$TARGET_BRANCH" =~ ^[A-Za-z0-9_./-]+$ ]] || fail "invalid target branch"

step "clone $CENTRAL_REPO (config and site) and switch to $TARGET_BRANCH"
clone "$CENTRAL_REPO" central
cd central
if git fetch --quiet origin "$TARGET_BRANCH" 2>/dev/null; then git checkout --quiet "$TARGET_BRANCH"; else git checkout --quiet -b "$TARGET_BRANCH"; fi

step "clone $SOURCE_REPO@${SOURCE_SHA:0:7}"
clone "$SOURCE_REPO" source-repo
(cd source-repo && git checkout --quiet "$SOURCE_SHA")
SHA=$(git -C source-repo rev-parse HEAD)

# Same rule as the workflow: only markdown under docs/ or documentation/, never workflow files, no path traversal.
if [ -n "${CHANGED_FILES:-}" ]; then LIST=$CHANGED_FILES
elif [ -n "${SOURCE_BEFORE:-}" ] && git -C source-repo cat-file -e "$SOURCE_BEFORE" 2>/dev/null; then
  LIST=$(git -C source-repo diff --name-only "$SOURCE_BEFORE" "$SHA" -- docs documentation README.md)
else LIST=$(git -C source-repo ls-files docs documentation README.md | grep -E '\.(md|mdx)$' || true); fi
LIST=$(echo "$LIST" | sed '/^$/d' | grep -E '^((docs|documentation)/.*\.(md|mdx|txt)|README\.md)$' | grep -v '\.\.' | sort -u || true)
echo "Changed docs:"; echo "$LIST"

step "decision pipeline"
export CHANGED_FILES="$LIST" SOURCE_SHA="$SHA" RUN_ID="job-${HOSTNAME:-local}"
export SITE_REPO="$CENTRAL_REPO" SITE_DIR="$PWD" DOC_STYLES="$PWD/config/doc-styles.json"
export REPOS_CONFIG="$PWD/config/repos.json" FEATURE_REGISTRY="$PWD/config/feature-registry.json"
export INSTRUCTIONS_FILE="$TOOL/.github/instructions/DocumentationInstructions.instructions.md" TEMPLATES_PATH="$TOOL/docs/templates"
[ ! -f prompts/judge-docs.md ] || export PROMPTS_DIR="$PWD/prompts"
python -m multisync.cli.run_pipeline

step "publish"
mapfile -t PUB < <(jq -r '.results[] | select(.outcome=="published" and .action!="none") | .targetPath' pipeline-results.json)
if [ "${#PUB[@]}" -gt 0 ]; then
  git add -A -- "${PUB[@]}"
  git diff --staged --quiet || git commit -q -m "docs: auto-sync from ${SOURCE_REPO}" -m "Source commit: $SHA"
  git push -q origin "HEAD:${TARGET_BRANCH}"
fi

mapfile -t REV < <(jq -r '.results[] | select(.outcome=="pending_review" and .action!="none") | .targetPath' pipeline-results.json)
if [ "${#REV[@]}" -eq 0 ]; then echo "nothing needs review"; python -m multisync.cli.generate_summary || true; exit 0; fi

# Review PRs are opened by the GitHub App bot, not by the owner's PAT, so a human can approve them (an author cannot approve their own PR).
if [ -n "${BOT_APP_ID:-}" ] && [ -n "${BOT_APP_PRIVATE_KEY:-}" ] && BOT_TOKEN=$(python -m multisync.cli.app_token); then
  export GH_TOKEN=$BOT_TOKEN GITHUB_TOKEN=$BOT_TOKEN
  GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$BOT_TOKEN" | base64 | tr -d '\n')"
  export GIT_CONFIG_VALUE_0
  echo "review PR will be opened by the GitHub App bot"
else
  echo "WARNING: no bot token (BOT_APP_ID/BOT_APP_PRIVATE_KEY missing or invalid); the PR is opened with DOCS_SYNC_PAT and cannot be approved by its owner" >&2
fi

SHORT=${SHA:0:7}
BR="docs-sync/$(echo "$SOURCE_REPO" | tr '/' '-')-$SHORT"
git checkout -q -B "$BR"
git add -A -- "${REV[@]}"
git commit -q -m "docs: proposed sync from ${SOURCE_REPO}" -m "Source commit: $SHA"
git push -q -f origin "$BR"
jq -r '"Source: `\(.repo)` @ `\(.commit[0:7])`\n\n| File | Precision | Recall | Style | Quality | Draft mode |\n|---|---|---|---|---|---|" , (.results[] | select(.outcome=="pending_review") | "| `\(.path)` | \(.metrics.final.precision) | \(.metrics.final.recall) | \(.metrics.final.style) | \(.metrics.final.quality) | \(.metrics.draftMode // "-") |")' pipeline-results.json > pr-body.md
printf '\nMerging indexes this text into the vector store. Closing without merging leaves the index untouched.\n' >> pr-body.md
if gh pr view "$BR" --repo "$CENTRAL_REPO" >/dev/null 2>&1; then
  gh pr edit "$BR" --repo "$CENTRAL_REPO" --body-file pr-body.md
else
  gh pr create --repo "$CENTRAL_REPO" --base "$TARGET_BRANCH" --head "$BR" --title "docs: sync from ${SOURCE_REPO} (${SHORT})" --body-file pr-body.md
fi
PR_URL=$(gh pr view "$BR" --repo "$CENTRAL_REPO" --json url -q .url)
echo "PR: $PR_URL"
# Older open drafts of this repository that this one replaces are closed (and marked, so they are not logged as rejected reviews).
PR_NUMBER=$(gh pr view "$BR" --repo "$CENTRAL_REPO" --json number -q .number) python -m multisync.cli.supersede || true

step "review ticket"
export PR_URL ENVIRONMENT=${REVIEW_ENVIRONMENT_NAME:-QA}
OUT=$(python -m multisync.cli.review_ticket open || true)
echo "$OUT"
MARKER=$(echo "$OUT" | tail -1 | jq -r '.marker // empty' 2>/dev/null || true)
URL=$(echo "$OUT" | tail -1 | jq -r '.url // empty' 2>/dev/null || true)
if [ -n "$MARKER" ]; then
  gh pr view "$BR" --repo "$CENTRAL_REPO" --json body -q .body | grep -v 'multisync:ticket=' > pr-body-new.md || true
  printf '\nTicket: %s\n\n%s\n' "$URL" "$MARKER" >> pr-body-new.md
  gh pr edit "$BR" --repo "$CENTRAL_REPO" --body-file pr-body-new.md
fi
python -m multisync.cli.generate_summary || true
