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
import os
import random
import re
from datetime import datetime, timezone

import httpx

SUPERSEDED_MARKER = "<!-- multisync:superseded -->"
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

    def open_prs(self, base: str) -> list[dict]:
        out, page = [], 1
        while True:
            r = self._c.get(f"/repos/{self.repo}/pulls", params={"state": "open", "base": base, "per_page": 100, "page": page})
            r.raise_for_status()
            chunk = r.json()
            out += chunk
            if len(chunk) < 100:
                return out
            page += 1

    def supersede(self, number: int, body: str, by_number: int) -> None:
        """Mark a pull request as replaced (so it is not logged as a review), say why, and close it."""
        self._c.patch(f"/repos/{self.repo}/pulls/{number}", json={"body": f"{body or ''}\n\n{SUPERSEDED_MARKER}"}).raise_for_status()
        self._c.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": f"Superseded by #{by_number}: a newer sync of the same repository regenerated every page this pull request contained."}).raise_for_status()
        self._c.patch(f"/repos/{self.repo}/pulls/{number}", json={"state": "closed"}).raise_for_status()

    def _paged(self, path: str) -> list[dict]:
        out, page = [], 1
        while True:
            r = self._c.get(path, params={"per_page": 100, "page": page})
            r.raise_for_status()
            chunk = r.json()
            out += chunk
            if len(chunk) < 100:
                return out
            page += 1

    # Review feedback loop (multisync.cli.revise_pr): reads, plus the one write it needs, a PR comment. Pushing is done outside this class.
    def review(self, number: int, review_id: int) -> dict:
        r = self._c.get(f"/repos/{self.repo}/pulls/{number}/reviews/{review_id}")
        r.raise_for_status()
        return r.json()

    def review_comments(self, number: int, review_id: int) -> list[dict]:
        return self._paged(f"/repos/{self.repo}/pulls/{number}/reviews/{review_id}/comments")

    def issue_comments(self, number: int) -> list[dict]:
        return self._paged(f"/repos/{self.repo}/issues/{number}/comments")

    def comment(self, number: int, body: str) -> None:
        self._c.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": body}).raise_for_status()

    def close(self) -> None:
        self._c.close()


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _iso(ts: str | None) -> str | None:
    return ts or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def collect_rows(facts, gh, number: int, pr_url: str, reviewed_by: str | None, reviewed_at: str | None, merged: bool) -> tuple[list[dict], list[str]]:
    """Rows for every draft of one closed docs-sync PR, and the reasons any page was skipped (no stored decision, an unreadable file...)."""
    pr = gh.pr(number)
    if SUPERSEDED_MARKER in (pr.get("body") or ""):
        return [], ["closed because a newer draft replaced it: not a review outcome"]  # a replaced draft says nothing about its quality
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
        # Never guess the labels a segment is built from: a decision stored before these features were recorded is left out, not defaulted.
        if m.get("diffClassification") not in SEGMENT_CLASSES or m.get("modelTier") not in SEGMENT_TIERS or not m.get("policyVersion"):
            skipped.append(f"{page}: the stored decision predates review-outcome features (diff classification, model tier or policy version missing)")
            continue
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
        tier, cls = m["modelTier"], m["diffClassification"]
        rows.append({
            "change_unit_id": f"{info['repo']}@{d['commit']}:{page}", "repo": info["repo"], "diff_classification": cls, "model_tier_used": tier,
            "similarity_score": _num(m.get("minChunkSimilarity")), "judge_score_precision": _num(final.get("precision")), "judge_score_recall": _num(final.get("recall")),
            "judge_score_style": _num(final.get("style")), "judge_score_quality": _num(final.get("quality")), "symbol_coverage_pct": _num(m.get("symbolCoverage")),
            "outcome": outcome, "reviewed_by": reviewed_by, "reviewed_at": _iso(reviewed_at), "policy_version": m["policyVersion"], "pr_url": pr_url,
        })
    return rows, skipped


def audit_sample_rate(env=None) -> float:
    """AUDIT_SAMPLE_RATE: the probability that a draft merged unchanged is sampled for a second-pass audit. Default 0.10, clamped to [0, 1]."""
    raw = (env if env is not None else os.environ).get("AUDIT_SAMPLE_RATE", "")
    try:
        return min(1.0, max(0.0, float(raw))) if raw != "" else 0.10
    except ValueError:
        return 0.10


