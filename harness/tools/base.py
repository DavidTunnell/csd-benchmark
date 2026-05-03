"""Runner abstract base. All three tool runners implement this.

The orchestrator only knows about Runner; concrete runners encapsulate their
own setup, teardown, and per-scenario execution. This makes adding a fourth
tool (eg an AWS SDK runner) a self-contained change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..results import RunResult
from ..scenarios import Scenario


class Runner(ABC):
    """Abstract benchmark runner."""

    name: str  # 'csd' | 'console' | 'cli'

    @abstractmethod
    def setup(self) -> None:
        """One-time setup before any runs (login, browser launch, etc.)."""

    @abstractmethod
    def teardown(self) -> None:
        """One-time cleanup after all runs."""

    @abstractmethod
    def reset_cache(self, state: str) -> None:
        """Bring the runner into the requested cache state.

        state is 'cold' or 'warm'. Concrete runners decide what that means
        for them (browser profile reset, CLI session reset, etc.).
        """

    @abstractmethod
    def run(self, scenario: Scenario, run_id: str, cache_state: str) -> RunResult:
        """Execute one run of one scenario. Return a populated RunResult.

        Implementations are responsible for measuring and populating every
        column relevant to their tool. Fields they cannot measure should
        be left as None - the orchestrator does not invent values.
        """
