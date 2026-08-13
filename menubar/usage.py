"""Poll Claude subscription usage using the Companion's own long-lived token.

Unlike the BLE daemon's payload builder (which treats 5h/7d windows and
overage as mutually exclusive), this reads all three independently — an
account can have 5h/7d windows AND overage rejected-because-disabled AND (for
accounts with pay-as-you-go enabled) a real overage utilization, all in the
same response. Verified live against a real account on 2026-08-13: 5h/7d
present, overage present but status "rejected" / reason "org_level_disabled".
"""

from __future__ import annotations

import time

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "content-type": "application/json",
}
# Minimal real request (max_tokens=1) — /api/oauth/usage is a documented dead
# end (aggressive, unrecoverable 429s; see anthropics/claude-code#31637), so
# this is the only reliable source for these numbers today.
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


class TokenInvalid(Exception):
    """401/403 — the long-lived token was revoked or is otherwise dead."""


def _pct(util: str | None) -> int | None:
    if util is None:
        return None
    try:
        return int(round(float(util) * 100))
    except ValueError:
        return None


def _reset_minutes(reset_ts: str | None, now: float) -> int | None:
    if reset_ts is None:
        return None
    try:
        mins = (float(reset_ts) - now) / 60.0
    except ValueError:
        return None
    return max(0, int(round(mins)))


def poll_usage(token: str) -> dict:
    """Return usage data. Raises TokenInvalid on 401/403.

    Shape:
        {
          "five_hour":  {"pct": int, "reset_min": int, "status": str} | None,
          "weekly":     {"pct": int, "reset_min": int, "status": str} | None,
          "extra_usage": {"pct": int, "reset_min": int, "status": str,
                           "disabled_reason": str | None} | None,
        }
    None for a section means the account doesn't expose that window at all
    (vs. "extra_usage" present-but-disabled, which is a real, renderable
    state — show it as off, don't hide it).
    """
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    resp = httpx.post(API_URL, headers=headers, json=API_BODY, timeout=20.0)

    if resp.status_code in (401, 403):
        raise TokenInvalid(f"HTTP {resp.status_code}")
    resp.raise_for_status()

    h = resp.headers
    now = time.time()

    five_hour = None
    if h.get("anthropic-ratelimit-unified-5h-utilization") is not None:
        five_hour = {
            "pct": _pct(h.get("anthropic-ratelimit-unified-5h-utilization")),
            "reset_min": _reset_minutes(h.get("anthropic-ratelimit-unified-5h-reset"), now),
            "status": h.get("anthropic-ratelimit-unified-5h-status", "unknown"),
        }

    weekly = None
    if h.get("anthropic-ratelimit-unified-7d-utilization") is not None:
        weekly = {
            "pct": _pct(h.get("anthropic-ratelimit-unified-7d-utilization")),
            "reset_min": _reset_minutes(h.get("anthropic-ratelimit-unified-7d-reset"), now),
            "status": h.get("anthropic-ratelimit-unified-7d-status", "unknown"),
        }

    extra_usage = None
    if h.get("anthropic-ratelimit-unified-overage-status") is not None:
        extra_usage = {
            "pct": _pct(h.get("anthropic-ratelimit-unified-overage-utilization")),
            "reset_min": _reset_minutes(h.get("anthropic-ratelimit-unified-overage-reset"), now),
            "status": h.get("anthropic-ratelimit-unified-overage-status", "unknown"),
            "disabled_reason": h.get("anthropic-ratelimit-unified-overage-disabled-reason"),
        }

    return {"five_hour": five_hour, "weekly": weekly, "extra_usage": extra_usage}
