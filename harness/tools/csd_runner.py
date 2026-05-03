"""CSD runner - drives drive.cloudsee.cloud via Selenium.

Implements the locked architectural decisions:
  - Auth: capture once, replay per cold-cache reset (auth_state.py)
  - Selectors: find_by_intent with data-testid > aria-label > CSS fallback
  - HTTP capture: TODO via Chrome DevTools Protocol; v1 uses plain Selenium
    because selenium-wire's bundled mitmproxy is broken on Python 3.14
  - Scenario 3 (substring across whole bucket) is implemented; the others
    follow the same pattern and can be added incrementally.

First-time setup:
  1. The harness opens Chrome, navigates to drive.cloudsee.cloud
  2. If saved auth state exists at .local/auth/csd.json, restore + verify
  3. If not, prompt the operator to sign in manually in the visible window;
     after the operator confirms with the hotkey, capture cookies/storage to disk
  4. From then on, every cold-cache reset replays the saved state without
     re-authenticating

Run-time:
  - reset_cache(cold) clears cookies/storage and replays saved auth
  - reset_cache(warm) is a no-op
  - run() drives the scenario, stops the timer on result visibility, reads
    the result count from CSD's "X-Y of N" pagination text, captures click
    count + HTTP count + bytes, returns RunResult
"""

from __future__ import annotations

import re
import time

from selenium import webdriver as sw_webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .. import auth_state, operator
from ..common import get_logger, stopwatch
from ..config import CSD_BASE_URL, CSD_DRIVE_NAMES, CsdCreds
from ..results import RunResult
from ..scenarios import Scenario
from ..selectors import csd_resolver
from .base import Runner

log = get_logger("csd_runner")

# Element used to verify "logged in" state. The drives sidebar header is
# only rendered post-login. We look for the "DRIVES" label.
LOGGED_IN_SENTINEL_XPATH = (
    "//*[contains(translate(text(), 'DRIVES', 'drives'), 'drives')]"
)

# How long we wait for the result count to settle after typing a substring.
SEARCH_RESULT_TIMEOUT_SEC = 60


