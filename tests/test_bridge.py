import asyncio

import pytest

from ad_arta import bridge
from ad_arta.bridge import Bridge


@pytest.fixture(autouse=True)
def quick_retry(monkeypatch):
    monkeypatch.setattr(bridge, "RETRY_AFTER", 0.01)


class FakeTV:
    def __init__(self):
        self.calls = []

    def go_to_art(self):
        self.calls.append("art")

    def go_to_tv(self, wake=True):
        self.calls.append("tv")


def run(coro):
    return asyncio.run(coro)


def test_first_report_only_records_state():
    async def scenario():
        tv = FakeTV()
        b = Bridge(tv)
        b.appletv_power_changed(False)
        await b.wait_idle()
        return tv.calls

    assert run(scenario()) == []


def test_sleep_goes_to_art_and_wake_goes_to_tv():
    async def scenario():
        tv = FakeTV()
        b = Bridge(tv)
        b.appletv_power_changed(True)
        b.appletv_power_changed(False)
        await b.wait_idle()
        b.appletv_power_changed(True)
        await b.wait_idle()
        return tv.calls

    assert run(scenario()) == ["art", "tv"]



def test_repeated_reports_do_nothing():
    async def scenario():
        tv = FakeTV()
        b = Bridge(tv)
        for _ in range(3):
            b.appletv_power_changed(True)
        await b.wait_idle()
        return tv.calls

    assert run(scenario()) == []



def test_tv_errors_are_logged_not_raised():
    class BrokenTV(FakeTV):
        def go_to_art(self):
            raise OSError("TV unreachable")

    async def scenario():
        b = Bridge(BrokenTV())
        b.appletv_power_changed(True)
        b.appletv_power_changed(False)
        await b.wait_idle()

    run(scenario())


def test_actions_never_overlap_and_latest_state_wins():
    import threading
    import time

    class SlowTV(FakeTV):
        def __init__(self):
            super().__init__()
            self.running = 0
            self.max_running = 0
            self.lock = threading.Lock()

        def _do(self, name):
            with self.lock:
                self.running += 1
                self.max_running = max(self.max_running, self.running)
            time.sleep(0.1)
            self.calls.append(name)
            with self.lock:
                self.running -= 1

        def go_to_art(self):
            self._do("art")

        def go_to_tv(self, wake=True):
            self._do("tv")

    async def scenario():
        tv = SlowTV()
        b = Bridge(tv)
        b.appletv_power_changed(True)
        b.appletv_power_changed(False)   # starts "art"
        await asyncio.sleep(0.02)
        b.appletv_power_changed(True)    # while "art" runs
        b.appletv_power_changed(False)   # superseded...
        b.appletv_power_changed(True)    # ...latest wins
        await b.wait_idle()
        return tv

    tv = run(scenario())
    assert tv.max_running == 1
    assert tv.calls == ["art", "tv"]


def test_failed_action_is_retried_once():
    class FlakyTV(FakeTV):
        def __init__(self, failures):
            super().__init__()
            self.failures = failures

        def go_to_tv(self, wake=True):
            self.calls.append("tv")
            if len(self.calls) <= self.failures:
                raise OSError("TV unreachable")

    async def scenario(failures):
        tv = FlakyTV(failures)
        b = Bridge(tv)
        b.appletv_power_changed(False)
        b.appletv_power_changed(True)
        await b.wait_idle()
        return tv.calls

    assert run(scenario(1)) == ["tv", "tv"]  # second try works
    assert run(scenario(9)) == ["tv", "tv"]  # gives up after one retry


def test_unconfirmed_wake_leaves_an_off_tv_off_until_confirmed():
    class OffTV(FakeTV):
        def go_to_tv(self, wake=True):
            self.calls.append(f"tv wake={wake}")
            return wake  # off: only a confirmed wake turns it on

    async def scenario(confirm):
        tv = OffTV()
        b = Bridge(tv)
        b.appletv_power_changed(False)
        b.appletv_power_changed(True, confirmed=False)  # MRP
        await b.wait_idle()
        if confirm:
            b.appletv_power_changed(True)  # Companion
        else:
            b.appletv_power_changed(False)  # safety net: it was a false wake
        await b.wait_idle()
        return tv.calls

    assert run(scenario(True)) == ["tv wake=False", "tv wake=True"]
    assert run(scenario(False)) == ["tv wake=False", "art"]


def test_confirmation_does_not_repeat_a_wake_that_was_acted_on():
    async def scenario():
        tv = FakeTV()  # on: go_to_tv acts at once
        b = Bridge(tv)
        b.appletv_power_changed(False)
        b.appletv_power_changed(True, confirmed=False)
        await b.wait_idle()
        b.appletv_power_changed(True)
        await b.wait_idle()
        return tv.calls

    assert run(scenario()) == ["tv"]
