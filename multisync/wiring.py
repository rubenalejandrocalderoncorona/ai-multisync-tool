"""Shared wiring for the CLI commands: builds real dependencies from the environment."""
from __future__ import annotations

import os

from .config import load_config, read_json
from .context import record_doc_refs
from .factstore import create_fact_store
from .llm import LLM
from .util import AttrDict, attrs
from .vectorstore import MemoryVectorStore, QdrantStore, index_approved


def build_deps(env=None) -> AttrDict:
    env = os.environ if env is None else env
    cfg = load_config(env)
    llm = LLM(cfg["ai"])
    memory = cfg["qdrant"]["driver"] == "memory"
    vectors = MemoryVectorStore() if memory else QdrantStore(cfg["qdrant"], cfg["ai"]["embedDim"])
    code_vectors = MemoryVectorStore() if memory else QdrantStore(cfg["qdrant"], cfg["ai"]["embedDim"], collection=cfg["qdrant"]["codeCollection"])
    facts = create_fact_store(cfg["factstore"])
    d = AttrDict(
        cfg=cfg, llm=llm, vectors=vectors, codeVectors=code_vectors, facts=facts,
        reposConfig=attrs(read_json(cfg["paths"]["reposConfig"], {"defaults": {}, "repos": {}})),
        registry=read_json(cfg["paths"]["featureRegistry"], {}),
    )
    return d


def apply_decision(decision: dict, d, repo: str, file: str, commit: str) -> None:
    """Side effects of a decision, shared by the CI runner and the demo: write/delete the page, index only what policy has already
    approved, keep rejected drafts out of the site."""
    if decision["action"] == "write":
        os.makedirs(os.path.dirname(decision["targetPath"]) or ".", exist_ok=True)
        with open(decision["targetPath"], "w", encoding="utf-8") as fh:
            fh.write(decision["content"])
    elif decision["action"] == "delete" and os.path.exists(decision["targetPath"]):
        os.unlink(decision["targetPath"])

    # Auto-trust repos are approved by policy: index now. Review repos index after the PR merges.
    if decision["outcome"] == "published":
        if decision["action"] == "write":
            index_approved(d["vectors"], d["llm"], repo, file, decision["content"], commit)
            d["facts"].approve_claims(repo, file, commit)
            if hasattr(d["facts"], "known_symbol_names"):
                record_doc_refs(d["facts"], repo, file, decision["content"], "approved", d["facts"].known_symbol_names())
        else:
            d["vectors"].delete_by_path(repo, file)

    # A fallback never touches the site; keep the draft for the human who picks up the ticket.
    if decision["outcome"] == "fallback" and decision.get("draft"):
        os.makedirs("rejected", exist_ok=True)
        with open(os.path.join("rejected", file.replace("\\", "__").replace("/", "__")), "w", encoding="utf-8") as fh:
            fh.write(decision["draft"])
