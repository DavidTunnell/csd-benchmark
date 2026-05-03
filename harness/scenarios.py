"""The five benchmark scenarios as data.

A Scenario is intentionally minimal: an id, a target description, the target file
or query, and a callable per tool that knows how to execute it. Tool-specific
runners pick up the relevant fields.

The fields here mirror the spec section 'Scenarios in scope (v1.0)'.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import (
    BUCKET_FLAT,
    BUCKET_OSS_MIRROR,
    INVENTORY_OSS_KEY,
)


@dataclass(frozen=True)
class Scenario:
    """One benchmark scenario."""

    id: int
    name: str
    bucket: str
    description: str
    # Target file (for scenarios 1, 2). Some scenarios search and don't have one fixed key.
    target_key: Optional[str] = None
    # Substring (for scenario 3).
    substring: Optional[str] = None
    # Date range (for scenario 4), as ISO 8601 strings.
    date_range_start: Optional[str] = None
    date_range_end: Optional[str] = None
    # Tag query (for scenario 5).
    tag_key: Optional[str] = None
    tag_value: Optional[str] = None
    # Operator skill assumption per spec: "non-technical" or "technical".
    # CLI is technical; CSD and Console are non-technical.
    operator_skill_by_tool: dict = field(
        default_factory=lambda: {"csd": "non-technical", "console": "non-technical", "cli": "technical"}
    )


SCENARIOS: list[Scenario] = [
    Scenario(
        id=1,
        name="flat-bucket-target-deep",
        bucket=BUCKET_FLAT,
        description="Find a known file at lexical index ~149,500 of a flat 150k-object bucket.",
        target_key="needle-quarterly-report-2024-Q4.pdf",
    ),
    Scenario(
        id=2,
        name="six-folders-deep",
        bucket=BUCKET_OSS_MIRROR,
        description="Find a file 6+ folders deep when the full path is known.",
        target_key="linux/v6.10/arch/arm64/boot/dts/freescale/imx8mq-evk.dts",
    ),
    Scenario(
        id=3,
        name="substring-across-bucket",
        bucket=BUCKET_OSS_MIRROR,
        description="Find files by partial filename across the whole bucket. Expecting 20k+ matches.",
        substring="test",
    ),
    Scenario(
        id=4,
        name="date-range",
        bucket=BUCKET_OSS_MIRROR,
        description="Find files modified in 2020 (source-mtime metadata) across all four projects.",
        date_range_start="2020-01-01T00:00:00Z",
        date_range_end="2020-12-31T23:59:59Z",
    ),
    Scenario(
        id=5,
        name="tag-search",
        bucket=BUCKET_OSS_MIRROR,
        description="Find files tagged domain=audio. Console cannot query tags natively.",
        tag_key="domain",
        tag_value="audio",
    ),
]

SCENARIOS_BY_ID: dict[int, Scenario] = {s.id: s for s in SCENARIOS}
