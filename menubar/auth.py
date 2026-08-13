"""Keychain storage for the Clawdmeter Companion's own long-lived OAuth token.

Deliberately a SEPARATE Keychain item from "Claude Code-credentials" — this
app never reads or writes Claude Code's own session. The token here comes
from `claude setup-token` (a one-year OAuth token scoped to model requests
only) and is entered once through the menu bar UI, decoupling this app from
Claude Code CLI's much shorter-lived login session.
"""

from __future__ import annotations

import getpass
import json
import re
import subprocess
import time

KEYCHAIN_SERVICE = "Clawdmeter Companion"
TOKEN_LIFETIME_DAYS = 365  # claude setup-token mints a one-year token


def _account() -> str:
    return getpass.getuser()


def _decode_keychain_blob(raw: str) -> str:
    """Undo the hex-dump `security -w` sometimes applies (see daemon's twin)."""
    s = raw.strip()
    if s and len(s) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", s):
        try:
            return bytes.fromhex(s).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return raw
    return raw


def store_token(token: str) -> None:
    """Save the token (plus mint timestamp) to our own Keychain item.

    -U updates in place if an item with this service+account already exists,
    so re-running the login flow (e.g. token rotation) just overwrites it.
    """
    blob = json.dumps({"token": token.strip(), "mintedAt": time.time()})
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            _account(),
            "-w",
            blob,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def read_token() -> dict | None:
    """Return {"token": str, "mintedAt": float} from the Keychain, or None."""
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                _account(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    try:
        return json.loads(_decode_keychain_blob(out.stdout))
    except json.JSONDecodeError:
        return None


def delete_token() -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _account()],
        capture_output=True,
        text=True,
        timeout=10,
    )


def days_until_expiry(minted_at: float) -> int:
    elapsed_days = (time.time() - minted_at) / 86400
    return int(TOKEN_LIFETIME_DAYS - elapsed_days)
