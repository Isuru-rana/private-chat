"""Growing delay after repeated wrong vault passwords."""

import time
from collections.abc import Callable

FREE_ATTEMPTS = 5
MAX_DELAY = 60.0


class Throttle:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.failures = 0
        self._until = 0.0

    def retry_after(self) -> float:
        return max(0.0, self._until - self.clock())

    def fail(self) -> None:
        self.failures += 1
        if self.failures > FREE_ATTEMPTS:
            exp = min(self.failures - FREE_ATTEMPTS, 6)  # 2**6 > MAX_DELAY; avoids float overflow
            self._until = self.clock() + min(2.0**exp, MAX_DELAY)

    def success(self) -> None:
        self.failures = 0
        self._until = 0.0
