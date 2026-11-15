"""Turn Apple TV power changes into Frame TV actions."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

log = logging.getLogger(__name__)

RETRY_AFTER = 2.0  # seconds before the one retry of a failed TV action


class TV(Protocol):
    def go_to_art(self) -> None: ...
    def go_to_tv(self, wake: bool) -> bool: ...


class Bridge:
    """One worker applies the latest wanted state; TV actions never overlap.

    A TV action runs in a thread and can't be cancelled halfway. So instead
    of cancelling, a newer report only changes the wanted state, and the
    worker applies it as soon as the running action is done. A failed action
    is retried once; otherwise the TV would stay wrong until the Apple TV's
    next power change.

    A wake reported with confirmed=False (MRP's early signal) may be false,
    so it doesn't turn on a TV that is off: go_to_tv(wake=False) then
    returns False, and runs again once the wake is confirmed.
    """

    def __init__(self, tv: TV) -> None:
        self.tv = tv
        self._appletv_on: bool | None = None
        self._wanted = False
        self._confirmed = True
        self._tv_left_off = False  # go_to_tv skipped: TV off, wake unconfirmed
        self._changed = asyncio.Event()
        self._worker: asyncio.Task | None = None

    @property
    def appletv_on(self) -> bool | None:
        return self._appletv_on

    def appletv_power_changed(self, on: bool, confirmed: bool = True) -> None:
        """Called on every power report, including the first one after (re)connecting.

        The first report ever only records the state. After that, only real
        changes trigger an action, so a reconnect doesn't fight a user who is,
        say, watching a TV app with the Samsung remote.
        """
        previous, self._appletv_on = self._appletv_on, on
        if previous is None:
            return
        if previous == on:
            if on and confirmed and not self._confirmed:
                self._confirmed = True
                if self._tv_left_off:
                    self._start()
            return
        log.info("Apple TV turned %s", "on" if on else "off")
        self._wanted = on
        self._confirmed = confirmed
        self._start()

    def _start(self) -> None:
        self._changed.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        retried = False
        while self._changed.is_set():
            self._changed.clear()
            try:
                self._tv_left_off = False
                if not self._wanted:
                    await asyncio.to_thread(self.tv.go_to_art)
                elif await asyncio.to_thread(self.tv.go_to_tv, self._confirmed) is False:
                    log.info("TV is off: waking it once the Apple TV's wake is confirmed")
                    self._tv_left_off = True
                    if self._confirmed:  # confirmed while the action ran
                        self._changed.set()
                retried = False
            except Exception:
                log.exception("Could not update the TV")
                if retried:
                    retried = False
                    continue
                retried = True
                if not self._changed.is_set():  # no newer report: wait a moment
                    await asyncio.sleep(RETRY_AFTER)
                self._changed.set()

    async def wait_idle(self) -> None:
        if self._worker:
            await asyncio.gather(self._worker, return_exceptions=True)
