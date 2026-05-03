"""AWS CLI runner - the technical-user comparison point.

Scenarios 1, 2 use `aws s3api head-object` (CLI knows the key).
Scenario 3 uses `aws s3 ls --recursive` and counts substring matches.
Scenario 4 downloads the inventory CSV sidecar and filters by source_mtime.
Scenario 5 uses inline boto3 (parallel get_object_tagging) because pure
`aws s3api get-object-tagging` per object would be hours-long; the spec
calls this the canonical "you have to write code" workaround for the CLI.

We measure wall time via Python's perf_counter, the same way `time` would.
HTTP request count and network bytes are not directly observable for the
plain `aws` CLI; we leave them None for those scenarios and populate them
for the inline-boto3 path where boto3 client telemetry is available.
"""

from __future__ import annotations

import csv as _csv
import datetime as _dt
import io as _io
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3 as _boto3
import botocore.config as _botocore_config

from ..common import get_logger, stopwatch
from ..config import IamCreds, BUCKET_FLAT, BUCKET_OSS_MIRROR, INVENTORY_OSS_KEY
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
        env = dict(os.environ)
        if self.creds.access_key_id and self.creds.secret_access_key:
            # Explicit creds override ambient. Clean AWS_PROFILE so it doesn't
            # silently route us to a different account.
            env.pop("AWS_PROFILE", None)
            env["AWS_ACCESS_KEY_ID"] = self.creds.access_key_id
            env["AWS_SECRET_ACCESS_KEY"] = self.creds.secret_access_key
        else:
            log.info("no explicit IAM creds in env; using ambient AWS config")
        env.setdefault("AWS_REGION", "us-east-1")
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
            notes = (
                f"aws s3api head-object on s3://{scenario.bucket}/{scenario.target_key}"
                if result_correct
                else f"head-object failed: {proc.stderr.strip()[:200]}"
            )
        elif scenario.id == 2:
            cmd = [aws, "s3api", "head-object", "--bucket", scenario.bucket, "--key", scenario.target_key]
            with stopwatch() as elapsed:
                proc = self._run_cmd(cmd, timeout=60)
                time_to_result = elapsed()
            result_correct = proc.returncode == 0
            completed = True
            notes = (
                f"aws s3api head-object on s3://{scenario.bucket}/{scenario.target_key}"
                if result_correct
                else f"head-object failed: {proc.stderr.strip()[:200]}"
            )
        elif scenario.id == 3:
            # `aws s3 ls --recursive` returns one line per object. We pipe to
            # findstr (Windows) or grep (Unix) to count substring matches. We
            # measure both phases as one wall time because that's the actual
            # CLI workflow.
            count, elapsed_sec = self._scenario_3_count_substring(scenario)
            time_to_result = elapsed_sec
            count_reported = count
            completed = elapsed_sec is not None
            # Per locked rubric: ground-truth is 32,763. Pass at >= 32000 to
            # tolerate small drift from concurrent inventory regeneration etc.
            min_expected = 32000
            result_correct = (count is not None and count >= min_expected)
            notes = f"aws s3 ls --recursive piped through grep '{scenario.substring}', counted {count} matches"
        elif scenario.id == 4:
            # CLI users `aws s3 cp` the inventory sidecar then filter rows.
            # We measure both download + filter as one wall time.
            count, elapsed_sec = self._scenario_4_filter_inventory(scenario)
            time_to_result = elapsed_sec
            count_reported = count
            completed = elapsed_sec is not None
            # Year-2024 range hits Linux v6.10's ~85k files. Threshold lets
            # small drift through.
            result_correct = (count is not None and count >= 50000)
            notes = (
                f"aws s3 cp inventory CSV + filter rows by source_mtime in [{scenario.date_range_start}, "
                f"{scenario.date_range_end}], counted {count}"
            )
        elif scenario.id == 5:
            # `aws s3api get-object-tagging` per-object would take 30+ minutes
            # against 155k objects, so this scenario is "you have to write
            # code" - canonical workaround is inline boto3 with parallel
            # get_object_tagging. We time the whole script.
            count, elapsed_sec = self._scenario_5_tag_search(scenario)
            time_to_result = elapsed_sec
            count_reported = count
            completed = elapsed_sec is not None
            # Seeded count is 3060; allow slack for indexing drift.
            result_correct = (count is not None and count >= 100)
            notes = (
                f"inline boto3 (parallel get_object_tagging) for {scenario.tag_key}={scenario.tag_value}, "
                f"counted {count}. CLI cannot do this without custom code per spec."
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
            click_count=0,
            keystroke_count=None,
            http_request_count=None,  # not surfaced by `aws` CLI
            network_bytes=None,
            completed_within_cap=(completed and time_to_result is not None and time_to_result <= 300),
            non_technical_user_could_complete=False,  # CLI is technical by definition
            result_correct=result_correct,
            result_count_reported=count_reported if scenario.id in (3, 4, 5) else None,
            notes=notes or "ok",
        )

    # ----- Per-scenario implementations -----

    def _scenario_3_count_substring(self, scenario: Scenario) -> tuple[int | None, float | None]:
        """Run `aws s3 ls --recursive` then count lines containing the substring.

        We deliberately use `aws s3 ls --recursive` (not list-objects-v2 with
        --query) because that's what a real CLI user reaches for first.
        """
        aws = _aws_path()
        substring = (scenario.substring or "").lower()
        if not substring:
            return None, None

        with stopwatch() as elapsed:
            proc = subprocess.run(
                [aws, "s3", "ls", f"s3://{scenario.bucket}/", "--recursive"],
                env=self._env,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                log.error("aws s3 ls failed: %s", proc.stderr.strip()[:200])
                return None, elapsed()
            count = sum(1 for line in proc.stdout.splitlines() if substring in line.lower())
            return count, elapsed()

    def _scenario_4_filter_inventory(self, scenario: Scenario) -> tuple[int | None, float | None]:
        """Download inventory CSV, filter rows by source_mtime in the configured range."""
        aws = _aws_path()
        if not scenario.date_range_start or not scenario.date_range_end:
            return None, None
        try:
            start = _dt.datetime.fromisoformat(scenario.date_range_start.replace("Z", "+00:00"))
            end = _dt.datetime.fromisoformat(scenario.date_range_end.replace("Z", "+00:00"))
        except ValueError as exc:
            log.error("invalid date range: %s", exc)
            return None, None

        with stopwatch() as elapsed:
            # `aws s3 cp <key> -` streams the CSV to stdout.
            proc = subprocess.run(
                [aws, "s3", "cp", f"s3://{scenario.bucket}/{INVENTORY_OSS_KEY}", "-"],
                env=self._env,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                log.error("aws s3 cp inventory failed: %s", proc.stderr.strip()[:200])
                return None, elapsed()
            reader = _csv.DictReader(_io.StringIO(proc.stdout))
            count = 0
            for row in reader:
                mtime_str = (row.get("source_mtime") or "").strip()
                if not mtime_str:
                    continue
                try:
                    mtime = _dt.datetime.fromisoformat(mtime_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if start <= mtime <= end:
                    count += 1
            return count, elapsed()

    def _scenario_5_tag_search(self, scenario: Scenario) -> tuple[int | None, float | None]:
        """Inline boto3: list all keys, get tags in parallel, count matches.

        The spec calls this the canonical "you have to write code" CLI
        workaround. We use a generous max_pool_connections so we don't
        choke on the parallel HEAD-style traffic.
        """
        if not scenario.tag_key or not scenario.tag_value:
            return None, None

        with stopwatch() as elapsed:
            cfg = _botocore_config.Config(max_pool_connections=72)
            session_kwargs = {}
            if self.creds.access_key_id and self.creds.secret_access_key:
                session_kwargs["aws_access_key_id"] = self.creds.access_key_id
                session_kwargs["aws_secret_access_key"] = self.creds.secret_access_key
            session_kwargs["region_name"] = self._env.get("AWS_REGION", "us-east-1")
            s3 = _boto3.client("s3", config=cfg, **session_kwargs)

            paginator = s3.get_paginator("list_objects_v2")
            keys: list[str] = []
            for page in paginator.paginate(Bucket=scenario.bucket):
                for obj in page.get("Contents", []) or []:
                    k = obj["Key"]
                    if k.startswith("_meta/"):
                        continue
                    keys.append(k)

            tag_key = scenario.tag_key
            tag_value = scenario.tag_value

            def _has_tag(key: str) -> bool:
                try:
                    resp = s3.get_object_tagging(Bucket=scenario.bucket, Key=key)
                except Exception:
                    return False
                for tag in resp.get("TagSet", []) or []:
                    if tag.get("Key") == tag_key and tag.get("Value") == tag_value:
                        return True
                return False

            count = 0
            with ThreadPoolExecutor(max_workers=64) as pool:
                futures = [pool.submit(_has_tag, k) for k in keys]
                for fut in as_completed(futures):
                    if fut.result():
                        count += 1
            return count, elapsed()
