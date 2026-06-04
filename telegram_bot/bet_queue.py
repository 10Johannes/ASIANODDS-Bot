from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

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
    Serializes bet placement so only one tip is placed (and confirmed) at a time.
    Pauses while AsianOdds is under maintenance.
    """

    def __init__(
        self,
        *,
        place_fn: Callable[..., Any],
        log_fn: Callable[[str], Any],
    ) -> None:
        self._place_fn = place_fn
        self._log_fn = log_fn
        self._queue: Optional[asyncio.Queue[PlacementJob]] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._maintenance_paused = False
        self._last_maintenance_log = 0.0
        self._started = False

    def _ensure_queue(self) -> asyncio.Queue[PlacementJob]:
        """Lazily initialize the queue on first access (ensures running event loop)."""
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    def start(self) -> None:
        """Start the bet placement worker (safe to call even without running event loop)."""
        if self._started:
            return
        self._started = True
        
        # Use ensure_future which works with or without a running loop
        try:
            self._worker_task = asyncio.ensure_future(self._run_worker())
        except RuntimeError:
            # If there's no event loop at all, get or create one
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                self._worker_task = loop.create_task(self._run_worker())
            except Exception:
                # Fallback: will retry on next call
                self._started = False

    @property
    def size(self) -> int:
        if self._queue is None:
            return 0
        return self._queue.qsize()

    @property
    def maintenance_paused(self) -> bool:
        return self._maintenance_paused

    async def enqueue(self, job: PlacementJob) -> None:
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

    async def _run_worker(self) -> None:
        while True:
            job = await self._ensure_queue().get()
            try:
                await self._wait_until_api_available(job.client_api)

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

                cfg = load_config()
                delay = float(cfg.get("bet_queue_delay_seconds", 3.0) or 0.0)
                if delay > 0:
                    await asyncio.sleep(delay)
            except Exception:
                pass
            finally:
                self._ensure_queue().task_done()

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
