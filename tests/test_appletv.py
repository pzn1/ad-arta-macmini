import asyncio
from types import SimpleNamespace

import pytest
from pyatv.const import PowerState

from ad_arta import appletv
from ad_arta.appletv import _EarlyWake

ON, OFF = PowerState.On, PowerState.Off


class FakeWatcher:
    """Mimics AppleTVWatcher._report updating the bridge state."""

    def __init__(self, on=False):
        self.bridge = SimpleNamespace(appletv_on=on)
        self.reports = []

    def wake_context(self):
        return ""

    def _report(self, state, confirmed=True):
        self.reports.append(state)
        self.bridge.appletv_on = state == ON


@pytest.fixture(autouse=True)
def short_confirm(monkeypatch):
    monkeypatch.setattr(appletv, "CONFIRM_WITHIN", 0.05)


def test_mrp_on_is_acted_on_immediately():
    async def run():
        w = FakeWatcher()
        ew = _EarlyWake(w)
        ew.powerstate_update(OFF, ON)
        assert w.reports == [ON]  # before any await
        ew.cancel()

    asyncio.run(run())


def test_wake_confirmed_by_companion_is_kept():
    async def run():
        w = FakeWatcher()
        ew = _EarlyWake(w)
        ew.powerstate_update(OFF, ON)
        await asyncio.sleep(0.01)
        ew.cancel()
        await asyncio.sleep(0.1)
        return w.reports

    assert asyncio.run(run()) == [ON]


def test_unconfirmed_wake_is_undone():
    async def run():
        w = FakeWatcher()
        _EarlyWake(w).powerstate_update(OFF, ON)
        await asyncio.sleep(0.1)
        return w.reports

    assert asyncio.run(run()) == [ON, OFF]


def test_old_check_does_not_undo_a_later_wake():
    """Wake, confirm, sleep, wake again: only the new wake's own check counts."""

    async def run():
        w = FakeWatcher()
        ew = _EarlyWake(w)
        ew.powerstate_update(OFF, ON)        # wake 1
        await asyncio.sleep(0.01)
        ew.cancel()            # confirmed
        ew.cancel()           # sleep (watcher reports Off)
        w._report(OFF)
        await asyncio.sleep(0.03)
        ew.powerstate_update(OFF, ON)        # wake 2, near wake 1's old deadline
        await asyncio.sleep(0.03)            # past wake 1's deadline
        assert w.reports == [ON, OFF, ON]
        ew.cancel()            # wake 2 confirmed
        await asyncio.sleep(0.1)
        return w.reports

    assert asyncio.run(run()) == [ON, OFF, ON]


def test_wake_flap_reports_once():
    async def run():
        w = FakeWatcher()
        ew = _EarlyWake(w)
        ew.powerstate_update(OFF, ON)
        ew.powerstate_update(ON, OFF)
        ew.powerstate_update(OFF, ON)
        ew.cancel()
        await asyncio.sleep(0.1)
        return w.reports

    assert asyncio.run(run()) == [ON]


def test_mrp_off_is_ignored():
    w = FakeWatcher(on=True)
    ew = _EarlyWake(w)
    ew.powerstate_update(ON, OFF)
    assert w.reports == [] and ew._task is None


# --- AppleTVWatcher.run_forever against a fake pyatv ------------------------

from pyatv.const import Protocol  # noqa: E402

from ad_arta.appletv import AppleTVWatcher  # noqa: E402
from ad_arta.config import AppleTVConfig  # noqa: E402


class FakeBridge:
    def __init__(self):
        self.appletv_on = None
        self.reports = []

    def appletv_power_changed(self, on, confirmed=True):
        self.appletv_on = on
        self.reports.append(on)
        self.confirmed = confirmed


class FakePower:
    def __init__(self, protocols, state):
        self._protocols = {p: SimpleNamespace(listener=None) for p in protocols}
        self.power_state = state
        self.listener = None

    def get(self, protocol):
        return self._protocols.get(protocol)


class FakeATV:
    def __init__(self, protocols, state=OFF):
        self.power = FakePower(protocols, state)
        self.listener = None
        self.closed = False
        self.connects = 0

    def close(self):
        self.closed = True


async def start_watcher(monkeypatch, atv):
    async def load():
        pass

    async def scan(loop, **kwargs):
        return [SimpleNamespace(name="TV")]

    async def connect(conf, loop, storage=None):
        atv.connects += 1
        return atv

    monkeypatch.setattr(
        appletv.FileStorage, "default_storage", lambda loop: SimpleNamespace(load=load)
    )
    monkeypatch.setattr(appletv.pyatv, "scan", scan)
    monkeypatch.setattr(appletv.pyatv, "connect", connect)
    watcher = AppleTVWatcher(AppleTVConfig(), FakeBridge())
    task = asyncio.ensure_future(watcher.run_forever())
    await asyncio.sleep(0.01)
    return watcher, task


def test_lost_connection_ends_the_wake_check(monkeypatch):
    async def run():
        atv = FakeATV([Protocol.Companion, Protocol.MRP])
        watcher, task = await start_watcher(monkeypatch, atv)
        atv.power.get(Protocol.MRP).listener.powerstate_update(OFF, ON)  # wake
        assert watcher.bridge.confirmed is False  # MRP's wake is unconfirmed
        watcher.connection_lost(OSError("gone"))
        await asyncio.sleep(0.1)  # past CONFIRM_WITHIN, still reconnecting
        task.cancel()
        assert atv.closed
        return watcher.bridge.reports

    assert asyncio.run(run()) == [False, True]  # the wake is not undone


def test_connection_without_companion_is_not_used(monkeypatch):
    async def run():
        atv = FakeATV([Protocol.MRP], state=ON)
        watcher, task = await start_watcher(monkeypatch, atv)
        task.cancel()
        assert atv.closed and atv.power.get(Protocol.MRP).listener is None
        return watcher.bridge.reports

    assert asyncio.run(run()) == []


def test_stable_connection_reconnects_at_once(monkeypatch):
    async def run(stable_after):
        monkeypatch.setattr(appletv, "STABLE_AFTER", stable_after)
        atv = FakeATV([Protocol.Companion, Protocol.MRP])
        watcher, task = await start_watcher(monkeypatch, atv)
        watcher.connection_lost(OSError("gone"))
        await asyncio.sleep(0.05)
        task.cancel()
        return atv.connects

    assert asyncio.run(run(0.0)) == 2   # had worked: no backoff
    assert asyncio.run(run(30.0)) == 1  # dropped right away: back off


def test_wake_with_unknown_state_arms_no_check():
    async def run():
        w = FakeWatcher(on=None)
        _EarlyWake(w).powerstate_update(OFF, ON)
        await asyncio.sleep(0.1)  # past CONFIRM_WITHIN
        return w.reports

    assert asyncio.run(run()) == [ON]


def test_signals_are_logged_with_the_companion_delay(caplog):
    caplog.set_level("INFO", logger="ad_arta.appletv")

    async def run():
        w = appletv.AppleTVWatcher(appletv.AppleTVConfig(), SimpleNamespace(appletv_on=False))
        w.bridge = FakeWatcher().bridge
        w._report = lambda state, confirmed=True: None
        w._early_wake = _EarlyWake(w)
        w._early_wake.powerstate_update(OFF, ON)
        await asyncio.sleep(0.01)
        w.powerstate_update(OFF, ON)

    asyncio.run(run())
    lines = [r.getMessage() for r in caplog.records]
    assert lines[0].startswith("Apple TV MRP: Off -> On (asleep ")
    assert lines[1].startswith("Apple TV Companion: Off -> On (") and "after MRP" in lines[1]
