# csd-benchmark-harness

Selenium and CLI runners that drive the five benchmark scenarios across **CloudSee Drive**, the **AWS Console**, and the **AWS CLI** against the buckets built by [`csd-benchmark`](https://bitbucket.org/cloudsee-drive/csd-benchmark).

This is the harness side of the benchmark. Bucket build, fairness rules, and run conditions live in the seed repo. The deliverable here is `results/*.csv` that drives Scott's marketing plan.

## Status

**Scaffold.** Directory structure, interfaces, and stub runners are in. Selenium logic and the Console human-handoff path are not implemented yet. See "What is and isn't done" below.

## How it works

```
                                       ┌─────────────┐
                                       │ run_matrix  │   (orchestrator)
                                       └──────┬──────┘
                                              │ for each (tool, scenario, cache, n)
                  ┌───────────────────────────┼───────────────────────────┐
                  │                           │                           │
            ┌─────┴──────┐             ┌──────┴──────┐             ┌──────┴──────┐
            │ csd_runner │             │console_runn.│             │ cli_runner  │
            │ Selenium   │             │ Selenium +  │             │ aws CLI     │
            │ + sw       │             │ manual      │             │ subprocess  │
            └─────┬──────┘             └──────┬──────┘             └──────┬──────┘
                  └───────────────────────────┼───────────────────────────┘
                                              ▼
                                     ┌────────────────┐
                                     │ results/*.csv  │
                                     │ one row / run  │
                                     └────────────────┘
```

Every runner implements the same `Runner` interface in `harness/tools/base.py` so the orchestrator can treat them identically. The CSV schema lives in `harness/results.py` and is the single source of truth for what gets reported.

## Layout

```
csd-benchmark-harness/
  README.md
  RUN_MATRIX.md            # how the matrix is structured, per-scenario notes
  requirements.txt
  harness/
    __init__.py
    config.py              # bucket names, URLs, env-driven creds dataclasses
    common.py              # logger, stopwatch
    scenarios.py           # the 5 scenarios as data
    results.py             # RunResult dataclass + ResultsWriter (CSV)
    run_matrix.py          # CLI orchestrator
    tools/
      __init__.py
      base.py              # Runner ABC
      csd_runner.py        # CSD Selenium runner (stub)
      console_runner.py    # AWS Console Selenium runner (stub)
      cli_runner.py        # AWS CLI runner (partial)
  results/                 # CSVs land here
```

## Running

After setup (see below):

```bash
# walk the full matrix without executing anything
python -m harness.run_matrix --dry-run

# 10 runs of scenario 3, cold cache only, against CSD only
python -m harness.run_matrix --tool csd --scenario 3 --cache cold --runs 10

# the official run group
python -m harness.run_matrix --tool all --scenario all --cache all --runs 10
```

Output: `results/run-<utc-timestamp>.csv`.

## Setup

```bash
python -m pip install -r requirements.txt
```

Environment variables:

```bash
# AWS Console + CLI runners
export CSD_BENCHMARK_AWS_ACCESS_KEY_ID=...     # csd-benchmark-readonly user
export CSD_BENCHMARK_AWS_SECRET_ACCESS_KEY=...

# CSD runner
export CSD_BENCHMARK_CSD_EMAIL=...             # dedicated trial account
export CSD_BENCHMARK_CSD_PASSWORD=...
```

Chrome must be installed; `webdriver-manager` auto-fetches a matching `chromedriver`.

## What is and isn't done

| Area | Status |
| :---- | :---- |
| Orchestrator (`run_matrix.py`) | Done. Walks the matrix, manages setup/teardown, writes CSV. |
| Result schema (`results.py`) | Done. 16 columns. |
| Scenario data (`scenarios.py`) | Done. All 5 scenarios with the right targets. |
| `cli_runner.py` | Partial. Scenarios 1 and 2 are wired (head-object + timing). 3, 4, 5 are stubs. |
| `csd_runner.py` | Stub. Selenium logic not written; design composes auth_state + selectors + operator. |
| `console_runner.py` | Stub. Includes structural failure for Scenario 5 (Console has no tag query). |
| `auth_state.py` | Done. Capture/save/load/restore cookies + storage, sentinel logged-in check. |
| `selectors.py` | Done. `find_by_intent` with data-testid → aria-label → CSS fallback for both CSD and Console. |
| `operator.py` | Done. Hotkey wait, click counter inject/reset/read. |
| `report.py` | Done. CSV → markdown aggregator with median, p95, completion rates, discarded runs. |
| HAR capture | Not wired. Plan: selenium-wire, dump per-run HAR alongside CSV. |
| Cache reset | Stub for browsers (auth replay path documented), no-op for CLI. |

## Architectural decisions

These were locked early so the scaffold has consistent shape. Each one has a corresponding helper module under `harness/`.

### 1. Authentication: capture once, replay per run

`harness/auth_state.py`. The operator signs in manually at the start of a run group. The harness extracts cookies + localStorage + sessionStorage and persists them to `.local/auth/<tool>.json`. Cold-cache resets clear browser state then replay the saved auth, so we never re-authenticate mid-run-group. If the saved state is stale (`is_logged_in()` fails on a sentinel selector), the harness pauses and prompts the operator to sign in again.

This was the right call because runs are human-supervised by spec, and Selenium-driving an OAuth flow against a production SaaS is a fragile use of test budget that will break the day CSD changes its login UX.

### 2. Selectors: `find_by_intent` with a fallback chain

`harness/selectors.py`. Scenarios reference UI elements by intent (`"search-input"`, `"fast-buckets-trigger"`, etc.). The resolver tries in order:

1. `[data-testid="<intent>"]`
2. `[aria-label="<configured label>"]`
3. CSS fallback

This works today using ARIA labels. When the CSD frontend team adds `data-testid` attributes (recommended; quick PR per intent), no scenario code changes — only `selectors.py`.

### 3. Console human handoff: hybrid scripted setup + manual drive + hotkey

`harness/operator.py`. AWS Console bot detection is aggressive, so for the Console runner the harness sets up the page state (URL, IAM identity), starts the stopwatch and metric counters, then prints a prompt to the terminal. The operator drives the actual interaction in the browser and types a single character hotkey + Enter when the target is visible. Click count comes from a JS snippet injected on each page load; HTTP count and bytes come from `selenium-wire` regardless of whether the human or the script is driving.

Same pattern works for CSD when a scenario is too brittle to script end-to-end. CLI runs are fully automated.

### 4. Correctness: two fields, per-tool definitions

The `RunResult` schema has both:

- `result_correct` — boolean, did the tool produce a usable answer for *this scenario by this tool's standards*
- `result_count_reported` — int, the count the tool surfaced (when applicable)

For Scenario 3, the Console will surface far fewer than 32,763 because its substring search is prefix-only. That's the entire point of the benchmark; punishing the Console for it would be circular. Per-scenario rubrics are documented in `RUN_MATRIX.md`.

### 5. Aggregation: deterministic markdown report

`harness/report.py`. Reads one or more `results/*.csv` files and emits a markdown report with median + p95 per (tool, scenario, cache_state), plus a discarded-runs list for transparency. CLI:

```bash
python -m harness.report results/run-2026-05-04T*.csv --out results/report-2026-05-04.md
```

Scott reads markdown. A notebook can be added later if anyone needs ad-hoc exploration.

## Out of scope for v1

Per spec:

- Videos, infographics, web embed
- Anything beyond the five scenarios

## Run conditions

See [`csd-benchmark/RUN_CONDITIONS.md`](https://bitbucket.org/cloudsee-drive/csd-benchmark/src/main/RUN_CONDITIONS.md) for the fairness rules. They live in the seed repo because they ship alongside the data, not the code.
