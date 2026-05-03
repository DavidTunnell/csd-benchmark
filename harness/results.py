"""Result schema and CSV writer.

One CSV row per individual run. The official report aggregates these into
median/p95 per (scenario, tool, cache state).

Schema is held here so all three runners write the same columns. If you add
a column, update REPORT_COLUMNS in stable order so old result CSVs still
parse with the new code.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


REPORT_COLUMNS = [
    "run_id",
    "started_at",
    "tool",
    "scenario_id",
    "scenario_name",
    "cache_state",         # cold | warm
    "operator_skill",      # non-technical | technical
    "time_to_result_sec",
    "click_count",
    "keystroke_count",
    "http_request_count",
    "network_bytes",
    "completed_within_cap",  # bool, soft cap is 5 min
    "non_technical_user_could_complete",  # bool, qualitative flag
    # Two correctness fields (decision recorded in harness/README.md):
    #   result_correct: did the tool produce a usable answer for this scenario,
    #     judged per-tool because Console's prefix-only search legitimately
    #     returns far fewer hits than CSD's bucket-wide search for Scenario 3.
    #   result_count_reported: numeric count the tool surfaced, for audit.
    "result_correct",
    "result_count_reported",
    "notes",
]


@dataclass
class RunResult:
    run_id: str
    started_at: str
    tool: str
    scenario_id: int
    scenario_name: str
    cache_state: str
    operator_skill: str
    time_to_result_sec: Optional[float] = None
    click_count: Optional[int] = None
    keystroke_count: Optional[int] = None
    http_request_count: Optional[int] = None
    network_bytes: Optional[int] = None
    completed_within_cap: Optional[bool] = None
    non_technical_user_could_complete: Optional[bool] = None
    result_correct: Optional[bool] = None
    result_count_reported: Optional[int] = None
    notes: str = ""


class ResultsWriter:
    """Append-only CSV writer.

    Use as a context manager so the file handle closes deterministically:

        with ResultsWriter(Path("results/run-2026-05-04.csv")) as w:
            w.write(result)
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None
        self._writer = None

    def __enter__(self) -> "ResultsWriter":
        new_file = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=REPORT_COLUMNS)
        if new_file:
            self._writer.writeheader()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            self._fh.close()

    def write(self, result: RunResult) -> None:
        if self._writer is None:
            raise RuntimeError("ResultsWriter not entered as context manager")
        row = {k: v for k, v in asdict(result).items() if k in REPORT_COLUMNS}
        # Coerce booleans to 'true'/'false' so the CSV is consistent across spreadsheets.
        for k, v in list(row.items()):
            if isinstance(v, bool):
                row[k] = "true" if v else "false"
        self._writer.writerow(row)


def new_run_id(prefix: str = "run") -> str:
    """Stable, sortable run id."""
    return f"{prefix}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
