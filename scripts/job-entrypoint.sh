#!/usr/bin/env bash
# Entry point of the ephemeral Kubernetes Job (the same steps as .github/workflows/sync-docs-central.yml and the `index` job of
# sync-docs-approved.yml, without a standing runner). Configuration arrives as environment variables:
#   JOB_MODE=sync   SOURCE_REPO SOURCE_SHA SOURCE_BEFORE TARGET_BRANCH CHANGED_FILES CENTRAL_REPO
#   JOB_MODE=index  CENTRAL_REPO BASE_SHA MERGE_SHA
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

SHORT=${SHA:0:7}
BR="docs-sync/$(echo "$SOURCE_REPO" | tr '/' '-')-$SHORT"
git checkout -q -B "$BR"
git add -A -- "${REV[@]}"
git commit -q -m "docs: proposed sync from ${SOURCE_REPO}" -m "Source commit: $SHA"
git push -q -f origin "$BR"
jq -r '"Source: `\(.repo)` @ `\(.commit[0:7])`\n\n| File | Precision | Recall | Style | Quality |\n|---|---|---|---|---|" , (.results[] | select(.outcome=="pending_review") | "| `\(.path)` | \(.metrics.final.precision) | \(.metrics.final.recall) | \(.metrics.final.style) | \(.metrics.final.quality) |")' pipeline-results.json > pr-body.md
printf '\nMerging indexes this text into the vector store. Closing without merging leaves the index untouched.\n' >> pr-body.md
if gh pr view "$BR" --repo "$CENTRAL_REPO" >/dev/null 2>&1; then
  gh pr edit "$BR" --repo "$CENTRAL_REPO" --body-file pr-body.md
else
  gh pr create --repo "$CENTRAL_REPO" --base "$TARGET_BRANCH" --head "$BR" --title "docs: sync from ${SOURCE_REPO} (${SHORT})" --body-file pr-body.md
fi
PR_URL=$(gh pr view "$BR" --repo "$CENTRAL_REPO" --json url -q .url)
echo "PR: $PR_URL"

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
