"""Verify Qdrant and the FactStore are reachable, create schema/collection, and exit non-zero on failure."""
from __future__ import annotations

import sys

from ..wiring import build_deps


def main() -> int:
    d = build_deps()

    def code():
        d["codeVectors"].ensure_collection()
        return d["codeVectors"].health()

    def semantic():
        d["vectors"].ensure_collection()
        return d["vectors"].health()

    def facts():
        d["facts"].migrate()
        return d["facts"].health()

    ok = True
    for name, fn in [("qdrant (semantic collection)", semantic), ("qdrant (code collection)", code), ("factstore", facts)]:
        try:
            print(f"{'ok  ' if fn() else 'FAIL'} {name}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"FAIL {name}: {e}")
    d["facts"].close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
