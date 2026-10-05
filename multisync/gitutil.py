"""Thin git accessors over a checkout directory, shared by the runner and the bootstrap command."""
from __future__ import annotations

import subprocess


def git(directory: str, *args: str) -> str:
    return subprocess.run(["git", "-C", directory, *args], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def rev_exists(directory: str, rev: str) -> bool:
    try:
        git(directory, "cat-file", "-e", f"{rev}^{{commit}}")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def list_files(directory: str, rev: str) -> list[str]:
    return [l for l in git(directory, "ls-tree", "-r", "--name-only", rev).split("\n") if l]


def read_at(directory: str, rev: str, file: str) -> str | None:
    try:
        return git(directory, "show", f"{rev}:{file}")
    except subprocess.CalledProcessError:
        return None


def changed_between(directory: str, a: str, b: str) -> list[str]:
    return [l for l in git(directory, "diff", "--name-only", a, b).split("\n") if l]


def head(directory: str) -> str:
    return git(directory, "rev-parse", "HEAD").strip()


class Accessors:
    """The accessors in the shape context.py and codesource.py expect."""

    def __init__(self, directory: str):
        self.dir = directory

    def list_files(self, rev: str) -> list[str]:
        return list_files(self.dir, rev)

    def read_at(self, rev: str, file: str) -> str | None:
        return read_at(self.dir, rev, file)

    def changed_between(self, a: str, b: str) -> list[str]:
        return changed_between(self.dir, a, b)


def accessors(directory: str) -> Accessors:
    return Accessors(directory)
