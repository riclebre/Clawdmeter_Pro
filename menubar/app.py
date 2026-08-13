"""Clawdmeter Companion — macOS menu bar app for Claude subscription usage.

Independent of the ESP32 device and of Claude Code CLI's own session: reads
a long-lived (`claude setup-token`) token from a dedicated Keychain item
(see auth.py) and polls the same Messages-API rate-limit-header trick the
BLE daemon uses (see usage.py) — proven to survive far longer than the
CLI's ~11h access token / ~10-day refresh token.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from pathlib import Path

import AppKit
import rumps

import auth
import usage

POLL_SECONDS = 60
LOGIN_POLL_SECONDS = 2
LOGIN_TIMEOUT_SECONDS = 300


def fmt_reset(mins: int | None) -> str:
    if mins is None:
        return ""
    if mins < 60:
        return f"{mins}m"
    hours = mins / 60
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


class ClawdmeterCompanion(rumps.App):
    def __init__(self):
        # Accessory policy: menu-bar-only, no Dock icon, no Cmd+Tab entry —
        # this is a background utility, not an app you switch to.
        # AppKit.NSApp is only populated after the shared app exists, so
        # fetch it explicitly rather than relying on the (possibly still
        # unset) module-level NSApp reference.
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        )
        icon_path = Path(__file__).parent / "assets" / "icon.png"
        super().__init__("Clawdmeter", title="…", icon=str(icon_path), template=False)
        self.five_hour_item = rumps.MenuItem("5h window: —")
        self.weekly_item = rumps.MenuItem("Weekly quota: —")
        # "Extra usage" is deliberately not shown: the rate-limit "overage"
        # header our token can read is a different metric from the R$ Usage
        # Credits balance on claude.ai/settings/usage (confirmed 0% vs 76%
        # live, 2026-08-13) — reading the real dollar figure would require
        # the `user:profile`-scoped short-lived Keychain token we're trying
        # to avoid depending on. Revisit if Anthropic exposes it elsewhere.
        self.token_status_item = rumps.MenuItem("Checking login…")
        self.login_item = rumps.MenuItem("Log in…", callback=self.start_login)
        self.paste_item = rumps.MenuItem("Paste token manually…", callback=self.paste_token)
        self.logout_item = rumps.MenuItem("Log out", callback=self.logout)
        self.menu = [
            self.five_hour_item,
            self.weekly_item,
            None,
            self.token_status_item,
            self.login_item,
            self.paste_item,
            self.logout_item,
        ]
        self._login_watch_thread: threading.Thread | None = None
        self.refresh()
        rumps.Timer(self.refresh, POLL_SECONDS).start()

    # -- polling -----------------------------------------------------------

    def refresh(self, _sender=None):
        creds = auth.read_token()
        if creds is None:
            self.title = "Clawdmeter (login)"
            self.token_status_item.title = "Not logged in"
            self._set_metric_rows(None)
            return

        days_left = auth.days_until_expiry(creds["mintedAt"])
        self.token_status_item.title = f"Logged in — token valid ~{days_left}d"

        try:
            data = usage.poll_usage(creds["token"])
        except usage.TokenInvalid:
            self.title = "Clawdmeter (login)"
            self.token_status_item.title = "Token rejected — log in again"
            self._set_metric_rows(None)
            return
        except Exception as e:  # network hiccup etc. — keep last-known display
            rumps.notification("Clawdmeter", "Poll failed", str(e)[:120])
            return

        self._set_metric_rows(data)
        self.title = self._compact_title(data)

    @staticmethod
    def _compact_title(data: dict) -> str:
        def seg(label: str, m: dict | None) -> str:
            if m is None or m.get("pct") is None:
                return f"{label}: —"
            return f"{label}: {m['pct']}%"

        return " | ".join([
            seg("S", data["five_hour"]),
            seg("W", data["weekly"]),
        ])

    def _set_metric_rows(self, data: dict | None):
        if data is None:
            self.five_hour_item.title = "5h window: —"
            self.weekly_item.title = "Weekly quota: —"
            return

        five, week = data["five_hour"], data["weekly"]
        self.five_hour_item.title = (
            f"5h window: {five['pct']}% (resets {fmt_reset(five['reset_min'])})"
            if five else "5h window: not reported"
        )
        self.weekly_item.title = (
            f"Weekly quota: {week['pct']}% (resets {fmt_reset(week['reset_min'])})"
            if week else "Weekly quota: not reported"
        )

    # -- login ---------------------------------------------------------

    def start_login(self, _sender):
        if self._login_watch_thread and self._login_watch_thread.is_alive():
            rumps.alert("Clawdmeter", "A login is already in progress in Terminal.")
            return

        out_path = Path(tempfile.gettempdir()) / f"clawdmeter-setup-token-{int(time.time())}.log"
        # Real Terminal.app window: `claude setup-token` needs a genuine
        # interactive session to open the browser and receive the OAuth
        # callback — a background subprocess from this app was NOT reliable
        # for this in testing, so we hand it to Terminal explicitly.
        script = (
            f'tell application "Terminal" to do script '
            f'"claude setup-token | tee {out_path}; echo DONE >> {out_path}"'
        )
        subprocess.run(["osascript", "-e", script], check=False)
        rumps.notification(
            "Clawdmeter", "Login started",
            "Approve in the browser Terminal opens. This app will pick up the token automatically.",
        )
        self._login_watch_thread = threading.Thread(
            target=self._watch_for_token, args=(out_path,), daemon=True
        )
        self._login_watch_thread.start()

    def _watch_for_token(self, out_path: Path):
        deadline = time.time() + LOGIN_TIMEOUT_SECONDS
        token = None
        while time.time() < deadline:
            if out_path.exists():
                text = out_path.read_text(errors="ignore")
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("sk-ant-oat"):
                        token = line
                        break
                if token or "DONE" in text:
                    break
            time.sleep(LOGIN_POLL_SECONDS)
        out_path.unlink(missing_ok=True)
        if token:
            auth.store_token(token)
            rumps.notification("Clawdmeter", "Logged in", "Token saved. Usage will update shortly.")
            self.refresh()
        else:
            rumps.notification(
                "Clawdmeter", "Login not detected",
                "If you completed it in Terminal, use 'Paste token manually…' instead.",
            )

    def paste_token(self, _sender):
        resp = rumps.Window(
            title="Paste your Claude token",
            message="Run `claude setup-token` in Terminal, then paste the printed token here.",
            ok="Save", cancel="Cancel",
        ).run()
        if resp.clicked and resp.text.strip().startswith("sk-ant-oat"):
            auth.store_token(resp.text.strip())
            self.refresh()
        elif resp.clicked:
            rumps.alert("Clawdmeter", "That doesn't look like a valid token (expected sk-ant-oat…).")

    def logout(self, _sender):
        auth.delete_token()
        self.refresh()


if __name__ == "__main__":
    ClawdmeterCompanion().run()
