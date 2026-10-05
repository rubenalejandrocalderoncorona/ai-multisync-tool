"""Model router. Decides, BEFORE any model call and without one, which tier drafts a change.

  cheap      the change touches only internals (a rename, a log message, a comment, a function body, a private helper)
  expensive  the change touches a public interface: an exported signature, a route or RPC procedure, a schema model, an
             environment variable or config key, a symbol in the cross-repo registry, or a symbol that other repos' documents
             mention (the code -> docs coupling kept in the FactStore)

Why this signal: wrong documentation of a public contract costs far more than wrong documentation of an internal detail, and the
same coupling data is what retrieval needs anyway, so no second classifier is built.

Not routed here: GAR rewrites and template/folder routing (always cheap), retrieval (embeddings and cosine only, no LLM at all),
and the judge (its own setting).
"""
from __future__ import annotations

from .symbols import diff_public_symbols


def route_change(change: dict, registry: dict | None = None, facts=None, force: str = "auto") -> dict:
    """{tier: 'cheap'|'expensive', reasons, signals, classification: 'internal'|'public_interface'}"""
    registry = registry or {}
    signals = {"publicChanged": [], "total": 0, "registry": [], "referencedBy": 0}
    reasons: list[str] = []

    is_code = change.get("kind") == "code"
    after = change.get("after") or ""

    # 1. Cross-repo registry: a registered contract point appears in the change.
    signals["registry"] = [sym for sym in registry if sym in after]
    if signals["registry"]:
        reasons.append(f"cross-repo contract point: {', '.join(signals['registry'][:4])}")

    if is_code:
        # 2. The public interface, before vs after.
        d = diff_public_symbols(change.get("before") or "", after)
        signals["total"] = d["total"]
        signals["publicChanged"] = d["names"]
        if not change.get("before"):
            # A page written from scratch (first run, forced full sync): everything public in scope is new.
            if d["total"] > 0:
                reasons.append(f"new page over {d['total']} public symbol(s)")
        elif d["touched"]:
            parts = []
            if d["added"]:
                parts.append(f"{len(d['added'])} added")
            if d["removed"]:
                parts.append(f"{len(d['removed'])} removed")
            if d["changed"]:
                parts.append(f"{len(d['changed'])} changed")
            reasons.append(f"public interface touched ({', '.join(parts)}): {', '.join(d['names'][:5])}{', ...' if len(d['names']) > 5 else ''}")

        # 3. Coupling: do other repos' documents (or the docs site) mention what changed?
        if facts is not None and hasattr(facts, "doc_refs") and d["names"] and change.get("before"):
            refs = facts.doc_refs(d["names"][:200], exclude_repo=change.get("repo"))
            signals["referencedBy"] = len({f"{r['doc_repo']}:{r['doc_path']}" for r in refs})
            if refs:
                syms = list(dict.fromkeys(r["symbol"] for r in refs))[:4]
                reasons.append(f"referenced by {signals['referencedBy']} document(s) elsewhere: {', '.join(syms)}")

    # What kind of change this is, whatever model ends up being used: the segment the review outcomes are grouped by.
    classification = "public_interface" if reasons else "internal"
    if force in ("cheap", "expensive"):
        return {"tier": force, "reasons": [f"forced by ROUTER_FORCE={force}"], "signals": signals, "classification": classification}
    if reasons:
        return {"tier": "expensive", "reasons": reasons, "signals": signals, "classification": classification}
    return {"tier": "cheap", "reasons": ["internals only: no public interface changed" if is_code else "documentation restructure with no cross-repo contract"],
            "signals": signals, "classification": classification}
