"""Background thread that auto-snapshots a sandbox before its timeout expires.

Adapted from MLE-Agent's ``JITSandbox._monitor_for_snapshot`` pattern.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from modal import Image, Sandbox

logger = logging.getLogger(__name__)

SNAPSHOT_SECONDS_BEFORE_TIMEOUT = 30
POLL_INTERVAL = 0.5


class SpriteMonitor:
    """Daemon thread that watches a sandbox's remaining lifetime.

    * Snapshots the filesystem ~30 s before timeout.
    * Calls *on_snapshot* with the resulting :class:`Image`.
    * Calls *on_expiry* when the sandbox has timed out so the owner can
      update the registry.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        timeout: int,
        on_snapshot: Callable[[Image], None],
        on_expiry: Callable[[], None],
        *,
        started_at: float | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._timeout = timeout
        self._started_at = started_at or time.time()
        self._on_snapshot = on_snapshot
        self._on_expiry = on_expiry
        self._stop_event = threading.Event()
        self._snapshot_taken = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def snapshot_taken(self) -> bool:
        return self._snapshot_taken

    def _run(self) -> None:
        while not self._stop_event.is_set():
            elapsed = time.time() - self._started_at
            remaining = self._timeout - elapsed

            # Snapshot window: [SNAPSHOT_SECONDS - 1, SNAPSHOT_SECONDS + 1]
            low = SNAPSHOT_SECONDS_BEFORE_TIMEOUT - 1
            high = SNAPSHOT_SECONDS_BEFORE_TIMEOUT + 1
            if low < remaining <= high and not self._snapshot_taken:
                logger.info(
                    "[SpriteMonitor] Snapshotting at %.1fs (%ds before timeout)",
                    elapsed,
                    SNAPSHOT_SECONDS_BEFORE_TIMEOUT,
                )
                image = self._sandbox.snapshot_filesystem(timeout=120)
                logger.info(
                    "[SpriteMonitor] Snapshot complete. ID: %s", image.object_id
                )
                self._snapshot_taken = True
                self._on_snapshot(image)

            if remaining <= 0:
                logger.info(
                    "[SpriteMonitor] Sandbox expired at %.1fs", elapsed
                )
                self._on_expiry()
                break

            time.sleep(POLL_INTERVAL)
