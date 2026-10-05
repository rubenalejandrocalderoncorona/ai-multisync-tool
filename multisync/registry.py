"""FEATURE_REGISTRY: maps a registered "contract point" symbol to the other repositories that must ship before the feature counts
as available.

Example (config/feature-registry.json):
  { "alert_channels_enum": { "owner": "org/backend", "requires": ["org/frontend"] } }

The cross-repo check is gated by the registry: unregistered symbols cost nothing.
"""
from __future__ import annotations


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
