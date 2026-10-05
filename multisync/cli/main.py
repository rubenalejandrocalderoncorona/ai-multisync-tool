"""`multisync <group> <command>`: one entry point over the commands. Today: `multisync metrics review-readiness ...`.
The other tools stay available as `python -m multisync.cli.<name>`."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "metrics":
        from .metrics import main as metrics_main

        return metrics_main(argv[1:])
    print("usage: multisync metrics review-readiness --segment <internal|public_interface>:<cheap|expensive> [--threshold 0.85] [--min-samples 30]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
