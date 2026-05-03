"""CSD runner - drives drive.cloudsee.cloud via Selenium.

STATUS: scaffold. Selenium logic intentionally not implemented yet so the
orchestrator interface and result schema get reviewed before time is sunk
into selectors that will change as CSD evolves.

Design notes:
  - Use selenium-wire so we can capture HTTP request count and bytes-out
    for free. selenium-wire wraps a Chrome WebDriver and exposes
    driver.requests as a list.
  - One headed Chrome session per benchmark run group, fresh profile per run.
  - Login at startup, then each scenario starts from the bucket root.
  - Cold cache = clear cookies + storage and reload. Warm cache = no reset.

Open questions David needs to weigh in on before this gets fleshed out:
  1. Auth flow - is there a non-OAuth login that selenium can drive headlessly?
     If only OAuth, we need a session cookie injection path, or accept manual
     login at the start of each run group (matches spec's "human-supervised").
  2. Selector strategy - do we use data-testid attributes, ARIA labels, or
     visual locators? Whichever is most stable across releases.
  3. How does CSD report search results? Single hit count we can read, or do
     we have to count items in the result list?
"""

from __future__ import annotations

from .. import auth_state, operator
from ..common import get_logger, stopwatch
from ..config import CSD_BASE_URL, CsdCreds
from ..results import RunResult
from ..scenarios import Scenario
from ..selectors import csd_resolver
from .base import Runner

log = get_logger("csd_runner")


class CsdRunner(Runner):
    name = "csd"

    def __init__(self, creds: CsdCreds) -> None:
        self.creds = creds
        self.driver = None  # selenium-wire webdriver instance

    def setup(self) -> None:
        # Real flow when implemented:
        #   1. launch Chrome with selenium-wire
        #   2. navigate to CSD_BASE_URL
        #   3. if auth_state.load("csd") returns a recent state, restore it
        #      and verify with auth_state.is_logged_in(driver, sentinel)
        #   4. otherwise: prompt the operator to sign in manually, then call
        #      auth_state.capture_from_driver and save
        #   5. inject click counter via operator.inject_click_counter
        log.info("setup: would launch Chrome and ensure CSD session is live at %s", CSD_BASE_URL)

    def teardown(self) -> None:
        log.info("teardown: would close Chrome")
        # TODO: driver.quit()

    def reset_cache(self, state: str) -> None:
        # Cold cache: clear cookies + storage, then re-restore the saved auth
        # state so we don't have to re-authenticate every run.
        if state == "cold":
            log.info("reset_cache(cold): would clear cookies + storage and replay saved auth state")
            saved = auth_state.load("csd")
            if saved is None:
                log.warning("no saved CSD auth state - run setup() first")
            # TODO: driver.delete_all_cookies(); clear storage via JS;
            # driver.get(CSD_BASE_URL); auth_state.restore_to_driver(driver, saved, CSD_BASE_URL)
            # operator.inject_click_counter(driver)
        else:
            log.info("reset_cache(warm): no-op")

    def run(self, scenario: Scenario, run_id: str, cache_state: str) -> RunResult:
        log.info("run: scenario=%s cache=%s (stub - returns placeholder)", scenario.name, cache_state)
        # Real flow when implemented:
        #   - operator.reset_click_counter(driver)
        #   - clear selenium-wire requests
        #   - prompt = operator.prompt_handoff_and_wait(scenario.description, target)
        #   - read elapsed = prompt.elapsed_sec
        #   - clicks = operator.read_click_counter(driver)
        #   - http_count = len(driver.requests)
        #   - net_bytes = sum(len(r.response.body) if r.response else 0 for r in driver.requests)
        #   - selectors via csd_resolver.find(driver, "search-input"), etc.
        with stopwatch() as elapsed:
            placeholder_time = elapsed()

        return RunResult(
            run_id=run_id,
            started_at="",  # TODO: set when scenario starts
            tool=self.name,
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            cache_state=cache_state,
            operator_skill=scenario.operator_skill_by_tool[self.name],
            time_to_result_sec=placeholder_time,
            notes="stub run, no real automation yet",
        )
