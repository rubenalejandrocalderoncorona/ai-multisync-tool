"""Alerting and audit logging for review-outcome segments. ALERTING ONLY: nothing here approves, merges, routes or switches anything on or off.

Two alerts, each fired once per (segment, policy version) and remembered in `segment_alerts`:

  graduation  a segment's no-edit rate is statistically solid (enough samples and a high enough Wilson lower bound). Tells a human that they MAY
              consider enabling auto-approval. Nothing is enabled by the alert, and no approval flag is read or written here.
  drift       the audited accuracy of a segment fell below AUDIT_ACCURACY_ALERT with enough audited drafts. Recommends that a human suspend any
              auto-approval for the segment until it is re-validated. Nothing is suspended by the alert.

Every function swallows and logs its own failures: an alert must never fail the Job that carries it, and a failed channel never removes the marker.

env: REVIEW_READINESS_MIN_SAMPLES (30)  REVIEW_READINESS_THRESHOLD (0.85)  AUDIT_MIN_SAMPLES (5)  AUDIT_ACCURACY_ALERT (0.80)
     GRADUATION_ALERT_SYNTHETIC=1 lets demo/synthetic data fire real alerts (off by default)
"""
from __future__ import annotations

import html
import os
import secrets
import sys

import httpx

from .review_outcomes import audit_gap, readiness

GRADUATION_TITLE = "🎓 Segment Graduation Alert!"
DRIFT_TITLE = "🚨 Drift Alert"
GRADUATION_RECOMMENDATION = ("Segment is statistically validated. A human may now consider enabling auto-approval for it "
                             "(nothing is enabled by this alert).")
DRIFT_RECOMMENDATION = "suspend any auto-approval for this segment until a human re-validates (nothing is suspended by this alert)."

_SYSTEM_RNG = secrets.SystemRandom()


def secure_random() -> float:
    """A uniform float in [0, 1) from the operating system's CSPRNG: the audit sample cannot be predicted or gamed by a reviewer."""
    return _SYSTEM_RNG.random()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _f(env, key, default, cast):
    try:
        raw = (env if env is not None else os.environ).get(key, "")
        return cast(raw) if raw != "" else default
    except ValueError:
        return default


def is_synthetic(policy_version: str | None, pr_urls) -> bool:
    return str(policy_version or "").startswith("demo-") or any(str(u).startswith("synthetic://") for u in pr_urls)


class Notifier:
    """Slack (alerts.slackWebhook) and the cAImanDesk informational ticket. Both are best effort and report whether they were delivered."""

    def __init__(self, alerts: dict | None, tickets=None, transport: httpx.BaseTransport | None = None):
        self.alerts, self.tickets, self.transport = alerts or {}, tickets, transport

    @property
    def slack_configured(self) -> bool:
        return bool(self.alerts.get("slackWebhook"))

    def slack(self, text: str) -> bool:
        if not self.slack_configured:
            return False
        try:
            with httpx.Client(transport=self.transport, timeout=30) as c:
                c.post(self.alerts["slackWebhook"], json={"text": text}).raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            _log(f"slack alert failed: {e}")
            return False

    def info_ticket(self, title: str, body_html: str) -> str | None:
        if not self.tickets or not getattr(self.tickets, "enabled", False):
            return None
        try:
            return self.tickets.open_info(title, body_html)
        except Exception as e:  # noqa: BLE001
            _log(f"informational ticket failed: {e}")
            return None


# ── graduation ───────────────────────────────────────────────────────────────────
def graduation_text(cls, tier, policy, res) -> str:
    return "\n".join([
        GRADUATION_TITLE,
        f"Segment: `{cls}:{tier}` (Policy: `{policy}`)",
        f"Sample Count: {res['n']} / {res['min_samples']} min",
        f"Unedited Rate: {100 * res['rate']:.1f}% ({res['noedition']}/{res['n']})",
        f"Wilson 95% Lower Bound: {res['wilson_lower']:.3f} (Threshold: {res['threshold']})",
        f"Recommendation: {GRADUATION_RECOMMENDATION}",
    ])


