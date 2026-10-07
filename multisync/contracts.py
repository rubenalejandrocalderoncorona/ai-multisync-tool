"""Linked repositories and API contract matching. Deterministic, no model.

A repo entry in config/repos.json may declare `linkedRepos` (["owner/repo", ...]) and `contract: {"role": "provider"|"consumer"|"both"}`.
A link is SYMMETRICAL in meaning: when A lists B, the pair is linked even if B does not list A (declare each pair once).

When a provider repo changes an HTTP route and a linked repo has a `client_call` symbol (a literal API path in its code) that matches the
route, that consumer must have approved documentation mentioning its call before the change counts as available end to end.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

MAX_LINKED = 10
ROLES = ("provider", "consumer", "both")
GATE_MODES = ("off", "warn", "block")

_VERB = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|ANY)\s+(?=/)", re.I)


def route_path(name: str) -> str:
    """`GET /api/v1/events/{id}` -> `/api/v1/events/{id}` (a bare path is returned unchanged)."""
    return _VERB.sub("", str(name or "").strip())


def normalize_route(path: str) -> str:
    """Lowercase, host-less, no query string or trailing slash; every path variable ({id}, {id:\\d+}, :id, <id>, ${expr}) becomes {}."""
    p = route_path(path)
    p = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", p, flags=re.I)  # scheme + host
    p = re.split(r"[?#]", p, maxsplit=1)[0]
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r"\{[^}]*\}", "{}", p)
    p = re.sub(r"<[^>]*>", "{}", p)
    p = "/".join("{}" if re.fullmatch(r":[A-Za-z_]\w*", s) else s for s in p.split("/")).lower()
    p = re.sub(r"/{2,}", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p if p.startswith("/") or not p else "/" + p


def routes_match(provider_route: str, consumer_call: str) -> bool:
    """True when both normalise to the same path with the same number of segments. No substring or prefix matching: `/api/events` never
    matches `/api/events/{}/venue`, and `/api/events` never matches `/api/events-archive`."""
    a, b = normalize_route(provider_route), normalize_route(consumer_call)
    if not a or not b or a == "/" or b == "/":
        return False
    return a.split("/") == b.split("/")


# ── config: links ────────────────────────────────────────────────────────────
def _repos(repos_config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return (repos_config or {}).get("repos") or {}


def _entry(repos_config, repo: str) -> dict:
    defaults = (repos_config or {}).get("defaults") or {}
    return {**defaults, **(_repos(repos_config).get(repo) or {})}


def _declared(entry: Mapping[str, Any]) -> list[str]:
    raw = entry.get("linkedRepos")
    if not isinstance(raw, (list, tuple)):
        return []
    return [r.strip() for r in raw if isinstance(r, str) and "/" in r.strip()][:MAX_LINKED]


def role_of(entry: Mapping[str, Any]) -> str:
    c = entry.get("contract")
    role = c.get("role") if isinstance(c, Mapping) else None
    return role if role in ROLES else "both"


def linked_repos(repos_config: Mapping[str, Any] | None, repo: str) -> list[str]:
    """Repos linked with `repo`, in either direction, without duplicates (capped)."""
    out = list(_declared(_entry(repos_config, repo)))
    for other in _repos(repos_config):
        if other != repo and repo in _declared(_entry(repos_config, other)):
            out.append(other)
    return [r for r in dict.fromkeys(out) if r != repo][:MAX_LINKED * 2]


def consumer_repos(repos_config, repo: str) -> list[str]:
    """Linked repos that may call this repo's API (role consumer or both). Empty when `repo` itself is consumer-only."""
    if role_of(_entry(repos_config, repo)) == "consumer":
        return []
    return [r for r in linked_repos(repos_config, repo) if role_of(_entry(repos_config, r)) in ("consumer", "both")]


def gate_mode(policy: Mapping[str, Any] | None, env_value: str | None = None) -> str:
    """off | warn | block. The CROSS_REPO_GATE environment value wins over the repo's `crossRepoGate`; anything invalid means block."""
    for v in (env_value, (policy or {}).get("crossRepoGate")):
        if isinstance(v, str) and v.strip().lower() in GATE_MODES:
            return v.strip().lower()
    return "block"


def validate_links(repos_config: Mapping[str, Any] | None) -> list[str]:
    """Problems with linkedRepos / contract / crossRepoGate across the config, as warnings. Never raises."""
    warnings: list[str] = []
    repos = _repos(repos_config)
    for name, raw in repos.items():
        e = raw if isinstance(raw, Mapping) else {}
        if "linkedRepos" in e:
            lr = e["linkedRepos"]
            if not isinstance(lr, (list, tuple)):
                warnings.append(f"{name}: linkedRepos must be a list of \"owner/repo\" strings")
                lr = []
            if len(lr) > MAX_LINKED:
                warnings.append(f"{name}: linkedRepos lists {len(lr)} repos; only the first {MAX_LINKED} are used. Link only repos that share an API")
            for r in lr[:MAX_LINKED]:
                if not isinstance(r, str) or not re.fullmatch(r"[\w.\-]+/[\w.\-]+", r.strip()):
                    warnings.append(f"{name}: linkedRepos entry {r!r} is not an \"owner/repo\" string")
                    continue
                other = r.strip()
                if other == name:
                    warnings.append(f"{name}: linkedRepos lists the repo itself")
                elif other not in repos:
                    warnings.append(f"{name}: linked repo {other} has no entry in repos.json (it is never indexed, so the link has no effect)")
                elif name < other and name in _declared(repos[other] if isinstance(repos[other], Mapping) else {}):
                    warnings.append(f"{name} <-> {other}: the link is declared on both sides; one declaration per pair is enough")
        if "contract" in e:
            c = e["contract"]
            if not isinstance(c, Mapping):
                warnings.append(f"{name}: contract must be an object like {{\"role\": \"provider\"}}")
            elif c.get("role") is not None and c.get("role") not in ROLES:
                warnings.append(f"{name}: contract.role {c.get('role')!r} must be one of {', '.join(ROLES)} (default both)")
        if "crossRepoGate" in e and e["crossRepoGate"] not in GATE_MODES:
            warnings.append(f"{name}: crossRepoGate {e['crossRepoGate']!r} must be one of {', '.join(GATE_MODES)} (default block)")
    return warnings
