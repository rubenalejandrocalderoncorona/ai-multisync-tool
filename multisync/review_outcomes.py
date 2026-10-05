"""Review-outcome logging: what a human did with each generated draft. LOGGING ONLY. Nothing here approves, rejects or routes anything.

When a docs-sync pull request is closed, one row per reviewed draft (a page) goes into `review_outcomes`:

  draft_with_noedition   merged, and the page is byte-for-byte what the pipeline generated
  draft_with_edition     merged, but a reviewer changed the page first
  draft_rejected         closed without merging

The features (diff classification, model tier, similarity, judge scores, symbol coverage, policy version) were computed during the run and are read
back from the decision stored then (`decisions.metrics`); the pull request body names the source repo, the commit and the pages, which finds it.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone

import httpx

SEGMENT_TIERS = ("cheap", "expensive")
SEGMENT_CLASSES = ("internal", "public_interface")


def parse_pr_body(body: str | None) -> dict | None:
    """The generated PR body: "Source: `owner/repo` @ `sha7`" and a table with one `page.md` row per page. None when it is not one of ours."""
    m = re.search(r"Source:\s*`([^`]+)`\s*@\s*`([0-9a-f]{7,40})`", body or "")
    if not m:
        return None
    pages = re.findall(r"(?m)^\|\s*`([^`]+)`\s*\|", body or "")
    return {"repo": m.group(1), "sha": m.group(2), "pages": [p for p in pages if p != "File"]}


class GitHub:
    """The few read-only GitHub calls needed to tell an edited draft from an untouched one."""

    def __init__(self, repo: str, token: str, transport: httpx.BaseTransport | None = None):
        self.repo = repo
        self._c = httpx.Client(base_url="https://api.github.com", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                               timeout=30, transport=transport)

    def pr(self, number: int) -> dict:
        r = self._c.get(f"/repos/{self.repo}/pulls/{number}")
        r.raise_for_status()
        return r.json()

    def files(self, number: int) -> list[str]:
        out, page = [], 1
        while True:
            r = self._c.get(f"/repos/{self.repo}/pulls/{number}/files", params={"per_page": 100, "page": page})
            r.raise_for_status()
            chunk = r.json()
            out += [f["filename"] for f in chunk]
            if len(chunk) < 100:
                return out
            page += 1

    def first_commit(self, number: int) -> str:
        r = self._c.get(f"/repos/{self.repo}/pulls/{number}/commits", params={"per_page": 1})
        r.raise_for_status()
        return r.json()[0]["sha"]

    def file_at(self, path: str, ref: str) -> str | None:
        r = self._c.get(f"/repos/{self.repo}/contents/{path}", params={"ref": ref})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return base64.b64decode(data["content"]).decode("utf-8") if data.get("encoding") == "base64" else data.get("content")

    def close(self) -> None:
        self._c.close()


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _iso(ts: str | None) -> str | None:
    return ts or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def collect_rows(facts, gh, number: int, pr_url: str, reviewed_by: str | None, reviewed_at: str | None, merged: bool, default_policy: str = "unknown") -> tuple[list[dict], list[str]]:
    """Rows for every draft of one closed docs-sync PR, and the reasons any page was skipped (no stored decision, an unreadable file...)."""
    pr = gh.pr(number)
    info = parse_pr_body(pr.get("body"))
    if not info:
        return [], ["the pull request body is not a docs-sync body"]
    files = gh.files(number) if merged else []
    first = tip = None
    rows, skipped = [], []
    for page in info["pages"]:
        d = facts.find_decision(info["repo"], info["sha"], page)
        if not d:
            skipped.append(f"{page}: no pending_review decision for {info['repo']}@{info['sha']}")
            continue
        m = d.get("metrics") or {}
        final = m.get("final") or {}
        if merged:
            target = next((f for f in files if f == page or f.endswith("/" + page)), None)
            if target is None:
                skipped.append(f"{page}: the pull request does not contain the page")
                continue
            first = first or gh.first_commit(number)
            tip = tip or (pr.get("head") or {}).get("sha")
            generated, final_text = gh.file_at(target, first), gh.file_at(target, tip)
            outcome = "draft_with_noedition" if generated is not None and generated == final_text else "draft_with_edition"
        else:
            outcome = "draft_rejected"
        tier = m.get("modelTier") if m.get("modelTier") in SEGMENT_TIERS else "expensive"
        cls = m.get("diffClassification") if m.get("diffClassification") in SEGMENT_CLASSES else "internal"
        rows.append({
            "change_unit_id": f"{info['repo']}@{d['commit']}:{page}", "repo": info["repo"], "diff_classification": cls, "model_tier_used": tier,
            "similarity_score": _num(m.get("minChunkSimilarity")), "judge_score_precision": _num(final.get("precision")), "judge_score_recall": _num(final.get("recall")),
            "judge_score_style": _num(final.get("style")), "judge_score_quality": _num(final.get("quality")), "symbol_coverage_pct": _num(m.get("symbolCoverage")),
            "outcome": outcome, "reviewed_by": reviewed_by, "reviewed_at": _iso(reviewed_at), "policy_version": m.get("policyVersion") or default_policy, "pr_url": pr_url,
        })
    return rows, skipped


def record(facts, gh, number: int, pr_url: str, reviewed_by: str | None, reviewed_at: str | None, merged: bool) -> dict:
    rows, skipped = collect_rows(facts, gh, number, pr_url, reviewed_by, reviewed_at, merged)
    inserted = sum(1 for r in rows if facts.record_review_outcome(r))
    return {"rows": len(rows), "inserted": inserted, "duplicates": len(rows) - inserted, "skipped": skipped, "outcomes": [r["outcome"] for r in rows]}


# ── readiness ────────────────────────────────────────────────────────────────────
def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (z = 1.96 is a 95% interval). (0, 0) for n = 0."""
    if n <= 0:
        return 0.0, 0.0
    p = successes / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * ((p * (1 - p) + z2 / (4 * n)) / n) ** 0.5
    denom = 1 + z2 / n
    return max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)


def readiness(counts: dict, threshold: float = 0.85, min_samples: int = 30, z: float = 1.96) -> dict:
    """Read-only verdict for one segment: does the Wilson lower bound of the no-edit rate clear the threshold, at enough samples?"""
    n, k = counts["n"], counts["noedition"]
    lo, hi = wilson_interval(k, n, z)
    reasons = []
    if n < min_samples:
        reasons.append(f"only {n} reviewed drafts, need at least {min_samples}")
    if lo < threshold:
        reasons.append(f"Wilson lower bound {lo:.3f} is below the threshold {threshold}")
    return {"n": n, "noedition": k, "edition": counts.get("edition", 0), "rejected": counts.get("rejected", 0), "rate": (k / n) if n else 0.0,
            "wilson_lower": lo, "wilson_upper": hi, "z": z, "threshold": threshold, "min_samples": min_samples, "ready": not reasons, "reasons": reasons}