def graduation_ticket(cls, tier, policy, res, trail) -> tuple[str, str]:
    e = html.escape
    prs = "".join(f'<li><a href="{e(u)}">{e(u)}</a></li>' for u in trail.get("pr_urls") or [])
    body = (f"<p><strong>INFORMATIONAL: no action is required and nothing was changed.</strong> This segment crossed the readiness criteria. "
            f"{e(GRADUATION_RECOMMENDATION)}</p>"
            f"<p><strong>Segment:</strong> <code>{e(cls)}:{e(tier)}</code><br><strong>Policy version:</strong> <code>{e(policy)}</code></p>"
            f"<p><strong>Audit trail</strong><br>Reviewed drafts: {res['n']} (unchanged {res['noedition']}, edited {res['edition']}, rejected {res['rejected']})<br>"
            f"Unedited rate: {100 * res['rate']:.1f}%<br>Wilson interval (z = {res['z']}): [{res['wilson_lower']:.3f}, {res['wilson_upper']:.3f}]<br>"
            f"Thresholds: lower bound &gt;= {res['threshold']} and n &gt;= {res['min_samples']}<br>"
            f"First reviewed: {e(str(trail.get('first_reviewed_at')))}<br>Last reviewed: {e(str(trail.get('last_reviewed_at')))}<br>"
            f"Reviewers: {e(', '.join(trail.get('reviewers') or []) or 'unknown')}</p>"
            f"<p><strong>Pull requests</strong></p><ul>{prs}</ul>")
    return f"[docs-info] Segment graduation: {cls}:{tier} ({policy})", body


def check_graduation(facts, rows, notifier: Notifier, env=None) -> list[dict]:
    """Evaluate each (segment, policy) among the freshly recorded `rows`; fire the alert once for each that qualifies. Returns what fired."""
    fired = []
    try:
        min_n = _f(env, "REVIEW_READINESS_MIN_SAMPLES", 30, int)
        thr = _f(env, "REVIEW_READINESS_THRESHOLD", 0.85, float)
        allow_synth = (env if env is not None else os.environ).get("GRADUATION_ALERT_SYNTHETIC") == "1"
        groups: dict = {}
        for r in rows:
            groups.setdefault((r["diff_classification"], r["model_tier_used"], r["policy_version"]), []).append(r["pr_url"])
        for (cls, tier, policy), urls in groups.items():
            try:
                if is_synthetic(policy, urls) and not allow_synth:
                    continue
                res = readiness(facts.review_outcome_counts(cls, tier, policy), thr, min_n)
                if not res["ready"]:
                    continue
                payload = {k: res[k] for k in ("n", "noedition", "edition", "rejected", "rate", "wilson_lower", "wilson_upper", "threshold", "min_samples")}
                if not facts.claim_segment_alert("graduation", cls, tier, policy, payload):
                    continue  # already announced for this policy version (a redelivery or a later PR)
                slack = notifier.slack(graduation_text(cls, tier, policy, res))
                title, body = graduation_ticket(cls, tier, policy, res, facts.segment_trail(cls, tier, policy))
                fired.append({"kind": "graduation", "segment": f"{cls}:{tier}", "policy_version": policy, "slack": slack, "ticket": notifier.info_ticket(title, body)})
            except Exception as e:  # noqa: BLE001
                _log(f"graduation check failed for {cls}:{tier}: {e}")
    except Exception as e:  # noqa: BLE001
        _log(f"graduation check failed: {e}")
    return fired


