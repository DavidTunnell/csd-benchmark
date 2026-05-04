"""AWS Console runner - drives console.aws.amazon.com/s3/ via Selenium.

Mirrors the locked architectural decisions used by csd_runner.py:

  - Auth: capture once, replay per cold-cache reset (auth_state.py). Console
    redirects unauthenticated requests to signin.aws.amazon.com, so a working
    cookie/storage replay keeps the harness off the IAM signin form during
    automated runs.
  - Selectors: console_resolver from harness.selectors, intent-driven with
    data-testid > aria-label > CSS fallback.
  - HTTP capture: not wired in v1 (selenium-wire is incompatible with Python
    3.14 per harness/requirements.txt). http_request_count and network_bytes
    are left None until a CDP-based capture lands.

The Console runner uses the same "attach to operator-launched Chrome" pattern
as the CSD runner because:
  1. AWS Console bot detection is aggressive against Selenium-managed Chrome.
  2. The spec calls for human supervision throughout, and the operator is
     already in the loop. Attaching to their browser is the lowest-friction
     way to keep the human and the harness on the same session.
  3. It composes naturally with the manual-handoff hotkey pattern that
     Scenarios 1-4 require (operator drives the click stream, harness times
     and counts).

Operator workflow per run group:
  1. Launch Chrome with --remote-debugging-port=9223 and a dedicated
     --user-data-dir.
  2. Sign in to https://console.aws.amazon.com/s3/ as csd-benchmark-readonly
     (no MFA per RUN_CONDITIONS.md). Leave the tab open.
  3. Run the harness: python -m harness.run_matrix --tool console ...

First run captures the operator's session cookies + storage to
.local/auth/console.json. Subsequent cold-cache runs clear the browser state
and replay that snapshot, so we never re-authenticate mid-run-group.

Status: scenarios 1 and 2 land in this slice. Scenarios 3 and 4 will follow
in their own commits. Scenario 5 short-circuits because the Console has no
tag query UI.
"""

from __future__ import annotations

import time

from selenium import webdriver as sw_webdriver

from .. import auth_state, operator
from ..common import get_logger
from ..config import (
    AWS_CONSOLE_BASE_URL,
    AWS_CONSOLE_CHROME_DEBUGGER_ADDRESS,
    IamCreds,
)
from ..results import RunResult
from ..scenarios import Scenario
from .base import Runner

log = get_logger("console_runner")


# Auth-state tool key. auth_state.save("console", ...) and load("console")
# resolve to .local/auth/console.json.
AUTH_TOOL_KEY = "console"

# Reasonable wait when verifying a freshly-attached tab is signed in.
LOGGED_IN_PROBE_TIMEOUT_SEC = 10.0

# Wait for the page to settle after a cold-cache reload + auth replay.
COLD_RESET_PROBE_TIMEOUT_SEC = 15.0


