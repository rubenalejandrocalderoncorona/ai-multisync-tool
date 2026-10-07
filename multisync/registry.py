"""FEATURE_REGISTRY: maps a registered "contract point" symbol to the other repositories that must ship before the feature counts
as available.

Example (config/feature-registry.json):
  { "alert_channels_enum": { "owner": "org/backend", "requires": ["org/frontend"] } }

The cross-repo check is gated by the registry: unregistered symbols cost nothing.

The registry is the optional manual override. The automatic path is `linkedRepos` in config/repos.json (see contracts.py): touched provider
routes that a linked repo calls become contract points without any registry entry.
"""
from __future__ import annotations

from .contracts import route_path
from .symbols import diff_public_symbols


def cross_repo_check(symbols: list[str], repo: str, registry: dict, factstore) -> dict:
    """{registered, incomplete: [{symbol, missing}], complete}"""
    registered, incomplete = [], []
    for symbol in symbols:
        entry = registry.get(symbol)
        if not entry or (entry.get("owner") and entry["owner"] != repo):
            continue
        registered.append(symbol)
        missing = [dep for dep in entry.get("requires") or [] if not factstore.repo_documents_symbol(dep, symbol)]
        if missing:
            incomplete.append({"symbol": symbol, "missing": missing})
    return {"registered": registered, "incomplete": incomplete, "complete": not incomplete}


def touched_routes(before: str | None, after: str | None) -> list[str]:
    """Provider routes (`GET /api/v1/x`) that a code change added or modified. Removed routes and first-time snapshots (no `before`) are not
    contract points: a full sync must not block on every route a repo already serves."""
    if not before or not after:
        return []
    d = diff_public_symbols(before, after)
    return [k.split(":", 1)[1] for k in d["added"] + d["changed"] if k.startswith("route:")]


def linked_contract_points(routes: list[str], consumers: list[str], factstore) -> list[dict]:
    """For each touched route, the linked consumers that call it and which of them have no approved document mentioning their call.

    [{route, consumers: [repo], missing: [repo], calls: {repo: [literal]}}]. A route nobody calls is not a point (nothing to keep in sync).
    A consumer counts as documented when an approved claim mentions its own literal (`/api/v1/events/{id}/venue`) or the provider's path as written."""
    points = []
    for route in routes:
        clients = factstore.linked_clients(consumers, route) if consumers and hasattr(factstore, "linked_clients") else []
        if not clients:
            continue
        calls: dict[str, list[str]] = {}
        for c in clients:
            lits = calls.setdefault(c["repo"], [])
            if c["name"] not in lits:
                lits.append(c["name"])
        missing = [repo for repo, lits in calls.items()
                   if not any(factstore.repo_documents_symbol(repo, x) for x in [*lits, route_path(route)])]
        points.append({"route": route, "consumers": list(calls), "missing": missing, "calls": calls})
    return points
