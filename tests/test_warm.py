import pytest
import websocket

from ad_arta import tv as tv_mod
from ad_arta.config import TVConfig
from ad_arta.frame import drain
from ad_arta.tv import FrameTV, _Warm


class FakeWS:
    """websocket with scripted recv results: opcodes, or exceptions."""

    def __init__(self, script=()):
        self.script = list(script)
        self.timeout = 8

    def gettimeout(self):
        return self.timeout

    def settimeout(self, t):
        self.timeout = t

    def recv_data_frame(self, control_frame=True):
        if not self.script:
            raise websocket.WebSocketTimeoutException("idle")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, None


def test_drain_idle_connection_is_alive():
    ws = FakeWS([websocket.ABNF.OPCODE_PING, websocket.ABNF.OPCODE_TEXT])
    assert drain(ws) is True
    assert ws.timeout == 8  # restored


def test_drain_detects_close_and_errors():
    assert drain(FakeWS([websocket.ABNF.OPCODE_CLOSE])) is False
    assert drain(FakeWS([ConnectionResetError()])) is False
    assert drain(FakeWS([websocket.WebSocketConnectionClosedException()])) is False
    assert drain(None) is False


class FakeClient:
    def __init__(self, ws=None):
        self.connection = ws or FakeWS()
        self.closed = False

    def close(self):
        self.closed = True


def test_warm_opens_then_keeps_and_reopens_after_close():
    opened = []

    def opener():
        c = FakeClient()
        opened.append(c)
        return c

    w = _Warm("art", opener)
    w.keep()
    assert w.client is opened[0]
    w.keep()  # alive: nothing new
    assert len(opened) == 1
    w.client.connection.script = [websocket.ABNF.OPCODE_CLOSE]
    w.keep()  # detects close, drops
    assert w.client is None and opened[0].closed
    w.keep()  # reopens
    assert w.client is opened[1]


