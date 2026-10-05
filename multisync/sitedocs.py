"""Read the pages of a documentation site checkout (Starlight layout) for semantic-context indexing."""
from __future__ import annotations

import os
import re
import subprocess

from .codesource import glob_to_regexp

# Pages the pipeline itself writes are indexed per source repo (kind approved), not as site context.
DEFAULT_EXCLUDE = ["src/content/docs/services/**", "src/content/docs/projects/**"]


def site_files(directory: str, exclude: list[str] | None = None) -> list[str]:
    exclude = DEFAULT_EXCLUDE if exclude is None else exclude
    root = os.path.join(directory, "src/content/docs")
    if not os.path.exists(root):
        return []
    out = []
    for cur, _dirs, files in os.walk(root):
        for name in files:
            if re.search(r"\.(md|mdx)$", name):
                out.append(os.path.relpath(os.path.join(cur, name), directory).replace(os.sep, "/"))
    ex = [glob_to_regexp(g) for g in exclude]
    return sorted(f for f in out if not any(r.match(f) for r in ex))


def site_commit(directory: str) -> str:
    try:
        return subprocess.run(["git", "-C", directory, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unversioned"


def read_site_file(directory: str):
    def read(f: str) -> str | None:
        try:
            with open(os.path.join(directory, f), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None
    return read
