"""Small helpers shared across the package."""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any


class AttrDict(dict):
    """A dict that also reads as attributes (`cfg.ai.tiers.cheap.model`). Keys keep their JSON spelling."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def attrs(value: Any) -> Any:
    """Recursively wrap dicts so nested config reads as attributes."""
    if isinstance(value, dict) and not isinstance(value, AttrDict):
        return AttrDict({k: attrs(v) for k, v in value.items()})
    if isinstance(value, AttrDict):
        return AttrDict({k: attrs(v) for k, v in value.items()})
    if isinstance(value, list):
        return [attrs(v) for v in value]
    return value


def read_json(path: str, fallback: Any = None) -> Any:
    try:
        with open(os.path.abspath(path), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def uniq(items):
    """Order-preserving de-duplication."""
    return list(dict.fromkeys(items))


def js_split_lines(text: str) -> list[str]:
    return text.split("\n")


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(text: str) -> str:
    return _CTRL.sub("", text)


def iso_now() -> str:
    """Like JavaScript's new Date().toISOString(): UTC, millisecond precision, trailing Z."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def print_table(rows: list[dict]) -> None:
    """A plain-text stand-in for console.table."""
    if not rows:
        print("(none)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(str(c).ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def arg_value(argv: list[str], name: str) -> str | None:
    flag = f"--{name}"
    if flag in argv:
        i = argv.index(flag)
        return argv[i + 1] if i + 1 < len(argv) else None
    return None
