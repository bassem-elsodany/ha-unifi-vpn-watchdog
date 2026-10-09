"""Injectable clock so the engine can be tested without real waiting."""
from __future__ import annotations

import time


class Clock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
