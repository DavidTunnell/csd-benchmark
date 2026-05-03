"""AWS CLI runner - shells out to `aws s3` and `aws s3api` and times them.

STATUS: scaffold with realistic command shape. Less brittle than the Selenium
runners because it doesn't have to deal with browsers; should be the first
runner to actually become functional.

Design notes:
  - Each scenario is one or more `aws` invocations. We measure wall time
    via Python's perf_counter, the same way `time` would.
  - Output is parsed minimally to confirm the result is correct (e.g.,
    Scenario 3 returns at least 20k matches).
  - HTTP request count and network bytes for the CLI are not directly
    observable; we leave them None and note that in the result.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from ..common import get_logger, stopwatch
from ..config import IamCreds, BUCKET_FLAT, BUCKET_OSS_MIRROR
from ..results import RunResult
from ..scenarios import Scenario
from .base import Runner

log = get_logger("cli_runner")


def _aws_path() -> str:
    p = shutil.which("aws")
    if not p:
        raise RuntimeError("aws CLI not on PATH; install AWS CLI v2 first")
    return p


class CliRunner(Runner):
    name = "cli"

    def __init__(self, creds: IamCreds) -> None:
        self.creds = creds
        self._env = None

    def setup(self) -> None:
        # Use a clean env slice so we don't accidentally inherit ambient
        # AWS_PROFILE that points at a different account.
        env = dict(os.environ)
        env.pop("AWS_PROFILE", None)
        env["AWS_ACCESS_KEY_ID"] = self.creds.access_key_id
        env["AWS_SECRET_ACCESS_KEY"] = self.creds.secret_access_key
        env["AWS_REGION"] = "us-east-1"
        self._env = env
        _aws_path()  # sanity check

    def teardown(self) -> None:
        self._env = None

    def reset_cache(self, state: str) -> None:
        # CLI has no meaningful client cache state. We treat cold and warm
        # as equivalent and just record which the run was tagged as.
        if state == "cold":
            log.info("reset_cache(cold): no-op for CLI; recorded for symmetry with browser tools")
        else:
            log.info("reset_cache(warm): no-op")

    def _run_cmd(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            env=self._env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def run(self, scenario: Scenario, run_id: str, cache_state: str) -> RunResult:
        aws = _aws_path()
        notes = ""
        result_correct = None
        time_to_result = None
        completed = None

        if scenario.id == 1:
            # Direct head-object - the CLI knows the key, no search needed.
            cmd = [aws, "s3api", "head-object", "--bucket", scenario.bucket, "--key", scenario.target_key]
            with stopwatch() as elapsed:
                proc = self._run_cmd(cmd, timeout=60)
                time_to_result = elapsed()
            result_correct = proc.returncode == 0
            completed = True
            if not result_correct:
                notes = f"head-object failed: {proc.stderr.strip()[:200]}"
        elif scenario.id == 2:
            cmd = [aws, "s3api", "head-object", "--bucket", scenario.bucket, "--key", scenario.target_key]
            with stopwatch() as elapsed:
                proc = self._run_cmd(cmd, timeout=60)
                time_to_result = elapsed()
            result_correct = proc.returncode == 0
            completed = True
            if not result_correct:
                notes = f"head-object failed: {proc.stderr.strip()[:200]}"
        elif scenario.id == 3:
            # `aws s3 ls --recursive` then grep. We measure both phases as
            # one wall time because that is what a CLI user actually does.
            # Real implementation will pipe through findstr/grep.
            notes = "TODO: aws s3 ls --recursive | grep test  (count hits)"
        elif scenario.id == 4:
            # Read the inventory CSV sidecar and filter rows by source_mtime.
            # CLI users in practice use `aws s3 cp` + a python/awk one-liner.
            notes = "TODO: aws s3 cp _meta/inventory-oss-mirror.csv - | filter date range"
        elif scenario.id == 5:
            # `aws s3api get-object-tagging` per object is impractical.
            # Real CLI users run a Python script using boto3. We treat this
            # as 'CLI can do it, but only with custom code' and time the
            # canonical custom script.
            notes = "TODO: boto3 script that lists then get_object_tagging in parallel"

        return RunResult(
            run_id=run_id,
            started_at="",
            tool=self.name,
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            cache_state=cache_state,
            operator_skill=scenario.operator_skill_by_tool[self.name],
            time_to_result_sec=time_to_result,
            click_count=0,
            keystroke_count=None,  # could be measured but not load-bearing
            http_request_count=None,  # CLI doesn't expose this easily
            network_bytes=None,
            completed_within_cap=completed,
            non_technical_user_could_complete=False,  # CLI is technical by definition
            result_correct=result_correct,
            notes=notes or "stub run, partial automation",
        )