class ConsoleRunner(Runner):
    """Drive the AWS Console S3 UI by attaching to an operator-launched Chrome."""

    name = "console"

    def __init__(self, creds: IamCreds) -> None:
        self.creds = creds
        self.driver = None
        # Whether we already have a saved auth state snapshot for this tool.
        # Set True by setup() on first run (after capture) or when an existing
        # snapshot is loaded. reset_cache(cold) reads this to decide whether
        # replay is possible or whether cold should degrade to warm.
        self._auth_captured = False

    # ----- Setup / teardown / cache -----

    def setup(self) -> None:
        """Attach to operator-launched Chrome at the configured debugger address.

        The operator is responsible for launching Chrome with the right flags
        and signing in. The harness only attaches and verifies the session
        looks signed in. This mirrors csd_runner.setup so the two runners
        stay in lockstep on session management.

        On first run (no saved auth state), captures the operator's live
        cookies + storage so subsequent cold-cache runs can replay them.
        """
        log.info(
            "setup: attaching to running Chrome at %s",
            AWS_CONSOLE_CHROME_DEBUGGER_ADDRESS,
        )
        options = sw_webdriver.ChromeOptions()
        options.add_experimental_option(
            "debuggerAddress", AWS_CONSOLE_CHROME_DEBUGGER_ADDRESS
        )
        try:
            self.driver = sw_webdriver.Chrome(options=options)
        except Exception as exc:
            msg = (
                "could not attach to Chrome at "
                + AWS_CONSOLE_CHROME_DEBUGGER_ADDRESS
                + "; make sure Chrome is running with --remote-debugging-port=9223 "
                  "and you have signed in to the AWS Console as the "
                  "csd-benchmark-readonly IAM user. See harness/tools/console_runner.py "
                  "module docstring."
            )
            raise RuntimeError(msg) from exc

        # If the attached tab is somewhere else (a CSD tab, a fresh new-tab
        # page, etc.), point it at the S3 console root so the signed-in
        # check has something concrete to evaluate.
        current = ""
        try:
            current = self.driver.current_url or ""
        except Exception:
            current = ""
        if "console.aws.amazon.com" not in current:
            log.info(
                "attached tab is on %r; navigating to %s",
                current,
                AWS_CONSOLE_BASE_URL,
            )
            self.driver.get(AWS_CONSOLE_BASE_URL)

        if not self._is_logged_in(timeout=LOGGED_IN_PROBE_TIMEOUT_SEC):
            raise RuntimeError(
                "attached Chrome does not appear to be signed in to the AWS Console; "
                "sign in manually as csd-benchmark-readonly in the Chrome window "
                "(no MFA per RUN_CONDITIONS.md), then re-run."
            )

        operator.inject_click_counter(self.driver)

        # First-run snapshot: if we don't have a saved auth state for the
        # console yet, capture the operator's live session now so cold-cache
        # resets can replay it. Re-capturing each setup() would be safer but
        # also overwrites a known-good snapshot with a possibly-stale one,
        # so we prefer "capture once, replay until it expires."
        existing = auth_state.load(AUTH_TOOL_KEY)
        if existing is None:
            log.info(
                "no saved auth state for %s; capturing the operator's live session",
                AUTH_TOOL_KEY,
            )
            try:
                state = auth_state.capture_from_driver(self.driver, AWS_CONSOLE_BASE_URL)
                auth_state.save(AUTH_TOOL_KEY, state)
                self._auth_captured = True
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "could not capture auth state for %s: %s; "
                    "cold-cache resets will degrade to warm",
                    AUTH_TOOL_KEY,
                    exc,
                )
                self._auth_captured = False
        else:
            log.info(
                "loaded existing auth state for %s (captured_at=%s)",
                AUTH_TOOL_KEY,
                existing.captured_at,
            )
            self._auth_captured = True

        log.info("setup complete; attached to live Chrome session")

    def teardown(self) -> None:
        """Detach from the attached browser without killing it.

        Same rationale as csd_runner.teardown: the Chrome was launched by the
        operator. driver.quit() would close the operator's window, which is
        not what we want. We just drop our reference; the Chrome stays open
        for the next run group or for the operator's manual use.
        """
        log.info("teardown: detaching from Chrome (window stays open)")
        self.driver = None
        self._auth_captured = False

    def reset_cache(self, state: str) -> None:
        """Bring the attached browser into the requested cache state.

        cold: navigate to the Console origin, clear cookies + localStorage +
        sessionStorage, then replay the captured auth state. After replay
        we reload and verify the session is signed-in; if not, the cold
        snapshot is stale and we surface a loud warning so the operator
        re-authenticates and re-captures.

        warm: no-op. Page state from the previous run carries over.
        """
        if self.driver is None:
            log.warning("reset_cache(%s): no driver attached, skipping", state)
            return

        if state != "cold":
            log.info("reset_cache(warm): no-op")
            return

        if not self._auth_captured:
            log.warning(
                "reset_cache(cold): no captured auth state available; "
                "skipping clear+replay so we don't sign the operator out. "
                "This run is effectively warm-cache; the result row will "
                "still be tagged 'cold' as the operator requested it."
            )
            return

        log.info("reset_cache(cold): clearing browser state and replaying auth")

        # 1. Navigate to the Console origin so cookies for that domain are
        #    in scope for delete_all_cookies(). Selenium scopes cookie
        #    deletion to the current page's origin.
        try:
            self.driver.get(AWS_CONSOLE_BASE_URL)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not navigate to console origin: %s", exc)

        # 2. Clear cookies + storage. We swallow exceptions on the storage
        #    clear because some Console pages briefly run on an opaque origin
        #    where storage access throws SecurityError; the cookie clear is
        #    the load-bearing step.
        try:
            self.driver.delete_all_cookies()
        except Exception as exc:  # noqa: BLE001
            log.warning("delete_all_cookies failed: %s", exc)
        try:
            self.driver.execute_script(
                "try { localStorage.clear(); } catch (e) {} "
                "try { sessionStorage.clear(); } catch (e) {}"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("storage clear failed: %s", exc)

        # 3. Replay the captured auth state. restore_to_driver assumes we
        #    have already navigated to the origin, which we did in step 1.
        loaded = auth_state.load(AUTH_TOOL_KEY)
        if loaded is None:
            log.warning(
                "auth state file disappeared between setup and reset; "
                "re-run setup() or sign in again"
            )
            self._auth_captured = False
            return
        try:
            auth_state.restore_to_driver(self.driver, loaded, AWS_CONSOLE_BASE_URL)
        except Exception as exc:  # noqa: BLE001
            log.warning("restore_to_driver failed: %s", exc)
            return

        # 4. Reload so the page picks up the replayed cookies + storage and
        #    re-injects the click counter on the fresh DOM.
        try:
            self.driver.get(AWS_CONSOLE_BASE_URL)
        except Exception as exc:  # noqa: BLE001
            log.warning("post-replay navigation failed: %s", exc)

        # 5. Verify the replay actually got us to a signed-in page. If not,
        #    the snapshot has expired; flag _auth_captured False so future
        #    cold resets degrade to warm with a warning rather than logging
        #    out the operator silently.
        if not self._is_logged_in(timeout=COLD_RESET_PROBE_TIMEOUT_SEC):
            log.warning(
                "cold-cache replay did not produce a signed-in session; "
                "saved auth state for %s appears stale. Re-authenticate "
                "manually in the Chrome window and re-run setup() to "
                "re-capture.",
                AUTH_TOOL_KEY,
            )
            self._auth_captured = False
            return

        operator.inject_click_counter(self.driver)
        log.info("reset_cache(cold): clear + replay verified signed-in")

    # ----- Per-run -----

    def run(self, scenario: Scenario, run_id: str, cache_state: str) -> RunResult:
        """Execute one run of one scenario.

        Scenarios 1, 2, 3, 4 follow in their own commits. Scenario 5 always
        short-circuits because the Console has no tag query UI.
        """
        log.info("run: scenario=%s cache=%s (scaffold)", scenario.name, cache_state)

        if scenario.id == 5:
            return RunResult(
                run_id=run_id,
                started_at="",
                tool=self.name,
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                cache_state=cache_state,
                operator_skill=scenario.operator_skill_by_tool[self.name],
                time_to_result_sec=None,
                completed_within_cap=False,
                non_technical_user_could_complete=False,
                result_correct=False,
                notes=(
                    "AWS Console has no tag query UI for S3 object tags. "
                    "Cannot complete by design (per RUN_MATRIX.md)."
                ),
            )

        # Scenarios 1-4: implementations land in their own commits.
        return RunResult(
            run_id=run_id,
            started_at="",
            tool=self.name,
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            cache_state=cache_state,
            operator_skill=scenario.operator_skill_by_tool[self.name],
            time_to_result_sec=None,
            completed_within_cap=False,
            non_technical_user_could_complete=False,
            result_correct=False,
            notes="not yet implemented in this commit",
        )

    # ----- Internal helpers -----

    def _is_logged_in(self, timeout: float = LOGGED_IN_PROBE_TIMEOUT_SEC) -> bool:
        """Best-effort signed-in check.

        The Console redirects unauthenticated requests to signin.aws.amazon.com.
        We treat the session as signed-in when:
          - the current URL is on console.aws.amazon.com (not the signin host)
          - the page body has rendered (non-empty innerText)

        We poll for up to `timeout` seconds because navigation may still be
        completing when this is called from setup() or reset_cache().
        """
        if self.driver is None:
            return False
        end = time.time() + timeout
        last_err = None
        while time.time() < end:
            url = ""
            try:
                url = (self.driver.current_url or "").lower()
            except Exception as exc:  # noqa: BLE001 - Selenium raises broadly
                last_err = exc
            on_signin = "signin.aws.amazon.com" in url or "/signin?" in url
            on_console = "console.aws.amazon.com" in url
            body_loaded = False
            try:
                body_loaded = bool(
                    self.driver.execute_script(
                        "return !!(document.body && "
                        "document.body.innerText && "
                        "document.body.innerText.length > 0);"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                body_loaded = False
            if on_console and not on_signin and body_loaded:
                return True
            time.sleep(0.5)
        if last_err is not None:
            log.debug("logged-in probe last error: %s", last_err)
        return False
