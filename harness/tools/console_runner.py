"""AWS Console runner - drives console.aws.amazon.com/s3/ via Selenium.

STATUS: scaffold. The Console runner is the highest-friction of the three
because of:
  - Bot detection. Per spec, runs are human-supervised and manual fallback
    is the policy from day one.
  - Pagination. The Console paginates at 1000 objects per page with no
    jump-to-page; Scenario 1 is specifically about that pain.
  - No tag query. Scenario 5 cannot complete; the runner records that as
    'completed_within_cap = false' and 'result_correct = false'.

Design notes:
  - Same selenium-wire setup as csd_runner.
  - Login uses the dedicated csd-benchmark-readonly IAM user. No MFA.
  - The metric for Scenario 1 is "time until the operator visually finds
    the target file in the viewport" - we measure clicks and elapsed time
    and the operator confirms with a key press when they see it.

Open questions:
  1. Whether to script the click stream or have a human drive while we time.
     Spec leans manual ('scripted clicks where bot detection allows, manual
     fallback otherwise'). Hybrid: script the obvious parts (search-bar input)
     and let the human do the next-page clicks while a stopwatch runs.
  2. How to capture click count when the human is driving. Option: chrome
     extension that posts events to a local listener.
"""

from __future__ import annotations

from .. import auth_state, operator
from ..common import get_logger, stopwatch
from ..config import AWS_CONSOLE_BASE_URL, IamCreds
from ..results import RunResult
from ..scenarios import Scenario
from ..selectors import console_resolver
from .base import Runner

log = get_logger("console_runner")


class ConsoleRunner(Runner):
    name = "console"

    def __init__(self, creds: IamCreds) -> None:
        self.creds = creds
        self.driver = None

    def setup(self) -> None:
        log.info("setup: would launch Chrome and sign in to %s", AWS_CONSOLE_BASE_URL)

    def teardown(self) -> None:
        log.info("teardown: would close Chrome")

    def reset_cache(self, state: str) -> None:
        if state == "cold":
            log.info("reset_cache: would clear cookies + storage and reload")
        else:
            log.info("reset_cache(warm): no-op")

    def run(self, scenario: Scenario, run_id: str, cache_state: str) -> RunResult:
        log.info("run: scenario=%s cache=%s (stub)", scenario.name, cache_state)

        # Scenario 5 is structurally impossible in the Console: there's no
        # tag query UI for S3 object tags. The runner records the failure
        # rather than attempting it.
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
                notes="AWS Console has no tag query UI for S3 object tags. Cannot complete by design.",
            )

        with stopwatch() as elapsed:
            # TODO: scenario-specific Selenium + manual handoff.
            placeholder_time = elapsed()

        return RunResult(
            run_id=run_id,
            started_at="",
            tool=self.name,
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            cache_state=cache_state,
            operator_skill=scenario.operator_skill_by_tool[self.name],
            time_to_result_sec=placeholder_time,
            notes="stub run, no real automation yet",
        )
