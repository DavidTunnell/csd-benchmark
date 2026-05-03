"""Shared helpers used by all three tool runners."""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def get_logger(name: str) -> logging.Logger:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
    return logging.getLogger(name)


@contextmanager
def stopwatch():
    """Yield a callable that returns elapsed seconds since enter.

        with stopwatch() as elapsed:
            do_work()
            print(elapsed())
    """
    start = time.perf_counter()
    yield lambda: time.perf_counter() - start
