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
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .. import operator
from ..common import get_logger, stopwatch
from ..config import (
    CSD_BASE_URL,
    CSD_CHROME_DEBUGGER_ADDRESS,
    CSD_DRIVE_NAMES,
    CsdCreds,
)
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
        """Attach to a Chrome already running at CSD_CHROME_DEBUGGER_ADDRESS.

        The operator launches Chrome separately with --remote-debugging-port
        and signs into CSD once. Selenium attaches to that running browser
        instead of launching a new one, which avoids the Selenium-managed
        Chrome environment that prevents CSD's debounced search from firing.
        """
        log.info("setup: attaching to running Chrome at %s", CSD_CHROME_DEBUGGER_ADDRESS)
        options = sw_webdriver.ChromeOptions()
        options.add_experimental_option("debuggerAddress", CSD_CHROME_DEBUGGER_ADDRESS)
        try:
            self.driver = sw_webdriver.Chrome(options=options)
        except Exception as exc:
            raise RuntimeError(
                f"could not attach to Chrome at {CSD_CHROME_DEBUGGER_ADDRESS}; "
                "make sure Chrome is running with --remote-debugging-port=9222 "
                "and you have signed in to CSD. See harness/README.md."
            ) from exc

        # Navigate to CSD if we are not already on a CSD page.
        current = ""
        try:
            current = self.driver.current_url
        except Exception:
            pass
        if "cloudsee.cloud" not in current:
            log.info("attached tab is on %s; navigating to %s", current, CSD_BASE_URL)
            self.driver.get(CSD_BASE_URL)
        else:
            log.info("attached tab already on %s", current)

        if not self._is_logged_in(timeout=10):
            raise RuntimeError(
                "attached Chrome does not appear to be signed in to CSD; "
                "sign in manually in the Chrome window first, then re-run."
            )

        operator.inject_click_counter(self.driver)
        log.info("setup complete; attached to live Chrome session")

    def teardown(self) -> None:
        """Detach from the attached browser without killing it.

        We attached to a Chrome the operator launched. Calling driver.quit()
        on an attached session would close that operator's Chrome, which is
        not what we want. We just drop our reference; the Chrome stays open
        for the next run group or for manual use.
        """
        # Selenium 4's quit() on an attached session does try to close
        # the browser. We avoid that by simply forgetting the driver.
        self.driver = None

    def reset_cache(self, state: str) -> None:
        """Cache reset for the attached-Chrome model.

        Cold cache: clear browser cache via CDP. We do NOT clear cookies
        or storage because that would log the operator out of the attached
        Chrome and break subsequent runs in the same group. Cold/warm
        in this model is therefore "browser HTTP cache cleared vs not";
        the auth session and any indexed-search prewarm stay intact.

        Warm cache: no-op.

        This is a softer cold-cache than the spec's original definition.
        We document it in RUN_CONDITIONS.md so the comparison stays honest:
        all three tools (CSD, Console, CLI) have the same auth/session
        warmth between cold and warm, only the network cache differs.
        """
        if state == "warm":
            log.info("reset_cache(warm): no-op")
            return
        if self.driver is None:
            raise RuntimeError("driver not initialised; call setup() first")

        log.info("reset_cache(cold): clearing browser HTTP cache via CDP")
        try:
            self.driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        except Exception as exc:  # noqa: BLE001
            log.warning("Network.clearBrowserCache failed: %s", exc)

        # Reload the page to fetch fresh assets.
        self.driver.get(CSD_BASE_URL)
        if not self._is_logged_in(timeout=15):
            raise RuntimeError(
                "no longer signed in after reload; re-authenticate in the attached Chrome"
            )
        operator.inject_click_counter(self.driver)

    # ----- Scenario dispatch -----

    def run(self, scenario: Scenario, run_id: str, cache_state: str) -> RunResult:
        if self.driver is None:
            raise RuntimeError("driver not initialised; call setup() first")

        # Clear per-run metrics
        operator.reset_click_counter(self.driver)

        # Scenarios 1, 2, 3 all share the same UX: click drive, type a
        # filename or substring into the basic search box, read the count.
        # The runner branches inside _run_substring_scenario based on what
        # to type and how to validate, but the timing path is identical
        # so the comparison stays apples-to-apples.
        if scenario.id in (1, 2, 3):
            return self._run_substring_scenario(scenario, run_id, cache_state)

        # Scenario 4: date range via Advanced Search > Filters.
        if scenario.id == 4:
            return self._run_scenario_4_date_range(scenario, run_id, cache_state)

        # Scenario 5: tag search via Advanced Search > Tag Explorer.
        if scenario.id == 5:
            return self._run_scenario_5_tag_search(scenario, run_id, cache_state)

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

    # ----- Per-scenario search-string + validation rubric -----

    def _search_string_for(self, scenario: Scenario) -> str:
        """Return the substring a non-technical user would type for this scenario.

        S1: user knows the filename, types a distinctive part. We pick the
            'needle' prefix on the seeded needle file - unique, minimal.
        S2: user knows the deep file, types the filename without extension.
        S3: substring is configured directly on the scenario.
        """
        if scenario.id == 3:
            return scenario.substring or ""
        if scenario.id == 1 and scenario.target_key:
            # target_key is the file basename in the flat bucket. Type the
            # 'needle-quarterly' portion - what a user would actually recall.
            return "needle-quarterly"
        if scenario.id == 2 and scenario.target_key:
            # target_key is a deep path. Type the filename only.
            basename = scenario.target_key.rsplit("/", 1)[-1]  # imx8mq-evk.dts
            # Drop the extension - users typically know the name part.
            return basename.rsplit(".", 1)[0]  # imx8mq-evk
        return ""

    def _validate_count(self, scenario: Scenario, count: "int | None") -> bool:
        """Per-scenario rubric: did the search return the right answer?

        S1: exact-1 match (the seeded needle file).
        S2: at least 1 match (the deep file).
        S3: 10000+ matches (the 'test' substring sweep).
        S4: 100000+ matches (date range covering the seeding window;
            see CSD_S4_DATE_FROM/TO and RUN_CONDITIONS.md for details).
        S5: 100+ matches (seeded domain=audio count is 3060;
            slack lets small drift through if tagging re-runs).
        """
        if count is None:
            return False
        if scenario.id == 1:
            return count == 1
        if scenario.id == 2:
            return count >= 1
        if scenario.id == 3:
            return count >= 10000
        if scenario.id == 4:
            return count >= 100000
        if scenario.id == 5:
            return count >= 100
        return False

    def _run_substring_scenario(
        self,
        scenario: Scenario,
        run_id: str,
        cache_state: str,
    ) -> RunResult:
        """Click drive, type substring into basic search, read result count.

        Shared implementation for scenarios 1, 2, 3 - all three are 'user
        knows what they're looking for and types it'. The substring and the
        validation rubric vary per scenario; the timed path does not.
        """
        drive_name = CSD_DRIVE_NAMES.get(scenario.bucket)
        if not drive_name:
            return self._fail_result(
                scenario, run_id, cache_state,
                f"no CSD drive name configured for bucket {scenario.bucket}",
            )

        substring = self._search_string_for(scenario)
        if not substring:
            return self._fail_result(
                scenario, run_id, cache_state,
                "scenario has no usable search string",
            )

        log.info("scenario %d: drive=%s substring=%s", scenario.id, drive_name, substring)

        # Step 0 (fix #1): dismiss any leftover search state from the prior run
        # before we navigate. Without this, warm runs land on the previous
        # drive's results view with the search chip still rendered and the
        # search input value stuck. The next click_drive then no-ops because
        # we're already on the drive, leaving stale state in place.
        self._dismiss_search_state()

        # Step 1: navigate to the drive root.
        # Fix #2: if the click misses every strategy, abort the run cleanly
        # rather than recording a 35s timeout against a stale baseline count.
        try:
            self._click_drive(drive_name)
        except TimeoutException as exc:
            return self._fail_result(
                scenario, run_id, cache_state,
                f"setup_failed: drive click missed - {exc}",
            )
        # MUI dialogs and drawers leave invisible backdrops in the DOM during
        # transitions; they intercept clicks even at opacity 0. Wait for the
        # backdrop to be gone before interacting with the search input.
        self._wait_for_backdrop_clear(timeout=15)

        # Fix #3: if a stale chip survived the click_drive (because we were
        # already on the drive and the click was a no-op), refresh the page
        # once and re-click. After refresh, if state is *still* stale, fail
        # clean instead of measuring against bad state.
        if self._search_state_is_stale():
            log.warning("stale search state detected after click_drive; refreshing once")
            try:
                self.driver.refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh failed: %s", exc)
            if not self._is_logged_in(timeout=15):
                return self._fail_result(
                    scenario, run_id, cache_state,
                    "setup_failed: not signed in after refresh fallback",
                )
            self._dismiss_search_state()
            try:
                self._click_drive(drive_name)
            except TimeoutException as exc:
                return self._fail_result(
                    scenario, run_id, cache_state,
                    f"setup_failed: drive click missed after refresh - {exc}",
                )
            self._wait_for_backdrop_clear(timeout=15)
            if self._search_state_is_stale():
                return self._fail_result(
                    scenario, run_id, cache_state,
                    "setup_failed: search state still stale after refresh",
                )

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

        # Capture pre-search count so the wait can detect when the search
        # actually fires (the count value moves off this baseline).
        baseline_count = self._read_pagination_count()
        log.info("pre-search pagination count = %s", baseline_count)

        # Step 3: timed search.
        # We type via Chrome DevTools Protocol so the keystrokes look real
        # to CSD's MUI search input. The wait then watches for the count
        # to change off the baseline, which is how we detect the search
        # has actually fired.
        with stopwatch() as elapsed:
            ActionChains(self.driver).move_to_element(search_input).click().perform()
            for ch in substring:
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyDown", "text": ch
                })
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyUp", "text": ch
                })
            count = self._wait_for_search_result_count(
                timeout=SEARCH_RESULT_TIMEOUT_SEC,
                baseline=baseline_count,
            )
            time_to_result = elapsed()


        click_count = operator.read_click_counter(self.driver)
        # HTTP request count + bytes deliberately not captured in v1 (see
        # module docstring re: selenium-wire incompatibility on Py 3.14).
        http_count = None
        net_bytes = None

        # Per-scenario validation rubric. CSD and the CLI define "match"
        # differently for scenario 3 (CSD matches by filename, CLI grep by
        # full key); for scenarios 1 and 2 both tools are looking for the
        # same known file so the rubric is exact-1 / at-least-1.
        result_correct = self._validate_count(scenario, count)

        if scenario.id == 3:
            match_note = (
                "CSD matches by filename; CLI grep on s3 ls matches by full key"
            )
        else:
            match_note = "CSD substring match against filename"
        notes = (
            f"CSD search '{substring}' on drive '{drive_name}', count={count} "
            f"({match_note})"
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

    # ----- Scenario 4: date range via Advanced Search > Filters -----
    #
    # CSD's "Date Updated" filter reads the S3 object's LastModified, not the
    # source-mtime metadata our seeders captured. Our seeded files were all
    # uploaded in 2026 during seeding, so a 2024 range (the spec's original
    # date) returns 0 in CSD even though the inventory CSV has 2024 source
    # mtimes. We use the seeding-window range here so both CSD and CLI return
    # meaningful counts; this is documented in RUN_CONDITIONS.md as a per-tool
    # date-source difference. The headline UX claim (date range filter that
    # a non-technical user can use) doesn't depend on the specific range.

    CSD_S4_DATE_FROM = "01/01/2025"
    CSD_S4_DATE_TO = "12/31/2026"

    def _run_scenario_4_date_range(
        self,
        scenario: Scenario,
        run_id: str,
        cache_state: str,
    ) -> RunResult:
        """Open Advanced Search > Filters, set Date Updated is between, apply, read count."""
        drive_name = CSD_DRIVE_NAMES.get(scenario.bucket)
        if not drive_name:
            return self._fail_result(
                scenario, run_id, cache_state,
                f"no CSD drive name configured for bucket {scenario.bucket}",
            )

        log.info("scenario 4: drive=%s date_range=%s..%s",
                 drive_name, self.CSD_S4_DATE_FROM, self.CSD_S4_DATE_TO)

        # Reset state and navigate.
        self._dismiss_search_state()
        try:
            self._click_drive(drive_name)
        except TimeoutException as exc:
            return self._fail_result(
                scenario, run_id, cache_state,
                f"setup_failed: drive click missed - {exc}",
            )
        self._wait_for_backdrop_clear(timeout=15)

        # Refresh fallback if state is stale.
        if self._search_state_is_stale():
            log.warning("stale search state detected; refreshing")
            try:
                self.driver.refresh()
            except Exception:
                pass
            if not self._is_logged_in(timeout=15):
                return self._fail_result(
                    scenario, run_id, cache_state,
                    "setup_failed: not signed in after refresh",
                )
            self._dismiss_search_state()
            try:
                self._click_drive(drive_name)
            except TimeoutException as exc:
                return self._fail_result(
                    scenario, run_id, cache_state,
                    f"setup_failed: drive click missed after refresh - {exc}",
                )
            self._wait_for_backdrop_clear(timeout=15)

        operator.reset_click_counter(self.driver)
        baseline_count = self._read_pagination_count()
        log.info("pre-filter pagination count = %s", baseline_count)

        # Timed window: open Advanced Search, pick Filters, fill the form,
        # apply, wait for the count to settle. This mirrors the actual user
        # workflow end-to-end.
        with stopwatch() as elapsed:
            current_step = "init"
            try:
                current_step = "open_filters_dialog"
                self._open_filters_dialog()
                current_step = "select_criteria"
                self._select_filter_criteria("Date Updated")
                current_step = "select_operator"
                self._select_filter_operator("is between")
                current_step = "type_date_range"
                self._type_date_range(self.CSD_S4_DATE_FROM, self.CSD_S4_DATE_TO)
                current_step = "click_apply"
                self._click_apply_filters()
                current_step = "wait_count"
                count = self._wait_for_search_result_count(
                    timeout=SEARCH_RESULT_TIMEOUT_SEC,
                    baseline=baseline_count,
                )
            except Exception as exc:  # noqa: BLE001
                time_to_result = elapsed()
                exc_type = type(exc).__name__
                msg = str(exc)[:200].replace("\n", " ")
                log.error("scenario 4 failed in step=%s exc=%s msg=%s",
                          current_step, exc_type, msg)
                try:
                    from pathlib import Path as _P
                    _P("results").mkdir(parents=True, exist_ok=True)
                    self.driver.save_screenshot(f"results/debug-s4-fail-{current_step}.png")
                except Exception:
                    pass
                return self._fail_result(
                    scenario, run_id, cache_state,
                    f"scenario 4 step={current_step} {exc_type}: {msg}",
                )
            time_to_result = elapsed()

        click_count = operator.read_click_counter(self.driver)
        # Keystrokes: dates we typed, e.g. 10 chars each.
        keystroke_count = len(self.CSD_S4_DATE_FROM) + len(self.CSD_S4_DATE_TO)

        result_correct = self._validate_count(scenario, count)
        notes = (
            f"CSD Filters: Date Updated is between {self.CSD_S4_DATE_FROM}..{self.CSD_S4_DATE_TO}, "
            f"count={count} (S3 LastModified; CLI runs source-mtime semantic per RUN_CONDITIONS.md)"
            if count is not None
            else f"CSD Filters: Date Updated is between {self.CSD_S4_DATE_FROM}..{self.CSD_S4_DATE_TO} did not produce a count"
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
            keystroke_count=keystroke_count,
            http_request_count=None,
            network_bytes=None,
            completed_within_cap=time_to_result is not None and time_to_result <= 300,
            non_technical_user_could_complete=result_correct,
            result_correct=result_correct,
            result_count_reported=count,
            notes=notes,
        )

    # ----- Scenario 5: tag search via Advanced Search > Tag Explorer -----
    #
    # The Tag Explorer dialog shows all tag KEYS at top level (e.g. 'domain'),
    # each with a chevron. Clicking the key expands its VALUES (e.g. 'audio',
    # 'image'). Clicking a value toggles its selection. Apply runs the filter.
    # Spec scenario 5: domain=audio. Seeded count = 3060.

    def _run_scenario_5_tag_search(
        self,
        scenario: Scenario,
        run_id: str,
        cache_state: str,
    ) -> RunResult:
        """Open Tag Explorer, expand the tag key, select the value, apply, read count."""
        drive_name = CSD_DRIVE_NAMES.get(scenario.bucket)
        if not drive_name:
            return self._fail_result(
                scenario, run_id, cache_state,
                f"no CSD drive name configured for bucket {scenario.bucket}",
            )

        tag_key = scenario.tag_key or ""
        tag_value = scenario.tag_value or ""
        if not tag_key or not tag_value:
            return self._fail_result(
                scenario, run_id, cache_state,
                "scenario 5 has no tag_key/tag_value configured",
            )

        log.info("scenario 5: drive=%s tag=%s=%s", drive_name, tag_key, tag_value)

        # Reset state, navigate.
        self._dismiss_search_state()
        try:
            self._click_drive(drive_name)
        except TimeoutException as exc:
            return self._fail_result(
                scenario, run_id, cache_state,
                f"setup_failed: drive click missed - {exc}",
            )
        self._wait_for_backdrop_clear(timeout=15)

        if self._search_state_is_stale():
            log.warning("stale search state detected; refreshing")
            try:
                self.driver.refresh()
            except Exception:
                pass
            if not self._is_logged_in(timeout=15):
                return self._fail_result(
                    scenario, run_id, cache_state,
                    "setup_failed: not signed in after refresh",
                )
            self._dismiss_search_state()
            try:
                self._click_drive(drive_name)
            except TimeoutException as exc:
                return self._fail_result(
                    scenario, run_id, cache_state,
                    f"setup_failed: drive click missed after refresh - {exc}",
                )
            self._wait_for_backdrop_clear(timeout=15)

        operator.reset_click_counter(self.driver)
        baseline_count = self._read_pagination_count()
        log.info("pre-tag-filter pagination count = %s", baseline_count)

        # Timed window
        with stopwatch() as elapsed:
            current_step = "init"
            try:
                current_step = "open_tag_explorer"
                self._open_tag_explorer_dialog()
                current_step = "expand_tag_key"
                self._expand_tag_key(tag_key)
                current_step = "select_tag_value"
                self._select_tag_value(tag_value)
                current_step = "click_apply_tag"
                self._click_apply_tag_explorer()
                current_step = "wait_count"
                count = self._wait_for_search_result_count(
                    timeout=SEARCH_RESULT_TIMEOUT_SEC,
                    baseline=baseline_count,
                )
            except Exception as exc:  # noqa: BLE001
                time_to_result = elapsed()
                exc_type = type(exc).__name__
                msg = str(exc)[:200].replace("\n", " ")
                log.error("scenario 5 failed in step=%s exc=%s msg=%s",
                          current_step, exc_type, msg)
                try:
                    from pathlib import Path as _P
                    _P("results").mkdir(parents=True, exist_ok=True)
                    self.driver.save_screenshot(f"results/debug-s5-fail-{current_step}.png")
                except Exception:
                    pass
                return self._fail_result(
                    scenario, run_id, cache_state,
                    f"scenario 5 step={current_step} {exc_type}: {msg}",
                )
            time_to_result = elapsed()

        click_count = operator.read_click_counter(self.driver)
        # Keystrokes: zero (Tag Explorer is mouse-only for value selection).
        keystroke_count = 0

        result_correct = self._validate_count(scenario, count)
        notes = (
            f"CSD Tag Explorer: {tag_key}={tag_value}, count={count} "
            f"(seeded count is 3060; CLI runs inline boto3 parallel get_object_tagging)"
            if count is not None
            else f"CSD Tag Explorer: {tag_key}={tag_value} did not produce a count"
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
            keystroke_count=keystroke_count,
            http_request_count=None,
            network_bytes=None,
            completed_within_cap=time_to_result is not None and time_to_result <= 300,
            non_technical_user_could_complete=result_correct,
            result_correct=result_correct,
            result_count_reported=count,
            notes=notes,
        )

    # ----- Helpers -----

    def _is_logged_in(self, timeout: float = 0.0) -> bool:
        """Check whether the post-login sentinel is in the DOM.

        timeout > 0 polls until either the sentinel appears or the timeout
        is hit. We use a short poll after auth restore because the dashboard
        JS takes a moment to render after navigation.
        """
        end = time.perf_counter() + max(timeout, 0.0)
        while True:
            try:
                self.driver.find_element(By.XPATH, LOGGED_IN_SENTINEL_XPATH)
                return True
            except Exception:
                if time.perf_counter() >= end:
                    return False
                time.sleep(0.25)

    def _dismiss_search_state(self) -> None:
        """Clear any leftover chip / search-input value from a prior run.

        Idempotent: safe to call when nothing is set. Strategies, in order:
          1. Send Escape twice via CDP - dismisses popovers/menus/dropdowns.
          2. Click any visible MUI chip's delete (X) button - removes the
             persistent search-term chip CSD shows after a search completes.
          3. Force-clear every input that looks like a search box: set
             .value='' and dispatch input + change events so React updates
             its controlled state.
        """
        # 1. Escape any open popover/menu.
        try:
            for _ in range(2):
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyDown", "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27,
                })
                self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27,
                })
        except Exception as exc:  # noqa: BLE001
            log.debug("dismiss_search_state: Escape dispatch failed: %s", exc)

        # 2. Click any chip delete buttons.
        try:
            chip_xes = self.driver.find_elements(
                By.CSS_SELECTOR, ".MuiChip-deleteIcon, .MuiChip-root [data-testid='CancelIcon']"
            )
            for x in chip_xes:
                try:
                    self.driver.execute_script("arguments[0].click();", x)
                except Exception:
                    continue
            if chip_xes:
                log.info("dismiss_search_state: removed %d chip(s)", len(chip_xes))
        except Exception as exc:  # noqa: BLE001
            log.debug("dismiss_search_state: chip removal failed: %s", exc)

        # 3. Force-clear any search-looking input via JS (so React state updates).
        try:
            self.driver.execute_script(
                """
                const sels = [
                    "input[placeholder='Search for...']",
                    "input[type='search']",
                    "[data-testid='search-input']"
                ];
                let cleared = 0;
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.value) {
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            setter.call(el, '');
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            cleared++;
                        }
                    }
                }
                return cleared;
                """
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("dismiss_search_state: JS clear failed: %s", exc)

    def _search_state_is_stale(self) -> bool:
        """Return True if leftover search state is still present.

        We treat the state as stale if EITHER a MUI chip is currently rendered
        OR any of the candidate search inputs has a non-empty value attribute.
        That covers both the "chip still up" and "input still has the prior
        query" failure modes we saw in the prod N=20 run.
        """
        try:
            chips = self.driver.find_elements(
                By.CSS_SELECTOR, ".MuiChip-root.MuiChip-deletable"
            )
            if chips:
                # Filter to ones that are visible-ish (have non-zero size).
                for c in chips:
                    try:
                        if c.is_displayed():
                            log.info("stale check: visible MuiChip detected")
                            return True
                    except Exception:
                        continue
        except Exception:
            pass

        try:
            stuck = self.driver.execute_script(
                """
                const sels = [
                    "input[placeholder='Search for...']",
                    "input[type='search']",
                    "[data-testid='search-input']"
                ];
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.value && el.value.length > 0) return true;
                    }
                }
                return false;
                """
            )
            if stuck:
                log.info("stale check: search input has non-empty value")
                return True
        except Exception:
            pass

        return False

    def _wait_for_backdrop_clear(self, timeout: float) -> None:
        """Wait until any MUI backdrop element is no longer in the DOM.

        Material-UI dialogs and drawers leave invisible <div class='MuiBackdrop-root'>
        elements during transitions; they intercept pointer events even when
        opacity is 0. We poll until none remain. If the timeout passes with
        a backdrop still around, we proceed anyway and rely on JS-based
        interactions in the caller.
        """
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            backdrops = self.driver.find_elements(
                By.CSS_SELECTOR, ".MuiBackdrop-root"
            )
            if not backdrops:
                return
            # All-zero-opacity backdrops are usually safe to ignore; but they
            # still intercept clicks per the error we saw, so we keep waiting
            # until they unmount entirely.
            time.sleep(0.2)
        log.warning("MUI backdrop still present after %.1fs; proceeding", timeout)

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

        CSD renders drives as buttons that contain a disk icon plus the
        drive name text. We try several strategies: data-testid first,
        then aria-label match, then visible-text contains() across button
        / link / role=button. We log which strategy hits so the team can
        prioritise data-testid coverage on the high-traffic selectors.
        """
        candidates = [
            ("data-testid", f"//*[@data-testid='drive-{drive_name}']"),
            ("aria-label", f"//*[@aria-label='{drive_name}']"),
            (
                "button-contains",
                f"//button[contains(normalize-space(.), '{drive_name}')]",
            ),
            (
                "link-contains",
                f"//a[contains(normalize-space(.), '{drive_name}')]",
            ),
            (
                "role-button-contains",
                f"//*[@role='button' and contains(normalize-space(.), '{drive_name}')]",
            ),
        ]

        last_err: Exception | None = None
        for label, xpath in candidates:
            try:
                elem = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                log.info("clicked drive %r via %s strategy", drive_name, label)
                elem.click()
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc

        raise TimeoutException(
            f"could not click drive {drive_name!r} via any strategy: {last_err}"
        )

    def _read_pagination_count(self) -> "int | None":
        """Read the current 'of N' value from the MUI pagination region."""
        candidate_xpaths = [
            "//*[contains(@class, 'MuiTablePagination')]",
            "//*[contains(text(), 'Rows per page')]/ancestor::*[1]",
            "//*[contains(text(), 'Rows per page')]/parent::*",
        ]
        for xp in candidate_xpaths:
            try:
                elem = self.driver.find_element(By.XPATH, xp)
                text = elem.text
                if not text:
                    continue
                m = re.search(
                    r"\b(\d[\d,]*)\s*[–—-]\s*(\d[\d,]*)\s+of\s+([\d,]+)\b",
                    text,
                )
                if m:
                    return int(m.group(3).replace(",", ""))
                m2 = re.search(r"\bof\s+([\d,]+)\b", text)
                if m2:
                    return int(m2.group(1).replace(",", ""))
            except Exception:
                continue
        return None

    def _wait_for_search_result_count(
        self,
        timeout: float,
        baseline: "int | None" = None,
    ) -> "int | None":
        """Wait for the search to fire (count moves off baseline), then settle.

        Two phases:
          1. Wait for the count to differ from baseline. This is how we know
             the search request actually started and the result has landed.
             We do not consider the baseline "stable" because a stable
             pre-search count just means the search has not fired yet.
          2. Once the count has moved, poll until it stops changing for
             settle_sec consecutive seconds, then return the stable value.

        If baseline is None, we skip phase 1 (used when there is no
        meaningful pre-search state).
        """
        end = time.perf_counter() + timeout
        # CSD's basic search may take a few seconds to fire on a large bucket.
        change_grace_sec = 30
        settle_sec = 2.0

        # Phase 1: wait for change off baseline (if a baseline was supplied).
        if baseline is not None:
            change_deadline = time.perf_counter() + min(change_grace_sec, timeout)
            log.info(
                "wait_for_count phase 1: waiting for count to change from baseline=%d",
                baseline,
            )
            while time.perf_counter() < change_deadline:
                current = self._read_pagination_count()
                if current is not None and current != baseline:
                    log.info(
                        "wait_for_count: count changed to %d after %.2fs",
                        current,
                        change_grace_sec - (change_deadline - time.perf_counter()),
                    )
                    break
                time.sleep(0.1)
            else:
                # Loop exited without break - count never changed.
                final = self._read_pagination_count()
                log.warning(
                    "wait_for_count: count never changed from baseline %d in %.0fs (read=%s)",
                    baseline, change_grace_sec, final,
                )
                return final

        # Phase 2: wait for stability.
        last_seen = self._read_pagination_count()
        last_change = time.perf_counter()
        while time.perf_counter() < end:
            current = self._read_pagination_count()
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

    # ----- Advanced Search > Filters helpers (used by scenario 4) -----

    def _native_click(self, el) -> None:
        """Dispatch a real mousedown+mouseup at the element's center via CDP.

        MUI Select components only open their listbox in response to native
        pointer events; Selenium's `.click()` and `arguments[0].click()` go
        through different code paths and don't always trigger the listbox.
        """
        rect = el.rect
        cx = rect["x"] + rect["width"] / 2
        cy = rect["y"] + rect["height"] / 2
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": cx, "y": cy, "button": "left", "clickCount": 1,
        })
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": cx, "y": cy, "button": "left", "clickCount": 1,
        })

    def _open_filters_dialog(self) -> None:
        """Click Advanced Search > Filters to open the filter-builder dialog.

        Both clicks use JS click to bypass any zero-opacity backdrop. The
        wait for the dialog uses a generous 12s timeout because the dialog
        mounts asynchronously and the prod backend is occasionally slow on
        a cold cache reload.
        """
        adv = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[normalize-space(.)='Advanced Search']")
            )
        )
        self.driver.execute_script("arguments[0].click();", adv)
        log.info("scenario 4: clicked Advanced Search")
        # Menu item; JS click since it's inside a popover.
        item = WebDriverWait(self.driver, 5).until(
            EC.presence_of_element_located(
                (By.XPATH,
                 "//*[@role='menuitem' or self::li or self::button]"
                 "[normalize-space(.)='Filters']")
            )
        )
        self.driver.execute_script("arguments[0].click();", item)
        log.info("scenario 4: clicked Filters menuitem")
        # Wait for the Filters dialog to be present. Be patient - the dialog
        # mounts asynchronously and we've seen 5s be too short on first cold
        # render after a CDP cache clear.
        WebDriverWait(self.driver, 12).until(
            EC.presence_of_element_located(
                (By.XPATH,
                 "//div[contains(@class, 'MuiDialog-paper')]"
                 "//*[normalize-space(.)='Apply filters']")
            )
        )
        log.info("scenario 4: filters dialog open")

    def _select_filter_criteria(self, label: str) -> None:
        """Click the first MUI Select inside the Filters dialog and pick the option.

        Some renders take longer than expected for the combobox to attach to
        the DOM. We poll up to 12s and log how many candidates we see along
        the way to make any future failure diagnosable.
        """
        criteria = None
        end = time.perf_counter() + 12
        last_log = 0.0
        while time.perf_counter() < end:
            cands = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'MuiDialog-paper')]"
                "//*[@role='combobox' or @aria-haspopup='listbox']"
            )
            visible = [c for c in cands if c.is_displayed()]
            if visible:
                criteria = visible[0]
                break
            now = time.perf_counter()
            if now - last_log > 2:
                log.info("scenario 4: waiting for criteria combobox (cands=%d, visible=0)", len(cands))
                last_log = now
            time.sleep(0.2)
        if criteria is None:
            raise TimeoutException("filters dialog criteria combobox never appeared")
        log.info("scenario 4: criteria combobox found, native-clicking")
        self._native_click(criteria)
        time.sleep(0.5)  # MUI listbox open animation
        try:
            opt = WebDriverWait(self.driver, 8).until(
                EC.presence_of_element_located(
                    (By.XPATH, f"//li[@role='option' and normalize-space(.)='{label}']")
                )
            )
        except TimeoutException:
            # Diagnostic snapshot so we can see why the listbox didn't open.
            try:
                from pathlib import Path as _P
                _P("results").mkdir(parents=True, exist_ok=True)
                self.driver.save_screenshot("results/debug-s4-criteria-fail.png")
                log.warning("saved results/debug-s4-criteria-fail.png on criteria-option timeout")
            except Exception:
                pass
            raise
        self._native_click(opt)

    def _select_filter_operator(self, label: str) -> None:
        """Pick an operator from the second visible Select in the Filters dialog.

        MUI re-renders the operator slot after the criteria pick. We:
          1. Wait for at least two visible comboboxes.
          2. Sleep 0.4s to let MUI's fade-in finish - clicking mid-animation
             gets the rect from the destination but the click handler isn't
             yet bound.
          3. Try CDP native click first; if listbox doesn't open within 1s,
             fall back to a Selenium .click() on a fresh-fetched element.
        """
        end = time.perf_counter() + 10
        op = None
        while time.perf_counter() < end:
            cands = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'MuiDialog-paper')]"
                "//*[@role='combobox' or @aria-haspopup='listbox']"
            )
            visible = [c for c in cands if c.is_displayed()]
            if len(visible) >= 2:
                op = visible[1]
                break
            time.sleep(0.2)
        if op is None:
            raise TimeoutException("operator combobox never appeared")
        time.sleep(0.4)  # let MUI fade-in finish before clicking
        self._native_click(op)

        # If the listbox doesn't open within 1s, the click missed. Try a
        # Selenium native click as fallback (works when the element is in
        # an open dialog because there's no backdrop above it).
        opened = False
        deadline = time.perf_counter() + 1.0
        while time.perf_counter() < deadline:
            opts = self.driver.find_elements(
                By.XPATH, f"//li[@role='option' and normalize-space(.)='{label}']"
            )
            if any(o.is_displayed() for o in opts):
                opened = True
                break
            time.sleep(0.1)
        if not opened:
            log.info("scenario 4: operator listbox didn't open via CDP, retrying via Selenium click")
            try:
                # Re-fetch in case it's stale after our CDP attempt.
                cands = self.driver.find_elements(
                    By.XPATH,
                    "//div[contains(@class, 'MuiDialog-paper')]"
                    "//*[@role='combobox' or @aria-haspopup='listbox']"
                )
                visible = [c for c in cands if c.is_displayed()]
                if len(visible) >= 2:
                    visible[1].click()
            except Exception as exc:  # noqa: BLE001
                log.warning("Selenium operator click also failed: %s", exc)

        opt = WebDriverWait(self.driver, 8).until(
            EC.presence_of_element_located(
                (By.XPATH, f"//li[@role='option' and normalize-space(.)='{label}']")
            )
        )
        self._native_click(opt)

    def _type_into_input(self, el, text: str) -> None:
        """Native-click the input then dispatch one keyDown/keyUp per character.

        Inter-character sleep matches what the exploration script used; without
        it MUI's date parser sometimes drops keystrokes and ends up with a
        partial value, which then makes Apply produce a no-op filter.
        """
        self._native_click(el)
        time.sleep(0.2)
        for ch in text:
            self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                "type": "keyDown", "text": ch
            })
            self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                "type": "keyUp", "text": ch
            })
            time.sleep(0.025)

    def _type_date_range(self, date_from: str, date_to: str) -> None:
        """Type from/to dates into the two MM/DD/YYYY inputs.

        Uses focus()+JS-set-value+input-event as the primary path because
        MUI date pickers have an inline calendar icon that intercepts clicks
        in the input's right half, and CDP mouse events sometimes hit the
        icon instead of the text. JS-set-value is robust to that.
        """
        # Wait for any operator-listbox popover to fully unmount; otherwise
        # keystrokes go to that listbox instead of the input.
        time.sleep(0.5)

        end = time.perf_counter() + 5
        inputs: list = []
        while time.perf_counter() < end:
            inputs = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'MuiDialog-paper')]"
                "//input[@placeholder='MM/DD/YYYY']"
            )
            visible = [i for i in inputs if i.is_displayed()]
            if len(visible) >= 2:
                inputs = visible
                break
            time.sleep(0.2)
        if len(inputs) < 2:
            raise TimeoutException("date inputs never appeared")
        log.info("scenario 4: date inputs found, from-rect=%s to-rect=%s",
                 inputs[0].rect, inputs[1].rect)

        # Set values via the React-aware setter so MUI's controlled state updates.
        # This bypasses the calendar-icon-intercepts-click problem entirely.
        self._set_input_value(inputs[0], date_from)
        time.sleep(0.2)
        self._set_input_value(inputs[1], date_to)
        time.sleep(0.3)

    def _set_input_value(self, el, value: str) -> None:
        """Set an input's value via the React-aware property setter and dispatch
        input + change events so React updates its controlled state.
        """
        self.driver.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            el.focus();
            setter.call(el, value);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.blur();
            """,
            el, value,
        )

    # ----- Advanced Search > Tag Explorer helpers (used by scenario 5) -----

    def _open_tag_explorer_dialog(self) -> None:
        """Click Advanced Search > Tag Explorer to open the tag-filter dialog."""
        adv = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[normalize-space(.)='Advanced Search']")
            )
        )
        self.driver.execute_script("arguments[0].click();", adv)
        log.info("scenario 5: clicked Advanced Search")
        item = WebDriverWait(self.driver, 5).until(
            EC.presence_of_element_located(
                (By.XPATH,
                 "//*[@role='menuitem' or self::li or self::button]"
                 "[normalize-space(.)='Tag Explorer']")
            )
        )
        self.driver.execute_script("arguments[0].click();", item)
        log.info("scenario 5: clicked Tag Explorer menuitem")
        # Wait for the Tag Explorer dialog (it has an Apply button).
        WebDriverWait(self.driver, 12).until(
            EC.presence_of_element_located(
                (By.XPATH,
                 "//div[contains(@class, 'MuiDialog-paper')]"
                 "//button[normalize-space(.)='Apply']")
            )
        )
        log.info("scenario 5: tag explorer dialog open")

    def _expand_tag_key(self, key: str) -> None:
        """Click the tag key row to expand its values."""
        end = time.perf_counter() + 8
        target = None
        while time.perf_counter() < end:
            cands = self.driver.find_elements(
                By.XPATH,
                f"//div[contains(@class, 'MuiDialog-paper')]"
                f"//*[normalize-space(text())='{key}']"
            )
            visible = [c for c in cands if c.is_displayed()]
            if visible:
                target = visible[0]
                break
            time.sleep(0.2)
        if target is None:
            raise TimeoutException(f"tag key {key!r} never appeared in Tag Explorer")
        log.info("scenario 5: expanding tag key %r", key)
        self._native_click(target)
        time.sleep(0.6)  # let the collapse animation play

    def _select_tag_value(self, value: str) -> None:
        """Click the tag value sub-row to toggle its selection."""
        end = time.perf_counter() + 8
        target = None
        while time.perf_counter() < end:
            cands = self.driver.find_elements(
                By.XPATH,
                f"//div[contains(@class, 'MuiDialog-paper')]"
                f"//*[normalize-space(text())='{value}']"
            )
            visible = [c for c in cands if c.is_displayed()]
            if visible:
                target = visible[0]
                break
            time.sleep(0.2)
        if target is None:
            raise TimeoutException(f"tag value {value!r} never appeared after expand")
        log.info("scenario 5: selecting tag value %r", value)
        self._native_click(target)
        time.sleep(0.4)

    def _click_apply_tag_explorer(self) -> None:
        """Click the Apply button in the Tag Explorer dialog (button text='Apply').

        Note: Tag Explorer's Apply button reads 'Apply' (not 'Apply filters'
        like the Filters dialog). Same robustness pattern: CDP click first,
        Selenium .click() fallback, dialog-close as success signal.
        """
        time.sleep(0.4)
        btn = WebDriverWait(self.driver, 5).until(
            EC.presence_of_element_located(
                (By.XPATH,
                 "//div[contains(@class, 'MuiDialog-paper')]"
                 "//button[normalize-space(.)='Apply']")
            )
        )
        self._native_click(btn)
        end = time.perf_counter() + 5
        while time.perf_counter() < end:
            still_open = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'MuiDialog-paper')]"
                "//button[normalize-space(.)='Apply']"
            )
            if not still_open:
                log.info("scenario 5: tag explorer dialog closed after CDP apply")
                return
            time.sleep(0.2)
        log.info("scenario 5: dialog still open after CDP apply; trying Selenium click")
        try:
            btn2 = self.driver.find_element(
                By.XPATH,
                "//div[contains(@class, 'MuiDialog-paper')]"
                "//button[normalize-space(.)='Apply']"
            )
            btn2.click()
        except Exception as exc:  # noqa: BLE001
            log.warning("Selenium tag apply click also failed: %s", exc)
        end = time.perf_counter() + 5
        while time.perf_counter() < end:
            still_open = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'MuiDialog-paper')]"
                "//button[normalize-space(.)='Apply']"
            )
            if not still_open:
                return
            time.sleep(0.2)
        log.warning("scenario 5: Apply click did not close tag explorer dialog")

    def _click_apply_filters(self) -> None:
        """Click the Apply filters button - CDP first, then Selenium .click() fallback.

        The Apply button is inside an open dialog so it's NOT covered by a
        backdrop; Selenium's native click works as a fallback. We wait for
        the dialog to actually close as the success signal.
        """
        # Small settle so the date inputs register their final values before
        # we click Apply - if the parser is still mid-debounce, Apply gets a
        # stale (partial) date range.
        time.sleep(0.4)

        btn = WebDriverWait(self.driver, 5).until(
            EC.presence_of_element_located(
                (By.XPATH, "//button[normalize-space(.)='Apply filters']")
            )
        )
        self._native_click(btn)
        # Wait for the dialog to close as a signal that the apply succeeded.
        end = time.perf_counter() + 5
        while time.perf_counter() < end:
            still_open = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'MuiDialog-paper')]"
                "//*[normalize-space(.)='Apply filters']"
            )
            if not still_open:
                log.info("scenario 4: filters dialog closed after CDP apply")
                return
            time.sleep(0.2)
        log.info("scenario 4: dialog still open after CDP apply; trying Selenium click")
        # Fallback - try Selenium .click() (works because Apply is in foreground dialog).
        try:
            btn2 = self.driver.find_element(
                By.XPATH, "//button[normalize-space(.)='Apply filters']"
            )
            btn2.click()
        except Exception as exc:  # noqa: BLE001
            log.warning("Selenium apply click also failed: %s", exc)
        # Final wait
        end = time.perf_counter() + 5
        while time.perf_counter() < end:
            still_open = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@class, 'MuiDialog-paper')]"
                "//*[normalize-space(.)='Apply filters']"
            )
            if not still_open:
                log.info("scenario 4: filters dialog closed after Selenium click")
                return
            time.sleep(0.2)
        log.warning("scenario 4: Apply filters click did not close dialog")
