"""Selector strategy: find_by_intent.

Decision (recorded in README): scenarios reference UI elements by intent
(e.g., 'search-input', 'fast-buckets-trigger', 'tag-explorer-button'). The
helper resolves intent to a concrete locator using this priority order:

  1. data-testid="<intent>"            (preferred, ask CSD team to add these)
  2. aria-label matching INTENT_LABELS (works today, accessibility-friendly)
  3. fallback CSS in INTENT_FALLBACKS  (last resort, brittle)

When the CSD frontend ships data-testid attributes, we don't change scenario
code; we just keep the helpers updated here. New intents only need an entry
in this file.

The Console runner uses the same helper but with a separate intent map so
the two tools don't accidentally share selectors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .common import get_logger

log = get_logger("selectors")

if TYPE_CHECKING:  # avoid hard selenium import at module load time
    from selenium.webdriver.remote.webdriver import WebDriver
    from selenium.webdriver.remote.webelement import WebElement


# Intent -> ARIA label text that matches the live CSD UI today.
# Update when the CSD UI changes; data-testid wins over this when available.
CSD_INTENT_LABELS: dict[str, str] = {
    "search-input": "Search files",
    "fast-buckets-trigger": "Fast Buckets",
    "tag-explorer-trigger": "Tag Explorer",
    "filter-by-date-range": "Date range",
    "filter-by-tag": "Tag",
    "result-count-badge": "Result count",
    "user-menu": "User menu",
}

# Last-resort CSS for CSD intents.
CSD_INTENT_FALLBACKS: dict[str, str] = {
    "search-input": "input[type='search'], input[placeholder*='Search']",
    "fast-buckets-trigger": "button:has(span:contains('Fast Buckets'))",
    "tag-explorer-trigger": "button:has(span:contains('Tag Explorer'))",
    "result-count-badge": "[class*='result-count'], [class*='ResultCount']",
    "user-menu": "[class*='user-menu'], [class*='UserMenu'], [class*='avatar']",
}

# Same shape for the AWS Console.
CONSOLE_INTENT_LABELS: dict[str, str] = {
    "search-input": "Find objects by prefix",
    "next-page-button": "Next page",
    "object-row": "Object",
}

CONSOLE_INTENT_FALLBACKS: dict[str, str] = {
    "search-input": "input[type='search']",
    "next-page-button": "button[data-analytics-name*='next']",
}


class IntentResolver:
    """Resolves intents for one tool. Use one instance per Runner."""

    def __init__(
        self,
        labels: dict[str, str],
        fallbacks: dict[str, str],
    ) -> None:
        self.labels = labels
        self.fallbacks = fallbacks

    def find(self, driver: "WebDriver", intent: str, timeout: float = 5.0) -> "Optional[WebElement]":
        """Return the first matching element, or None.

        Tries data-testid -> aria-label -> CSS fallback, with a single short
        timeout per attempt. If nothing matches, logs a warning and returns
        None; callers decide whether None is fatal.
        """
        from selenium.webdriver.common.by import By

        # 1. data-testid
        elem = self._try_by(driver, By.CSS_SELECTOR, f"[data-testid='{intent}']")
        if elem is not None:
            return elem

        # 2. aria-label
        label = self.labels.get(intent)
        if label:
            elem = self._try_by(driver, By.CSS_SELECTOR, f"[aria-label='{label}']")
            if elem is not None:
                return elem

        # 3. CSS fallback
        css = self.fallbacks.get(intent)
        if css:
            elem = self._try_by(driver, By.CSS_SELECTOR, css)
            if elem is not None:
                log.warning("resolved intent %r via CSS fallback (%r); ask the team to add data-testid",
                            intent, css)
                return elem

        log.warning("could not resolve intent %r in any strategy", intent)
        return None

    @staticmethod
    def _try_by(driver: "WebDriver", by: str, value: str) -> "Optional[WebElement]":
        from selenium.common.exceptions import NoSuchElementException

        try:
            return driver.find_element(by, value)
        except NoSuchElementException:
            return None


# Pre-built resolvers ready to import.
csd_resolver = IntentResolver(CSD_INTENT_LABELS, CSD_INTENT_FALLBACKS)
console_resolver = IntentResolver(CONSOLE_INTENT_LABELS, CONSOLE_INTENT_FALLBACKS)
