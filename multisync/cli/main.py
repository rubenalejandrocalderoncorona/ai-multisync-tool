"""`multisync <group> <command>`: one entry point over the commands. `multisync metrics review-readiness|audit-gap ...`, `multisync audit list|show|submit` and `multisync evals flag-low-scores`.
The other tools stay available as `python -m multisync.cli.<name>`."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "metrics":
        from .metrics import main as metrics_main

        return metrics_main(argv[1:])
    if argv and argv[0] == "audit":
        from .audit import main as audit_main

        return audit_main(argv[1:])
    if argv and argv[0] == "evals":
        from .evals import main as evals_main

        return evals_main(argv[1:])
    print("usage: multisync metrics review-readiness|audit-gap --segment <internal|public_interface>:<cheap|expensive> [...]\n       multisync audit list|show <id>|submit <id> --reviewer <login> --accurate yes|no\n       multisync evals flag-low-scores [--threshold 0.7] [--hours 24] [--ticket]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