# ── drift ────────────────────────────────────────────────────────────────────────
def drift_text(cls, tier, policy, gap, alert_below, min_audits) -> str:
    return "\n".join([
        DRIFT_TITLE,
        f"Segment: `{cls}:{tier}` (Policy: `{policy}`)",
        f"Audited accuracy: {gap['pct_confirmed_accurate']:.1f}% ({gap['confirmed_accurate']}/{gap['audited']} audits confirmed accurate; alert below {100 * alert_below:.0f}%, needs {min_audits}+ audits)",
        f"Raw unedited rate: {gap['raw_noedition_pct']:.1f}% ({gap['raw_noedition']}/{gap['raw_n']})",
        f"Recommendation: {DRIFT_RECOMMENDATION}",
    ])


def check_drift(facts, cls, tier, policy, notifier: Notifier, env=None) -> dict | None:
    """One urgent alert when a segment's audited accuracy is below AUDIT_ACCURACY_ALERT with at least AUDIT_MIN_SAMPLES verdicts. The accuracy is
    audit_gap's pct_confirmed_accurate (accurate / audited), the same number `metrics audit-gap` prints. Never writes any flag."""
    try:
        min_audits = _f(env, "AUDIT_MIN_SAMPLES", 5, int)
        alert_below = _f(env, "AUDIT_ACCURACY_ALERT", 0.80, float)
        gap = audit_gap(facts.review_outcome_counts(cls, tier, policy), facts.audit_counts(cls, tier, policy), min_audits=min_audits)
        if not gap["enough_audits"] or gap["pct_confirmed_accurate"] is None or gap["pct_confirmed_accurate"] / 100 >= alert_below:
            return None
        payload = {"audited": gap["audited"], "accurate": gap["confirmed_accurate"], "pct": gap["pct_confirmed_accurate"], "alert_below": alert_below}
        if not facts.claim_segment_alert("drift", cls, tier, policy, payload):
            return None
        return {"kind": "drift", "segment": f"{cls}:{tier}", "policy_version": policy, "slack": notifier.slack(drift_text(cls, tier, policy, gap, alert_below, min_audits))}
    except Exception as e:  # noqa: BLE001
        _log(f"drift check failed for {cls}:{tier}: {e}")
        return None


def run_after_record(facts, written: list[dict], notifier: Notifier, env=None) -> dict:
    """The hook record_review calls once a PR's rows are written: graduation for every touched segment, then the drift check for each."""
    out = {"graduation": check_graduation(facts, written, notifier, env), "drift": []}
    try:
        for cls, tier, policy in sorted({(r["diff_classification"], r["model_tier_used"], r["policy_version"]) for r in written}):
            d = check_drift(facts, cls, tier, policy, notifier, env)
            if d:
                out["drift"].append(d)
    except Exception as e:  # noqa: BLE001
        _log(f"drift sweep failed: {e}")
    return out


# ── audit spot-check announcement ────────────────────────────────────────────────
def audit_comment_text(rate: float) -> str:
    return f"🔍 [Audit Spot-Check] {100 * rate:g}% Quality Sample. A second reviewer must confirm this page against the code."


def sampled_handler(gh, number: int, rate: float, tickets=None, notifier: Notifier | None = None):
    """The on_sampled callback for `record`: opens the audit ticket and, once per pull request, comments on it (and pings Slack if configured)."""
    state = {"commented": False}

    def on_sampled(audit_id, row):
        if tickets is not None:
            repo, _, rest = row["change_unit_id"].partition("@")
            commit, _, page = rest.partition(":")
            try:
                url = tickets.open_audit(audit_id, repo, commit, page)
                if url:
                    print(f"audit {audit_id} requested: {url}")
            except Exception as e:  # noqa: BLE001
                _log(f"audit ticket failed: {e}")
        if state["commented"]:
            return
        state["commented"] = True
        try:
            gh.comment(number, audit_comment_text(rate))
        except Exception as e:  # noqa: BLE001
            _log(f"audit comment failed: {e}")
        if notifier and notifier.slack_configured:
            notifier.slack(f"{audit_comment_text(rate)}\n{row['pr_url']}")

    return on_sampled
