"""Auth state capture and replay for browser-based runners.

Decision (recorded in README): runs are human-supervised, so the operator
signs in once at the start of a run group; the harness captures the session
state (cookies + localStorage) to disk and replays it for every subsequent
run, including cold-cache runs (where we clear browser state and then
restore the session before the run starts).

The captured state is local-only and never committed. .gitignore excludes
.local/ and credentials.json.

Two scopes:
  - CSD: drive.cloudsee.cloud session cookies, replay on each cold-cache run.
  - AWS Console: same pattern, with the IAM signin URL as the entry point.

This file is pure I/O and side-effect free except for filesystem reads/writes
and Selenium driver state mutations. No network calls.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .common import get_logger

log = get_logger("auth_state")

AUTH_DIR = Path(__file__).resolve().parents[1] / ".local" / "auth"


@dataclass(frozen=True)
class AuthState:
    """Captured session state for a single tool."""

    cookies: list[dict[str, Any]]
    local_storage: dict[str, str]
    session_storage: dict[str, str]
    captured_at: float  # epoch seconds


def _path_for(tool: str) -> Path:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    return AUTH_DIR / f"{tool}.json"


def save(tool: str, state: AuthState) -> None:
    p = _path_for(tool)
    p.write_text(
        json.dumps(
            {
                "cookies": state.cookies,
                "local_storage": state.local_storage,
                "session_storage": state.session_storage,
                "captured_at": state.captured_at,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info("saved auth state: %s (cookies=%d, ls=%d, ss=%d)",
             tool, len(state.cookies), len(state.local_storage), len(state.session_storage))


def load(tool: str) -> Optional[AuthState]:
    p = _path_for(tool)
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return AuthState(
        cookies=list(data.get("cookies", [])),
        local_storage=dict(data.get("local_storage", {})),
        session_storage=dict(data.get("session_storage", {})),
        captured_at=float(data.get("captured_at", 0)),
    )


# ----- Selenium-side helpers (driver may be None during tests) -----

def capture_from_driver(driver, origin_url: str) -> AuthState:
    """Capture cookies and storage for the current origin in driver.

    Caller is responsible for navigating to the origin first; this function
    just reads what's there.
    """
    cookies = list(driver.get_cookies())
    local_storage = _read_storage(driver, "localStorage")
    session_storage = _read_storage(driver, "sessionStorage")
    log.info("captured auth state from %s (cookies=%d, ls=%d, ss=%d)",
             origin_url, len(cookies), len(local_storage), len(session_storage))
    return AuthState(
        cookies=cookies,
        local_storage=local_storage,
        session_storage=session_storage,
        captured_at=time.time(),
    )


def restore_to_driver(driver, state: AuthState, origin_url: str) -> None:
    """Replay captured state into driver. Caller navigates to origin first.

    Selenium requires being on the right origin before add_cookie. We assume
    the caller already did driver.get(origin_url).
    """
    for cookie in state.cookies:
        # Selenium is picky about extra keys. Strip anything it doesn't accept.
        clean = {k: v for k, v in cookie.items() if k in {
            "name", "value", "path", "domain", "secure", "httpOnly", "expiry", "sameSite"
        }}
        try:
            driver.add_cookie(clean)
        except Exception as exc:  # noqa: BLE001 - Selenium raises a wide set
            log.warning("could not restore cookie %s: %s", clean.get("name"), exc)
    _write_storage(driver, "localStorage", state.local_storage)
    _write_storage(driver, "sessionStorage", state.session_storage)
    log.info("restored auth state into driver for %s", origin_url)


def _read_storage(driver, kind: str) -> dict[str, str]:
    script = f"""
        const out = {{}};
        for (let i = 0; i < {kind}.length; i++) {{
            const k = {kind}.key(i);
            out[k] = {kind}.getItem(k);
        }}
        return out;
    """
    try:
        return dict(driver.execute_script(script) or {})
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s: %s", kind, exc)
        return {}


def _write_storage(driver, kind: str, values: dict[str, str]) -> None:
    if not values:
        return
    for k, v in values.items():
        # Use JSON.stringify for safe quoting.
        driver.execute_script(
            f"{kind}.setItem(arguments[0], arguments[1]);",
            k,
            v,
        )


def is_logged_in(driver, sentinel_selector: str) -> bool:
    """Quick check: is a known logged-in element present?

    Each runner provides its own sentinel CSS selector (e.g., the user-menu
    avatar in CSD, or the IAM identity badge in the Console).
    """
    from selenium.common.exceptions import NoSuchElementException
    from selenium.webdriver.common.by import By

    try:
        driver.find_element(By.CSS_SELECTOR, sentinel_selector)
        return True
    except NoSuchElementException:
        return False
