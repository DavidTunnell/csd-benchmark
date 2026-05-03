"""Human-in-the-loop helpers used by the browser runners.

Decision (recorded in README): the harness sets up the page state and
starts measuring; the operator drives the actual interaction; the operator
hits a hotkey when the target file is visible. Objective metrics
(HTTP request count, network bytes via selenium-wire, click count via
injected JS) are still captured automatically.

This module:
  - shows console prompts ('Run 3/10. Scenario flat-bucket-target-deep. Cold cache. Find <key>; press F12 when visible.')
  - waits for a hotkey press in a way that does not block the Selenium driver
  - injects a click counter into the page that survives navigation
  - reads the click counter back when the run ends

The hotkey is read from stdin via a helper thread to avoid the heavy
keyboard hooks that some libraries set up. Operator types the hotkey letter
and presses Enter. Less ergonomic than a global hotkey, but zero deps
beyond stdlib.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .common import get_logger

log = get_logger("operator")


# JavaScript injected into the page to count clicks. The counter survives
# navigation by re-injecting from page-load events (handled in inject_click_counter).
CLICK_COUNTER_JS = """
(function() {
    if (window.__csd_benchmark_click_counter_installed) { return; }
    window.__csd_benchmark_click_counter_installed = true;
    window.__csd_benchmark_click_count = 0;
    document.addEventListener('click', function() {
        window.__csd_benchmark_click_count = (window.__csd_benchmark_click_count || 0) + 1;
    }, true);
})();
"""

CLICK_COUNTER_RESET_JS = "window.__csd_benchmark_click_count = 0;"
CLICK_COUNTER_READ_JS = "return window.__csd_benchmark_click_count || 0;"


@dataclass
class HandoffSignal:
    """The result of a handoff: either operator confirmed, or it timed out."""

    confirmed: bool
    elapsed_sec: float
    notes: str = ""


def prompt_handoff_and_wait(
    description: str,
    target_hint: str,
    hotkey: str = "f",
    timeout_sec: float = 300.0,
) -> HandoffSignal:
    """Print a prompt, start a stopwatch, wait for operator hotkey on stdin.

    The operator types `hotkey` followed by Enter when the target is visible.
    Any other input cancels the run with confirmed=False.

    timeout_sec mirrors the spec's 5-minute soft cap.
    """
    print()
    print("=" * 72)
    print(f"  {description}")
    print(f"  Target: {target_hint}")
    print(f"  Press '{hotkey}' + Enter when target is visible. Any other input cancels.")
    print("=" * 72, flush=True)

    start = time.perf_counter()
    answer: list[Optional[str]] = [None]

    def reader() -> None:
        try:
            answer[0] = sys.stdin.readline()
        except Exception as exc:  # noqa: BLE001
            log.warning("stdin read failed: %s", exc)
            answer[0] = ""

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    elapsed = time.perf_counter() - start

    if answer[0] is None:
        # Timed out
        return HandoffSignal(confirmed=False, elapsed_sec=elapsed, notes="timed out at 5-minute cap")

    pressed = (answer[0] or "").strip().lower()
    if pressed == hotkey.lower():
        return HandoffSignal(confirmed=True, elapsed_sec=elapsed)
    return HandoffSignal(confirmed=False, elapsed_sec=elapsed, notes=f"operator cancelled with input {pressed!r}")


def inject_click_counter(driver) -> None:
    """Install the click counter on the current page.

    Re-call after every navigation; the script is idempotent.
    """
    driver.execute_script(CLICK_COUNTER_JS)


def reset_click_counter(driver) -> None:
    driver.execute_script(CLICK_COUNTER_RESET_JS)


def read_click_counter(driver) -> int:
    try:
        n = driver.execute_script(CLICK_COUNTER_READ_JS)
        return int(n or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read click counter: %s", exc)
        return 0
