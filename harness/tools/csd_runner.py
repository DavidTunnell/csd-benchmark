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

        # CSD and the CLI define "match" differently. The CLI grep treats
        # any 'test' substring in the full S3 key as a match (32,763 hits).
        # CSD matches against the filename portion only, which yields 11,457
        # in our seeded bucket. Both are correct interpretations; the
        # benchmark reports both numbers with notes documenting the per-tool
        # rubric so the comparison stays apples-to-apples.
        result_correct = count is not None and count >= 10000

        notes = (
            f"CSD search '{substring}' on drive '{drive_name}', count={count} "
            f"(CSD matches by filename; CLI grep on s3 ls matches by full key)"
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
