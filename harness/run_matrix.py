"""Orchestrator. Runs scenarios across tools with N=10 per default.

CLI:
    python -m harness.run_matrix --tool csd --scenario 3 --runs 10
    python -m harness.run_matrix --tool all --scenario all --runs 10 --cache cold,warm
    python -m harness.run_matrix --dry-run    # walk the matrix, run nothing

Output goes to results/<run_id>.csv with one row per individual run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import get_logger
from .config import CsdCreds, IamCreds
from .results import ResultsWriter, new_run_id
from .scenarios import SCENARIOS, SCENARIOS_BY_ID
from .tools.base import Runner
from .tools.cli_runner import CliRunner
from .tools.console_runner import ConsoleRunner
from .tools.csd_runner import CsdRunner

log = get_logger("run_matrix")

ALL_TOOLS = ("csd", "console", "cli")
ALL_CACHES = ("cold", "warm")


def parse_csv_arg(arg: str, allowed: tuple[str, ...]) -> list[str]:
    if arg == "all":
        return list(allowed)
    items = [x.strip() for x in arg.split(",") if x.strip()]
    bad = [x for x in items if x not in allowed]
    if bad:
        raise SystemExit(f"unknown values in {arg!r}: {bad}; allowed: {allowed}")
    return items


def parse_scenarios_arg(arg: str) -> list[int]:
    if arg == "all":
        return sorted(SCENARIOS_BY_ID.keys())
    out = []
    for x in arg.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            n = int(x)
        except ValueError:
            raise SystemExit(f"--scenario must be 'all' or comma-list of ints, got {x!r}")
        if n not in SCENARIOS_BY_ID:
            raise SystemExit(f"unknown scenario id {n}; valid: {sorted(SCENARIOS_BY_ID)}")
        out.append(n)
    return out


def build_runner(name: str, dry_run: bool) -> Runner:
    if dry_run:
        # In dry-run mode we still want to walk the matrix without setup.
        # Provide creds with placeholder values so __init__ does not raise.
        if name == "csd":
            return CsdRunner(CsdCreds(email="", password=""))
        if name == "console":
            return ConsoleRunner(IamCreds("", ""))
        if name == "cli":
            return CliRunner(IamCreds("", ""))
        raise SystemExit(f"unknown tool {name}")

    if name == "csd":
        return CsdRunner(CsdCreds.from_env())
    if name == "console":
        return ConsoleRunner(IamCreds.from_env())
    if name == "cli":
        return CliRunner(IamCreds.from_env())
    raise SystemExit(f"unknown tool {name}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tool", default="all", help='comma-list of csd|console|cli, or "all"')
    p.add_argument("--scenario", default="all", help='comma-list of 1..5, or "all"')
    p.add_argument("--runs", type=int, default=10, help="N runs per (tool, scenario, cache)")
    p.add_argument("--cache", default="cold,warm", help='comma-list of cold|warm, or "all"')
    p.add_argument("--dry-run", action="store_true", help="walk the matrix, run nothing")
    p.add_argument(
        "--out",
        default=None,
        help="path to results CSV (default: results/<run_id>.csv)",
    )
    args = p.parse_args()

    tools = parse_csv_arg(args.tool, ALL_TOOLS)
    scenario_ids = parse_scenarios_arg(args.scenario)
    caches = parse_csv_arg(args.cache, ALL_CACHES)

    run_id = new_run_id()
    out_path = Path(args.out) if args.out else Path("results") / f"{run_id}.csv"

    log.info(
        "matrix: tools=%s scenarios=%s caches=%s runs=%d dry_run=%s out=%s",
        tools,
        scenario_ids,
        caches,
        args.runs,
        args.dry_run,
        out_path,
    )

    runners = {name: build_runner(name, args.dry_run) for name in tools}
    failures = 0

    with ResultsWriter(out_path) as writer:
        for tool_name, runner in runners.items():
            log.info("--- tool: %s ---", tool_name)
            if not args.dry_run:
                runner.setup()
            try:
                for sid in scenario_ids:
                    scenario = SCENARIOS_BY_ID[sid]
                    for cache in caches:
                        for n in range(args.runs):
                            log.info(
                                "  run %d/%d: scenario=%s cache=%s",
                                n + 1,
                                args.runs,
                                scenario.name,
                                cache,
                            )
                            if args.dry_run:
                                continue
                            runner.reset_cache(cache)
                            try:
                                result = runner.run(scenario, run_id, cache)
                                writer.write(result)
                            except Exception as exc:
                                failures += 1
                                log.exception("run failed: %s", exc)
            finally:
                if not args.dry_run:
                    runner.teardown()

    log.info("matrix done; failures=%d output=%s", failures, out_path)
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