def test_warm_backs_off_after_failed_open(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(tv_mod.time, "monotonic", lambda: now[0])
    calls = []

    def opener():
        calls.append(now[0])
        raise OSError("unreachable")

    w = _Warm("remote", opener)
    w.keep()
    w.keep()  # within backoff: no new try
    assert calls == [100.0]
    now[0] += 5
    w.keep()
    assert calls == [100.0, 105.0]
    now[0] += 5  # backoff doubled to 10 s
    w.keep()
    assert len(calls) == 2


# --- FrameTV using warm connections ----------------------------------------


class FakeArt(FakeClient):
    timeout = 8

    def __init__(self, current="on", fail=None):
        super().__init__()
        self.current = current
        self.fail = fail
        self.set_calls = []

    def get_artmode(self):
        if self.fail:
            raise self.fail
        return self.current

    def set_artmode_confirmed(self, wanted, timeout=None):
        self.set_calls.append(wanted)
        self.current = wanted


class FakeRemote(FakeClient):
    def __init__(self):
        super().__init__()
        self.keys = []

    def send_key(self, key):
        self.keys.append(key)


def make_tv(tmp_path, input_key="KEY_HDMI2"):
    return FrameTV(TVConfig(host="1.2.3.4", token_file=str(tmp_path / "t"), input_key=input_key))


def no_fresh(*a, **kw):
    raise AssertionError("fresh path used")


def test_go_to_tv_uses_warm_connections_only(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    art, remote = FakeArt("on"), FakeRemote()
    tv._warm_art.client, tv._warm_remote.client = art, remote
    monkeypatch.setattr(tv, "set_art_mode", no_fresh)
    monkeypatch.setattr(tv, "ensure_on", no_fresh)
    monkeypatch.setattr(tv, "_remote", no_fresh)
    tv.go_to_tv()
    assert art.set_calls == ["off"]
    assert remote.keys == ["KEY_HDMI2"]


def test_go_to_art_already_on_does_nothing(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    art = FakeArt("on")
    tv._warm_art.client = art
    monkeypatch.setattr(tv, "set_art_mode", no_fresh)
    tv.go_to_art()
    assert art.set_calls == []


def test_warm_failure_falls_back_to_fresh_path(tmp_path, monkeypatch):
    tv = make_tv(tmp_path, input_key="")
    art = FakeArt(fail=tv_mod.exceptions.ConnectionFailure("timeout"))
    tv._warm_art.client = art
    fresh = []
    monkeypatch.setattr(tv, "power_state", lambda timeout=8: "on")
    monkeypatch.setattr(tv, "set_art_mode", lambda on, just_woke=False: fresh.append(on))
    tv.go_to_art()
    assert fresh == [True]
    assert tv._warm_art.client is None and art.closed


def test_dead_warm_connection_is_not_used(tmp_path, monkeypatch):
    tv = make_tv(tmp_path, input_key="")
    art = FakeArt("off")
    art.connection.script = [websocket.ABNF.OPCODE_CLOSE]
    tv._warm_art.client = art
    fresh = []
    monkeypatch.setattr(tv, "power_state", lambda timeout=8: "on")
    monkeypatch.setattr(tv, "set_art_mode", lambda on, just_woke=False: fresh.append(on))
    tv.go_to_art()
    assert art.set_calls == [] and fresh == [True]


def test_warm_key_failure_falls_back_to_fresh_remote(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    tv._warm_art.client = FakeArt("on")
    bad = FakeRemote()
    bad.send_key = lambda key: (_ for _ in ()).throw(BrokenPipeError())
    tv._warm_remote.client = bad
    fresh = FakeRemote()
    monkeypatch.setattr(tv, "_remote", lambda *a, **kw: fresh)
    tv.go_to_tv()
    assert fresh.keys == ["KEY_HDMI2"] and fresh.closed
    assert tv._warm_remote.client is None


def test_no_warm_connection_uses_fresh_path(tmp_path, monkeypatch):
    tv = make_tv(tmp_path, input_key="")
    fresh = []
    monkeypatch.setattr(tv, "ensure_on", lambda: True)
    monkeypatch.setattr(tv, "set_art_mode", lambda on, just_woke=False: fresh.append((on, just_woke)))
    tv.go_to_tv()
    assert fresh == [(False, True)]


def test_input_key_sent_before_leaving_art_mode(tmp_path):
    tv = make_tv(tmp_path)
    calls = []
    art, remote = FakeArt("on"), FakeRemote()
    art.set_artmode_confirmed = lambda wanted, timeout=None: calls.append(f"art {wanted}")
    remote.send_key = lambda key: calls.append(key)
    tv._warm_art.client, tv._warm_remote.client = art, remote
    tv.go_to_tv()
    assert calls == ["KEY_HDMI2", "art off"]

def test_tv_back_on_skips_the_reopen_backoff(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    for w in (tv._warm_art, tv._warm_remote):
        w._next_try = float("inf")  # deep in backoff
        w._backoff = 60.0
    states = iter(["off", "off", "standby", "on", "on"])
    monkeypatch.setattr(tv, "power_state", lambda timeout=8: next(states))
    tv._watch_power()  # off
    tv._watch_power()  # still off
    assert tv._warm_art._next_try == float("inf")
    tv._watch_power()  # standby
    assert tv._warm_art._next_try == float("inf")
    tv._watch_power()  # on: retry now
    assert tv._warm_art._next_try == 0.0 and tv._warm_remote._next_try == 0.0
    assert tv._warm_art._backoff == tv_mod.REOPEN_BACKOFF[0]
    tv._warm_art._next_try = 99.0  # failed again while on (e.g. art service stopped)
    tv._watch_power()  # still on: no new reset, backoff applies
    assert tv._warm_art._next_try == 99.0


def test_power_not_probed_while_both_connections_open(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    tv._warm_art.client, tv._warm_remote.client = FakeArt("on"), FakeRemote()
    monkeypatch.setattr(tv, "power_state", no_fresh)
    tv._watch_power()


def test_fresh_input_key_runs_alongside_leaving_art_mode(tmp_path, monkeypatch):
    import threading

    tv = make_tv(tmp_path)
    both = threading.Barrier(2, timeout=2)  # fails unless both run at once
    calls = []

    class SlowRemote(FakeRemote):
        def send_key(self, key):
            both.wait()
            calls.append(key)

    def fresh_art(on, just_woke=False):
        both.wait()
        calls.append(f"art {on}")

    monkeypatch.setattr(tv, "_remote", lambda *a, **kw: SlowRemote())
    monkeypatch.setattr(tv, "ensure_on", lambda: False)
    monkeypatch.setattr(tv, "set_art_mode", fresh_art)
    tv.go_to_tv()
    assert sorted(calls) == ["KEY_HDMI2", "art False"]


def test_go_to_art_leaves_a_tv_that_is_off_alone(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    monkeypatch.setattr(tv, "power_state", lambda timeout=8: "standby")
    monkeypatch.setattr(tv, "ensure_on", no_fresh)  # no Wake-on-LAN
    monkeypatch.setattr(tv, "set_art_mode", no_fresh)
    tv.go_to_art()


def test_input_key_failing_everywhere_still_leaves_art_mode(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    art, bad = FakeArt("on"), FakeRemote()
    bad.send_key = lambda key: (_ for _ in ()).throw(BrokenPipeError())
    tv._warm_art.client, tv._warm_remote.client = art, bad
    monkeypatch.setattr(tv, "_remote", lambda *a, **kw: bad)
    tv.go_to_tv()
    assert art.set_calls == ["off"]


def test_go_to_tv_waits_for_art_service_after_tv_turned_on(tmp_path, monkeypatch):
    tv = make_tv(tmp_path, input_key="")
    fresh = []
    states = iter(["on", "standby", "on"])
    monkeypatch.setattr(tv, "power_state", lambda timeout=8: next(states))
    monkeypatch.setattr(tv, "ensure_on", lambda: False)
    monkeypatch.setattr(tv, "set_art_mode", lambda on, just_woke=False: fresh.append(just_woke))
    tv._watch_power()  # on at startup: not a turn-on
    tv.go_to_tv()
    tv._watch_power()  # standby
    tv._watch_power()  # turned on (motion sensor)
    tv.go_to_tv()
    assert fresh == [False, True]


def test_power_key_uses_warm_remote(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    sent = []
    remote = FakeRemote()
    remote.send_key = lambda key, key_press_delay=None: sent.append((key, key_press_delay))
    tv._warm_remote.client = remote
    monkeypatch.setattr(tv, "_remote", no_fresh)
    tv._press_power()
    assert sent == [("KEY_POWER", 1)]


def test_unconfirmed_wake_does_not_wake_a_tv_that_is_off(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    monkeypatch.setattr(tv, "power_state", lambda timeout=8: "standby")
    monkeypatch.setattr(tv, "ensure_on", no_fresh)  # no Wake-on-LAN
    monkeypatch.setattr(tv, "_remote", no_fresh)    # no input key either
    assert tv.go_to_tv(wake=False) is False
    fresh = []
    monkeypatch.setattr(tv, "ensure_on", lambda: True)
    monkeypatch.setattr(tv, "_remote", lambda *a, **kw: FakeRemote())
    monkeypatch.setattr(tv, "set_art_mode", lambda on, just_woke=False: fresh.append(just_woke))
    assert tv.go_to_tv(wake=True) is True and fresh == [True]


def test_warm_keeps_a_connection_installed_while_opening():
    other, opened = FakeClient(), []

    def opener():
        w.client = other  # someone else installed one meanwhile
        opened.append(FakeClient())
        return opened[0]

    w = _Warm("art", opener)
    w.keep()
    assert w.client is other and opened[0].closed


def test_input_key_is_sent_again_after_waking_the_tv(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    remotes = []

    def remote(*a, **kw):
        r = FakeRemote()
        if not remotes:  # the TV is still off for the first one
            r.send_key = lambda key: (_ for _ in ()).throw(ConnectionRefusedError())
        remotes.append(r)
        return r

    monkeypatch.setattr(tv, "_remote", remote)
    monkeypatch.setattr(tv, "ensure_on", lambda: True)
    monkeypatch.setattr(tv, "set_art_mode", lambda on, just_woke=False: None)
    tv.go_to_tv()
    assert len(remotes) == 2 and remotes[1].keys == ["KEY_HDMI2"]


def test_warm_change_gives_up_quickly(tmp_path):
    tv = make_tv(tmp_path, input_key="")
    art = FakeArt("on")
    seen = []
    art.set_artmode_confirmed = lambda wanted, timeout=None: seen.append(timeout)
    tv._warm_art.client = art
    tv.go_to_tv()
    assert seen == [tv_mod.WARM_CONFIRM_TIMEOUT]


def test_unconfirmed_wake_leaves_tv_in_standby_alone(tmp_path, monkeypatch):
    # A Frame in standby answers get_artmode on the warm connection but never
    # confirms the change. An unconfirmed wake must not then Wake-on-LAN it.
    tv = make_tv(tmp_path, input_key="")
    art = FakeArt("on")

    def no_confirm(wanted, timeout=None):
        raise tv_mod.exceptions.ConnectionFailure("Websocket Time out")

    art.set_artmode_confirmed = no_confirm
    tv._warm_art.client = art
    monkeypatch.setattr(tv, "power_state", lambda timeout=8: "standby")
    monkeypatch.setattr(tv, "ensure_on", no_fresh)
    monkeypatch.setattr(tv, "set_art_mode", no_fresh)
    assert tv.go_to_tv(wake=False) is False
    assert tv._warm_art.client is None  # dropped
