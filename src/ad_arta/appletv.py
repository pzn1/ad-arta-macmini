"""Watch the Apple TV's power state with pyatv and keep the connection alive."""

from __future__ import annotations

import asyncio
import logging
import time

import pyatv
from pyatv.const import PowerState, Protocol
from pyatv.interface import DeviceListener, PowerListener
from pyatv.storage.file_storage import FileStorage

from .bridge import Bridge
from .config import AppleTVConfig

log = logging.getLogger(__name__)


# Companion usually confirms a real wake 4-15 s after MRP, but after a long
# sleep it took 32 s and once 111 s. If it hasn't within this many seconds,
# the MRP "On" is taken as false: go back to Art Mode. Keep this generous:
# no false MRP "On" has been seen, and undoing a real wake is costly, because
# the Frame entering Art Mode puts the waking Apple TV to sleep over HDMI-CEC.
CONFIRM_WITHIN = 300.0

# A connection that lasted this long had worked: when it drops, reconnect at
# once instead of after the backoff.
STABLE_AFTER = 30.0


def _ago(since: float | None) -> str:
    if since is None:
        return "?"
    s = time.monotonic() - since
    return f"{s / 60:.0f} min" if s >= 120 else f"{s:.1f} s"


class _EarlyWake(PowerListener):
    """Act on MRP's wake report, ~9 s before Companion's.

    pyatv only relays Companion power changes to the user listener, and
    Companion reports a wake 4-15 s after MRP. So MRP "On" is acted on at
    once. As a safety net against a false MRP "On", the wake is undone if
    Companion reports nothing at all within CONFIRM_WITHIN seconds. Any
    Companion report (On or Off) or a lost connection ends the check, and
    each new wake starts a fresh one. MRP "Off" is ignored (MRP flaps
    On/Off/On at wake; Companion reports sleep at once).
    """

    def __init__(self, watcher: "AppleTVWatcher") -> None:
        self._watcher = watcher
        self._task: asyncio.Task | None = None
        self._woke_at: float | None = None

    def powerstate_update(self, old_state: PowerState, new_state: PowerState) -> None:
        w = self._watcher
        was_on = w.bridge.appletv_on
        detail = w.wake_context() if new_state == PowerState.On and was_on is False else ""
        log.info("Apple TV MRP: %s -> %s%s", old_state.name, new_state.name, detail)
        if new_state != PowerState.On or was_on is True:
            return
        w._report(PowerState.On, confirmed=False)
        self.cancel()
        if was_on is False:  # None: the bridge only recorded it, nothing to undo
            self._woke_at = time.monotonic()
            self._task = asyncio.ensure_future(self._undo_if_unconfirmed())

    def pending_since(self) -> float | None:
        """When the wake still waiting for Companion was seen, else None."""
        if self._task is not None and not self._task.done():
            return self._woke_at
        return None

    def cancel(self) -> None:
        """Drop the pending wake check: Companion spoke, or the connection is gone."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _undo_if_unconfirmed(self) -> None:
        await asyncio.sleep(CONFIRM_WITHIN)
        w = self._watcher
        if w.bridge.appletv_on is not True:
            return
        log.warning("Apple TV wake wasn't confirmed within %.0fs, back to Art Mode", CONFIRM_WITHIN)
        w._report(PowerState.Off)


class AppleTVWatcher(PowerListener, DeviceListener):
    def __init__(self, cfg: AppleTVConfig, bridge: Bridge) -> None:
        self.cfg = cfg
        self.bridge = bridge
        self._disconnected = asyncio.Event()
        self._early_wake: _EarlyWake | None = None
        self._connected_at: float | None = None
        self._off_since: float | None = None  # last time the bridge saw it sleep

    def wake_context(self) -> str:
        return f" (asleep {_ago(self._off_since)}, connected {_ago(self._connected_at)})"

    # pyatv callbacks
    def powerstate_update(self, old_state: PowerState, new_state: PowerState) -> None:
        # pyatv's facade only relays the main power instance: Companion.
        pending = self._early_wake.pending_since() if self._early_wake is not None else None
        log.info("Apple TV Companion: %s -> %s%s", old_state.name, new_state.name,
                 f" ({_ago(pending)} after MRP)" if pending is not None else "")
        if self._early_wake is not None:
            self._early_wake.cancel()
        self._report(new_state)

    def connection_lost(self, exception: Exception) -> None:
        log.warning("Lost connection to Apple TV: %s", exception)
        self._disconnected.set()

    def connection_closed(self) -> None:
        self._disconnected.set()

    def _report(self, state: PowerState, confirmed: bool = True) -> None:
        if state == PowerState.On:
            self.bridge.appletv_power_changed(True, confirmed)
        elif state == PowerState.Off:
            if self.bridge.appletv_on is not False:
                self._off_since = time.monotonic()
            self.bridge.appletv_power_changed(False)

    async def _find(self, loop, storage):
        kwargs = {"storage": storage}
        if self.cfg.identifier:
            kwargs["identifier"] = self.cfg.identifier
        if self.cfg.host:
            kwargs["hosts"] = [self.cfg.host]
        devices = await pyatv.scan(loop, **kwargs)
        return devices[0] if devices else None

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        # Same credential file that `atvremote` writes when you pair.
        storage = FileStorage.default_storage(loop)
        await storage.load()
        backoff = 5
        while True:
            atv = None
            was_stable = False
            try:
                conf = await self._find(loop, storage)
                if conf is None:
                    raise RuntimeError("Apple TV not found on the network")
                atv = await pyatv.connect(conf, loop, storage=storage)
                if atv.power.get(Protocol.Companion) is None:
                    # Without Companion nothing reports sleep or confirms a
                    # wake (pyatv would relay MRP instead): try again.
                    raise RuntimeError("connected without Companion")
                log.info("Connected to %s", conf.name)
                backoff = 5
                connected = loop.time()
                self._connected_at = time.monotonic()
                self._disconnected.clear()
                atv.listener = self
                atv.power.listener = self
                mrp_power = atv.power.get(Protocol.MRP)
                if mrp_power is not None:
                    # pyatv keeps listeners as weak refs, so keep it on self
                    self._early_wake = _EarlyWake(self)
                    mrp_power.listener = self._early_wake
                self._report(atv.power.power_state)
                await self._disconnected.wait()
                was_stable = loop.time() - connected >= STABLE_AFTER
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Apple TV connection problem: %s", exc)
            finally:
                # Disconnected, nothing can confirm a wake now. The first
                # report after reconnecting settles the state instead.
                if self._early_wake is not None:
                    self._early_wake.cancel()
                    self._early_wake = None
                if atv is not None:
                    atv.close()
            if was_stable:
                continue
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
