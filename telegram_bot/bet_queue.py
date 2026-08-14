from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .api import AsianOddsClient
from .config import load_config
from .maintenance import check_api_maintenance, exception_indicates_maintenance


@dataclass
class PlacementJob:
    client_api: AsianOddsClient
    resolved: Dict[str, Any]
    cfg: Dict[str, Any]
    chat: Optional[str]
    message_id: Optional[int]
    original_message: Optional[str] = None
    queued_at: float = field(default_factory=time.time)


class BetPlacementQueue:
    """
    Serializes bet placement so bets sharing the same bet_signature (same event +
    market + side) are never placed concurrently. Distinct bets may run in parallel,
    up to ``bet_queue_max_parallel`` workers, so rapid-fire tips on different events
    no longer have to wait for each other. Pauses while AsianOdds is under maintenance.
    """

    _MAX_WORKERS_CAP = 5

    def __init__(
        self,
        *,
        place_fn: Callable[..., Any],
        log_fn: Callable[[str], Any],
        max_workers: Optional[int] = None,
    ) -> None:
        self._place_fn = place_fn
        self._log_fn = log_fn
        self._max_workers = max_workers
        self._queue: Optional[asyncio.Queue[PlacementJob]] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._active_signatures: set = set()
        self._maintenance_paused = False
        self._last_maintenance_log = 0.0
        self._started = False

    def _ensure_queue(self) -> asyncio.Queue[PlacementJob]:
        """Lazily initialize the queue on first access (ensures running event loop)."""
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    def start(self) -> None:
        """Mark queue as started; worker(s) are created lazily on first enqueue to ensure a running event loop."""

    @property
    def size(self) -> int:
        if self._queue is None:
            return 0
        return self._queue.qsize()

    @property
    def maintenance_paused(self) -> bool:
        return self._maintenance_paused

    async def enqueue(self, job: PlacementJob) -> None:
        # Lazily start the worker pool on first use (ensures a running event loop exists).
        if not self._started:
            self._started = True
            loop = asyncio.get_event_loop()
            n_workers = self._max_workers
            if n_workers is None:
                cfg = load_config()
                n_workers = int(cfg.get("bet_queue_max_parallel", 1) or 1)
            n_workers = max(1, min(int(n_workers), self._MAX_WORKERS_CAP))
            self._worker_tasks = [
                loop.create_task(self._run_worker(i)) for i in range(n_workers)
            ]

        cfg = load_config()
        max_size = int(cfg.get("bet_queue_max_size", 30))
        if self.size >= max_size:
            await self._log_fn(
                "⚠️ Bet queue is full; this tip was not queued. Try again later or increase bet_queue_max_size."
            )
            return

        position = self.size + 1
        await self._ensure_queue().put(job)
        if position > 1:
            await self._log_fn(f"📥 Bet queued (position {position})…")

    async def _run_worker(self, worker_id: int) -> None:
        while True:
            job = await self._ensure_queue().get()
            key = self._job_key(job)
            try:
                await self._wait_until_api_available(job.client_api)

                if key:
                    await self._acquire_signature_slot(key)
                try:
                    while True:
                        try:
                            await self._place_fn(
                                job.client_api,
                                job.resolved,
                                job.cfg,
                                job.chat,
                                job.message_id,
                                job.original_message,
                            )
                            break
                        except Exception as exc:
                            is_maint, reason = exception_indicates_maintenance(exc)
                            if not is_maint:
                                break
                            await self._notify_maintenance(reason)
                            await self._wait_until_api_available(job.client_api)
                finally:
                    if key:
                        self._release_signature_slot(key)

                cfg = load_config()
                delay = float(cfg.get("bet_queue_delay_seconds", 3.0) or 0.0)
                if delay > 0:
                    await asyncio.sleep(delay)
            except Exception:
                pass
            finally:
                self._ensure_queue().task_done()

    @staticmethod
    def _job_key(job: PlacementJob) -> str:
        """
        Grouping key for concurrent-safety. Bets that share a bet_signature are the
        same tip (event + market + side), so they must serialize. Fall back to
        eventId, then a shared key, so bets that cannot be keyed are never concurrent.
        """
        resolved = job.resolved or {}
        sig = resolved.get("bet_signature")
        if sig:
            return f"sig:{sig}"
        event_id = resolved.get("eventId")
        if event_id:
            return f"ev:{event_id}"
        return "global"

    async def _acquire_signature_slot(self, key: str) -> None:
        # Single-threaded event loop: check-then-add below is atomic (no await in between).
        while key in self._active_signatures:
            await asyncio.sleep(0.05)
        self._active_signatures.add(key)

    def _release_signature_slot(self, key: str) -> None:
        self._active_signatures.discard(key)

    async def _wait_until_api_available(self, client_api: AsianOddsClient) -> None:
        cfg = load_config()
        interval = float(cfg.get("maintenance_check_interval_seconds", 30.0) or 30.0)
        interval = max(5.0, interval)

        while True:
            is_maint, reason = await asyncio.to_thread(check_api_maintenance, client_api)
            if not is_maint:
                if self._maintenance_paused:
                    await self._log_fn("✅ AsianOdds API is available again. Resuming bet queue…")
                    self._maintenance_paused = False
                return

            await self._notify_maintenance(reason)
            await asyncio.sleep(interval)

    async def _notify_maintenance(self, reason: str) -> None:
        now = time.time()
        cfg = load_config()
        interval = float(cfg.get("maintenance_check_interval_seconds", 30.0) or 30.0)
        interval = max(5.0, interval)
        if self._maintenance_paused and (now - self._last_maintenance_log) < interval:
            return
        detail = f" ({reason})" if reason else ""
        await self._log_fn(
            f"🛑 AsianOdds appears to be under maintenance. Bet queue paused.{detail}"
        )
        self._maintenance_paused = True
        self._last_maintenance_log = now