def record(facts, gh, number: int, pr_url: str, reviewed_by: str | None, reviewed_at: str | None, merged: bool,
           audit_rate: float | None = None, rng=random.random, on_sampled=None) -> dict:
    """Log the outcome of one closed PR; then sample each newly logged `draft_with_noedition` row for a second-pass audit with probability
    `audit_rate`. `on_sampled(audit_id, row)` lets the caller open a ticket. Sampling is detection only: it changes nothing about the review."""
    rows, skipped = collect_rows(facts, gh, number, pr_url, reviewed_by, reviewed_at, merged)
    rate = audit_sample_rate() if audit_rate is None else audit_rate
    inserted, sampled = 0, []
    for r in rows:
        if not facts.record_review_outcome(r):
            continue
        inserted += 1
        if r["outcome"] == "draft_with_noedition" and rng() < rate:
            audit_id = facts.sample_for_audit(r["pr_url"], r["change_unit_id"])
            if audit_id is not None:
                sampled.append(audit_id)
                if on_sampled:
                    try:
                        on_sampled(audit_id, r)
                    except Exception as e:  # noqa: BLE001 - the queue is the table; a ticket that fails to open must not undo the sampling
                        skipped.append(f"audit {audit_id}: ticket not opened ({e})")
    return {"rows": len(rows), "inserted": inserted, "duplicates": len(rows) - inserted, "skipped": skipped, "outcomes": [r["outcome"] for r in rows], "audit_sampled": sampled,
            "audit_rate": rate}


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


# ── audit gap ────────────────────────────────────────────────────────────────────
def audit_gap(raw_counts: dict, audit: dict, max_gap_pp: float = 10.0, min_audits: int = 5, z: float = 1.96) -> dict:
    """Compare the raw share of drafts merged unchanged with the share an independent second reviewer confirmed accurate.

    pct_confirmed_accurate is computed over audits that have a verdict; sampled-but-pending rows are unknown, not inaccurate, so they are reported
    separately. A drift warning needs at least `min_audits` verdicts, since one miss among three audits would be a 33-point swing by chance alone."""
    n, k = raw_counts["n"], raw_counts["noedition"]
    raw_pct = 100.0 * k / n if n else 0.0
    audited, accurate = audit["audited"], audit["accurate"]
    pct = 100.0 * accurate / audited if audited else None
    lo, hi = wilson_interval(accurate, audited, z)
    gap = (raw_pct - pct) if pct is not None else None
    enough = audited >= min_audits
    drift = bool(enough and gap is not None and gap > max_gap_pp)
    return {"raw_noedition_pct": raw_pct, "raw_n": n, "raw_noedition": k, "audit_sampled": audit["sampled"], "audit_pending": audit["sampled"] - audited, "audited": audited,
            "confirmed_accurate": accurate, "pct_confirmed_accurate": pct, "accurate_wilson_lower_pct": 100 * lo if audited else None, "accurate_wilson_upper_pct": 100 * hi if audited else None,
            "gap_pp": gap, "max_gap_pp": max_gap_pp, "min_audits": min_audits, "enough_audits": enough, "drift_warning": drift}


def supersede_older(gh, new_number: int, source_repo: str, base: str) -> dict:
    """Close the older open docs-sync pull requests of `source_repo` that a new one replaces, so a busy repository does not pile up a review queue
    of stale drafts. A pull request is only closed when the new one contains every page it contained; otherwise closing it would drop a page nobody
    regenerated, and it is left open. Returns {closed: [numbers], kept: [(number, reason)]}."""
    slug = re.escape(source_repo.replace("/", "-"))
    pattern = re.compile(rf"^docs-sync/{slug}-[0-9a-f]{{7}}$")
    new_files = set(gh.files(new_number))
    closed, kept = [], []
    for pr in gh.open_prs(base):
        n = pr["number"]
        if n == new_number or not pattern.match((pr.get("head") or {}).get("ref", "")):
            continue
        old_files = set(gh.files(n))
        if old_files and old_files <= new_files:
            gh.supersede(n, pr.get("body") or "", new_number)
            closed.append(n)
        else:
            kept.append((n, "it changes pages the newer pull request does not contain"))
    return {"closed": closed, "kept": kept}
