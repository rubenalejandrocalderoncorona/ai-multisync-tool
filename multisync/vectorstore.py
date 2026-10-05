"""Qdrant over its REST API (no SDK dependency).

Rules enforced here:
  - Only *approved* documentation text is ever indexed (callers' responsibility, see index_approved): never drafts,
    never GAR hypothetical paragraphs.
  - Every point carries the source `commit`; a mismatch means the chunk is stale.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Callable

import httpx

from .chunker import chunk_markdown, point_id
from .llm import cosine

_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class QdrantStore:
    def __init__(self, cfg, dim: int, transport: httpx.BaseTransport | None = None, collection: str | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.dim, self.collection, self._sleep = cfg, dim, collection or cfg["collection"], sleep
        self._client = httpx.Client(transport=transport, timeout=60)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.cfg.get("apiKey"):
            h["api-key"] = self.cfg["apiKey"]
        return h

    def _req(self, method: str, path: str, body=None, retries: int = 4):
        """One request, retried on transient failures (408, 425, 429, 5xx, network errors) with exponential backoff.
        A long run must not die because of one dropped connection or a busy server."""
        attempt = 0
        while True:
            try:
                res = self._client.request(method, f"{self.cfg['url']}{path}", headers=self._headers(), content=json.dumps(body) if body is not None else None)
            except httpx.HTTPError:
                if attempt >= retries:
                    raise
                self._sleep(min(0.5 * 2**attempt, 8))
                attempt += 1
                continue
            if res.is_success:
                return res.json() if res.text else {}
            if res.status_code in _RETRYABLE and attempt < retries:
                self._sleep(min(0.5 * 2**attempt, 8))
                attempt += 1
                continue
            raise RuntimeError(f"Qdrant {method} {path} -> {res.status_code}: {res.text[:300]}")

    def health(self) -> bool:
        return self._client.get(f"{self.cfg['url']}/readyz").is_success

    def ensure_collection(self) -> None:
        c = self.collection
        if self._client.get(f"{self.cfg['url']}/collections/{c}", headers=self._headers()).is_success:
            return
        self._req("PUT", f"/collections/{c}", {"vectors": {"size": int(self.dim), "distance": "Cosine"}})
        for field in ("repo", "path", "commit", "kind"):
            self._req("PUT", f"/collections/{c}/index", {"field_name": field, "field_schema": "keyword"})

    def upsert(self, points: list[dict], batch: int = 256) -> None:
        """Batched: Qdrant rejects a request over 32 MB, which a large repository exceeds in one go."""
        for i in range(0, len(points), batch):
            self._req("PUT", f"/collections/{self.collection}/points?wait=true", {"points": points[i:i + batch]})

    def search(self, vector, limit: int = 5, repo=None, path=None, kind=None) -> list[dict]:
        must = []
        if repo:
            must.append({"key": "repo", "match": {"value": repo}})
        if path:
            must.append({"key": "path", "match": {"value": path}})
        if kind:
            must.append({"key": "kind", "match": {"any": list(kind)} if isinstance(kind, (list, tuple)) else {"value": kind}})
        body = {"vector": vector, "limit": limit, "with_payload": True}
        if must:
            body["filter"] = {"must": must}
        out = self._req("POST", f"/collections/{self.collection}/points/search", body)
        return [{"id": r["id"], "score": r["score"], "payload": r["payload"]} for r in out["result"]]

    def touch_commit(self, ids: list, commit: str) -> None:
        """Anti-staleness: re-key unchanged chunks to the new commit without re-embedding."""
        if not ids:
            return
        self._req("POST", f"/collections/{self.collection}/points/payload?wait=true", {"payload": {"commit": commit, "refreshed_at": _now()}, "points": ids})

    def delete_by_path(self, repo: str, file_path: str) -> None:
        self._req("POST", f"/collections/{self.collection}/points/delete?wait=true",
                  {"filter": {"must": [{"key": "repo", "match": {"value": repo}}, {"key": "path", "match": {"value": file_path}}]}})

    def delete_by_repo(self, repo: str, kind: str | None = None) -> None:
        must = [{"key": "repo", "match": {"value": repo}}]
        if kind:
            must.append({"key": "kind", "match": {"value": kind}})
        self._req("POST", f"/collections/{self.collection}/points/delete?wait=true", {"filter": {"must": must}})

    def count(self, repo: str | None = None, kind: str | None = None) -> int:
        must = []
        if repo:
            must.append({"key": "repo", "match": {"value": repo}})
        if kind:
            must.append({"key": "kind", "match": {"value": kind}})
        body = {"exact": True}
        if must:
            body["filter"] = {"must": must}
        return self._req("POST", f"/collections/{self.collection}/points/count", body)["result"]["count"]

    def close(self) -> None:
        self._client.close()


class MemoryVectorStore:
    """In-memory twin used by unit tests and the infra-free demo mode."""

    def __init__(self):
        self.points: dict = {}

    def health(self): return True
    def ensure_collection(self): pass
    def close(self): pass

    def upsert(self, points):
        for p in points:
            self.points[p["id"]] = p

    def search(self, vector, limit=5, repo=None, path=None, kind=None):
        kinds = None if not kind else (list(kind) if isinstance(kind, (list, tuple)) else [kind])
        hits = [
            {"id": p["id"], "score": cosine(vector, p["vector"]), "payload": p["payload"]}
            for p in self.points.values()
            if (not repo or p["payload"].get("repo") == repo) and (not path or p["payload"].get("path") == path)
            and (kinds is None or p["payload"].get("kind") in kinds)
        ]
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    def touch_commit(self, ids, commit):
        for i in ids:
            if i in self.points:
                self.points[i]["payload"]["commit"] = commit

    def delete_by_path(self, repo, file_path):
        for i in [i for i, p in self.points.items() if p["payload"].get("repo") == repo and p["payload"].get("path") == file_path]:
            del self.points[i]

    def delete_by_repo(self, repo, kind=None):
        for i in [i for i, p in self.points.items() if p["payload"].get("repo") == repo and (not kind or p["payload"].get("kind") == kind)]:
            del self.points[i]

    def count(self, repo=None, kind=None):
        return sum(1 for p in self.points.values() if (not repo or p["payload"].get("repo") == repo) and (not kind or p["payload"].get("kind") == kind))


def index_approved(store, llm, repo: str, file_path: str, content: str, commit: str) -> int:
    """Index an approved document. Replaces the file's previous chunks so the index always mirrors the final published text."""
    chunks = chunk_markdown(content)
    store.delete_by_path(repo, file_path)
    if not chunks:
        return 0
    vectors = llm.embed([f"{c['heading']}\n{c['text']}" for c in chunks])
    store.upsert([
        {"id": point_id(repo, file_path, i), "vector": vectors[i],
         "payload": {"repo": repo, "path": file_path, "chunk": i, "heading": c["heading"], "text": c["text"], "commit": commit, "kind": "approved", "approved_at": _now()}}
        for i, c in enumerate(chunks)
    ])
    return len(chunks)
