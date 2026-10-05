"""Prompt and style loading. Prompts are files (prompts/*.md) so they can be reviewed and edited without code changes; the style
library (config/doc-styles.json) is a key-value store of writer personas + judge rubrics keyed by documentation type."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "prompts"
DEFAULT_STYLES = ROOT / "config" / "doc-styles.json"
_cache: dict[str, dict] = {}


def load_prompt(name: str, directory: str | os.PathLike | None = None) -> dict:
    """{text, id}; id = `<name>@<sha8>` for the run logs."""
    directory = directory or os.environ.get("PROMPTS_DIR") or DEFAULT_DIR
    file = Path(directory) / f"{name}.md"
    if not file.exists():
        file = DEFAULT_DIR / f"{name}.md"  # partial override dirs are fine
    key = str(file)
    if key not in _cache:
        text = file.read_text(encoding="utf-8").strip()
        _cache[key] = {"text": text, "id": f"{name}@{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"}
    return _cache[key]


def fill(text: str, variables: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: "" if variables.get(m.group(1)) is None else str(variables[m.group(1)]), text)


def load_styles(file: str | None = None) -> dict:
    """Custom library first (DOC_STYLES), then the bundled one, then empty."""
    file = file if file is not None else os.environ.get("DOC_STYLES")
    for f in (file, str(DEFAULT_STYLES)):
        if not f:
            continue
        try:
            return json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {"defaultStyle": None, "styles": {}}


def resolve_style(library: dict, key: str | None) -> dict:
    """Resolve a style key to {key, prompt, rubric, ...}. Unknown or missing keys fall back to `defaultStyle`, then to an empty
    style, and say so via `fallback` so the logs show it."""
    styles = library.get("styles") or {}
    if key and key in styles:
        return {"key": key, **styles[key], "fallback": False}
    dk = library.get("defaultStyle")
    if dk and dk in styles:
        return {"key": dk, **styles[dk], "fallback": bool(key)}
    return {"key": key or None, "prompt": "", "rubric": [], "fallback": bool(key)}


def style_text(style: dict, policy: dict | None = None) -> str:
    """The STYLE block handed to the judge and the polish pass."""
    policy = policy or {}
    glossary = "\n".join(f"- {k}: {v}" for k, v in (policy.get("glossary") or {}).items())
    rubric = style.get("rubric") or []
    parts = [
        style.get("key") and f"Style: {style['key']}",
        rubric and "Rubric:\n" + "\n".join(f"- {r}" for r in rubric),
        policy.get("styleGuide") and f"Repo style guide:\n{policy['styleGuide']}",
        glossary and f"Glossary (use these terms):\n{glossary}",
    ]
    return "\n\n".join(p for p in parts if p)


def outline_text(style: dict | None) -> str:
    """The section skeleton for a style, as text for the writer and planner. Empty when the style has none."""
    outline = (style or {}).get("outline") or []
    return "OUTLINE (follow this order; delete any section you have no facts for):\n" + "\n".join(f"## {o}" for o in outline) if outline else ""
