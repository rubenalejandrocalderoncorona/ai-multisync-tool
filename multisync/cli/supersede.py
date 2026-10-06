"""Close the older open docs-sync pull requests of a source repository that a newer one replaces. Never fails the Job that carries it.

env: CENTRAL_REPO  PR_NUMBER (the newer pull request)  SOURCE_REPO  TARGET_BRANCH  GITHUB_TOKEN (or DOCS_SYNC_PAT)
A pull request is closed only when the newer one contains every page it contained; the closure is marked so the review-outcome log ignores it.
"""
from __future__ import annotations

import json
import os
import sys

from ..review_outcomes import GitHub, supersede_older


def main() -> int:
    env = os.environ
    try:
        gh = GitHub(env["CENTRAL_REPO"], env.get("GITHUB_TOKEN") or env["DOCS_SYNC_PAT"])
        print(json.dumps(supersede_older(gh, int(env["PR_NUMBER"]), env["SOURCE_REPO"], env.get("TARGET_BRANCH", "qa"))))
    except Exception as e:  # noqa: BLE001
        print(f"older pull requests not superseded: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