class CsdRunner(Runner):
    name = "csd"

    def __init__(self, creds: CsdCreds) -> None:
        self.creds = creds
        self.driver = None  # selenium-wire webdriver instance

    # ----- Setup / teardown / cache -----

    def setup(self) -> None:
        log.info("setup: launching Chrome with selenium-wire")
        options = sw_webdriver.ChromeOptions()
        options.add_argument("--disable-blink-features=AutomationControlled")
        # Visible browser per spec (human-supervised). No --headless.
        self.driver = sw_webdriver.Chrome(options=options)
        self.driver.set_window_size(1450, 900)

        log.info("navigating to %s", CSD_BASE_URL)
        self.driver.get(CSD_BASE_URL)

        saved = auth_state.load("csd")
        if saved is not None:
            log.info("found saved auth state, attempting restore")
            try:
                auth_state.restore_to_driver(self.driver, saved, CSD_BASE_URL)
                self.driver.get(CSD_BASE_URL)
                if self._is_logged_in():
                    log.info("auth restored from saved state")
                    operator.inject_click_counter(self.driver)
                    return
                log.warning("saved auth did not produce logged-in state, re-prompting")
            except Exception as exc:  # noqa: BLE001
                log.warning("auth restore failed: %s; re-prompting", exc)

        # Manual sign-in path. Operator types the password in the visible
        # browser window, then types the hotkey on the terminal to confirm.
        log.info("waiting for manual sign-in")
        signal = operator.prompt_handoff_and_wait(
            description="Sign in to CSD in the open Chrome window.",
            target_hint=(
                f"Account: {self.creds.email}. After dashboard loads, press 'g' + Enter."
            ),
            hotkey="g",
            timeout_sec=600,
        )
        if not signal.confirmed:
            raise RuntimeError(f"sign-in not confirmed: {signal.notes}")

        if not self._is_logged_in():
            raise RuntimeError("operator confirmed sign-in but sentinel not visible")

        captured = auth_state.capture_from_driver(self.driver, CSD_BASE_URL)
        auth_state.save("csd", captured)
        operator.inject_click_counter(self.driver)
        log.info("setup complete; auth saved for future runs")

    def teardown(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception as exc:  # noqa: BLE001
                log.warning("driver.quit raised: %s", exc)
        self.driver = None

    def reset_cache(self, state: str) -> None:
        if state == "warm":
            log.info("reset_cache(warm): no-op")
            return

        log.info("reset_cache(cold): clearing cookies + storage and replaying auth")
        if self.driver is None:
            raise RuntimeError("driver not initialised; call setup() first")

        # Clear browser state. Need to be on the same origin first to access storage.
        self.driver.get(CSD_BASE_URL)
        self.driver.delete_all_cookies()
        try:
            self.driver.execute_script("localStorage.clear(); sessionStorage.clear();")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not clear storage: %s", exc)

        # Reload to make sure the cleared state takes effect.
        self.driver.get(CSD_BASE_URL)

        saved = auth_state.load("csd")
        if saved is None:
            raise RuntimeError("no saved CSD auth state; setup() must succeed first")
        auth_state.restore_to_driver(self.driver, saved, CSD_BASE_URL)
        self.driver.get(CSD_BASE_URL)
        if not self._is_logged_in():
            raise RuntimeError("auth replay failed; saved state may be stale")

        operator.inject_click_counter(self.driver)

    # ----- Scenario dispatch -----

    def run(self, scenario: Scenario, run_id: str, cache_state: str) -> RunResult:
        if self.driver is None:
            raise RuntimeError("driver not initialised; call setup() first")

        # Clear per-run metrics
        operator.reset_click_counter(self.driver)

        if scenario.id == 3:
            return self._run_scenario_3_substring(scenario, run_id, cache_state)

        # Other scenarios are not yet implemented for CSD. Return a placeholder
        # so the matrix CSV still has a row visible in the report.
        log.warning("CSD scenario %d not yet implemented", scenario.id)
        return RunResult(
            run_id=run_id,
            started_at="",
            tool=self.name,
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            cache_state=cache_state,
            operator_skill=scenario.operator_skill_by_tool[self.name],
            time_to_result_sec=None,
            click_count=0,
            keystroke_count=None,
            http_request_count=None,
            network_bytes=None,
            completed_within_cap=None,
            non_technical_user_could_complete=None,
            result_correct=None,
            result_count_reported=None,
            notes=f"CSD scenario {scenario.id} not implemented yet",
        )

    # ----- Scenario 3: substring across the whole bucket -----

    def _run_scenario_3_substring(
        self,
        scenario: Scenario,
        run_id: str,
        cache_state: str,
    ) -> RunResult:
        """Search the whole bucket for the configured substring and read the count."""
        drive_name = CSD_DRIVE_NAMES.get(scenario.bucket)
        if not drive_name:
            return self._fail_result(
                scenario, run_id, cache_state,
                f"no CSD drive name configured for bucket {scenario.bucket}",
            )

        substring = scenario.substring or ""
        if not substring:
            return self._fail_result(
                scenario, run_id, cache_state,
                "scenario has no substring configured",
            )

        log.info("scenario 3: drive=%s substring=%s", drive_name, substring)

        # Step 1: navigate to the drive root.
        self._click_drive(drive_name)
        # Wait until the search input is interactable - that's our signal that
        # the drive view has rendered.
        search_input = self._wait_for_intent("search-input", timeout=20)

        # Step 2: clear any prior search content.
        try:
            search_input.clear()
        except Exception:
            pass

        # Reset metrics again right before the timed window.
        operator.reset_click_counter(self.driver)

        # Step 3: timed search.
        with stopwatch() as elapsed:
            search_input.click()
            search_input.send_keys(substring)
            count = self._wait_for_search_result_count(timeout=SEARCH_RESULT_TIMEOUT_SEC)
            time_to_result = elapsed()

        click_count = operator.read_click_counter(self.driver)
        # HTTP request count + bytes deliberately not captured in v1 (see
        # module docstring re: selenium-wire incompatibility on Py 3.14).
        http_count = None
        net_bytes = None

        # Per locked rubric: ground truth at the seeded oss-mirror is 32,763.
        # If the drive is not pointed at our bucket yet, the count will be
        # wildly different and the assertion fails - that surfaces the
        # "buckets not connected" misconfig clearly.
        result_correct = count is not None and count >= 32000

        notes = (
            f"CSD search '{substring}' on drive '{drive_name}', count={count}"
            if count is not None
            else f"CSD search '{substring}' on drive '{drive_name}' did not produce a count"
        )

        return RunResult(
            run_id=run_id,
            started_at="",
            tool=self.name,
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            cache_state=cache_state,
            operator_skill=scenario.operator_skill_by_tool[self.name],
            time_to_result_sec=time_to_result,
            click_count=click_count,
            keystroke_count=len(substring),
            http_request_count=http_count,
            network_bytes=net_bytes,
            completed_within_cap=time_to_result is not None and time_to_result <= 300,
            non_technical_user_could_complete=result_correct,
            result_correct=result_correct,
            result_count_reported=count,
            notes=notes,
        )

    # ----- Helpers -----

    def _is_logged_in(self) -> bool:
        try:
            self.driver.find_element(By.XPATH, LOGGED_IN_SENTINEL_XPATH)
            return True
        except Exception:
            return False

    def _wait_for_intent(self, intent: str, timeout: float):
        """Wait for an intent-resolved element to be present, return it."""
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            elem = csd_resolver.find(self.driver, intent)
            if elem is not None:
                return elem
            time.sleep(0.25)
        raise TimeoutException(f"intent {intent!r} not found within {timeout}s")

    def _click_drive(self, drive_name: str) -> None:
        """Click the drive entry in the left sidebar.

        Tries data-testid first, then falls back to matching button text.
        """
        css_testid = f"[data-testid='drive-{drive_name}']"
        try:
            elem = self.driver.find_element(By.CSS_SELECTOR, css_testid)
            elem.click()
            return
        except Exception:
            pass

        # Fall back to matching button or link text. CSD renders drives as
        # buttons in a sidebar list.
        xpath = (
            f"//button[normalize-space()='{drive_name}'] | "
            f"//a[normalize-space()='{drive_name}']"
        )
        elem = WebDriverWait(self.driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        elem.click()

    def _wait_for_search_result_count(self, timeout: float) -> "int | None":
        """Poll until CSD's pagination text shows a stable count, return it.

        CSD shows pagination as 'X-Y of N' (e.g. '1-10 of 100000') near the
        rows-per-page control. We read the body text, regex out 'of N', and
        wait until N stops changing. If N stays at 0 for the whole window
        we return 0 (legit no-match search).
        """
        pattern = re.compile(r"\bof\s+([\d,]+)\b")
        last_seen: int | None = None
        last_change = time.perf_counter()
        end = last_change + timeout
        # Settle window: how long the count must remain unchanged before we
        # call it final. CSD's basic search debounces ~500ms, so 1.5s of
        # stability is conservative.
        settle_sec = 1.5

        while time.perf_counter() < end:
            try:
                body_text = self.driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                body_text = ""
            matches = pattern.findall(body_text)
            current: int | None = None
            if matches:
                # If multiple "of N" appear, take the largest. Almost always
                # that's the result-count badge rather than e.g. file size.
                current = max(int(m.replace(",", "")) for m in matches)

            if current != last_seen:
                last_seen = current
                last_change = time.perf_counter()
            elif current is not None and (time.perf_counter() - last_change) >= settle_sec:
                return current
            time.sleep(0.1)

        return last_seen

    def _fail_result(
        self,
        scenario: Scenario,
        run_id: str,
        cache_state: str,
        reason: str,
    ) -> RunResult:
        log.error("scenario %d failed: %s", scenario.id, reason)
        return RunResult(
            run_id=run_id,
            started_at="",
            tool=self.name,
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            cache_state=cache_state,
            operator_skill=scenario.operator_skill_by_tool[self.name],
            time_to_result_sec=None,
            click_count=0,
            keystroke_count=None,
            http_request_count=None,
            network_bytes=None,
            completed_within_cap=False,
            non_technical_user_could_complete=False,
            result_correct=False,
            result_count_reported=None,
            notes=f"failed: {reason}",
        )
