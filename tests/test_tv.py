import pytest
import websocket

from ad_arta import tv as tv_mod
from ad_arta.config import TVConfig
from ad_arta.tv import FrameTV


def make_tv(tmp_path):
    return FrameTV(TVConfig(host="1.2.3.4", token_file=str(tmp_path / "t.txt")))


def test_pair_timeout_gives_friendly_error(tmp_path, monkeypatch):
    class FakeTVWS:
        def __init__(self, *a, **kw):
            pass

        def open(self):
            raise websocket.WebSocketTimeoutException("Connection timed out")

        def close(self):
            pass

    monkeypatch.setattr(tv_mod, "SamsungTVWS", FakeTVWS)
    with pytest.raises(RuntimeError, match="pair-tv"):
        make_tv(tmp_path).pair()


def test_pair_uses_long_timeout(tmp_path, monkeypatch):
    seen = {}

    class FakeTVWS:
        def __init__(self, *a, **kw):
            seen.update(kw)

        def open(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(tv_mod, "SamsungTVWS", FakeTVWS)
    make_tv(tmp_path).pair()
    assert seen["timeout"] == 60


# --- FrameArt.open ---------------------------------------------------------


def test_art_mode_returns_none_when_art_service_silent(tmp_path, monkeypatch):
    from samsungtvws import exceptions

    class SilentArt:
        def get_artmode(self):
            raise exceptions.ConnectionFailure("Websocket Time out")

        def close(self):
            pass

    tv = make_tv(tmp_path)
    monkeypatch.setattr(tv, "_art", lambda: SilentArt())
    assert tv.art_mode() is None


# --- FrameArt.set_artmode_confirmed / wait_ready --------------------------


class FakeArt:
    """Stands in for FrameArt. `ready_after_power` decides if a press helps."""

    def __init__(self, tv, ready=True, ready_after_power=True, mode="off",
                 get_fails=False):
        self.tv = tv
        self.ready = ready
        self.ready_after_power = ready_after_power
        self.mode = mode
        self.get_fails = get_fails
        self.opened_with = None
        self.set_calls = []
        self.closed = False
        self.opens = 0
        self.unreachable_opens = 0

    def open(self, ready_wait, wait_if_absent=None):
        if self.opened_with is None:
            self.opened_with = (ready_wait, wait_if_absent)
        self.opens += 1
        if self.unreachable_opens:
            self.unreachable_opens -= 1
            raise ConnectionRefusedError("TV is switching")
        if self.tv.power_presses and self.ready_after_power:
            self.ready = True

    def get_artmode(self):
        from samsungtvws import exceptions

        if self.get_fails:
            raise exceptions.ConnectionFailure("Websocket Time out")
        return self.mode

    def set_artmode_confirmed(self, wanted):
        self.set_calls.append(wanted)

    def close(self):
        self.closed = True


def tv_with_fake_art(tmp_path, monkeypatch, **art_kw):
    tv = make_tv(tmp_path)
    tv.power_presses = 0

    def press():
        tv.power_presses += 1

    unreachable = art_kw.pop("unreachable_opens", 0)
    art = FakeArt(tv, **art_kw)
    art.unreachable_opens = unreachable
    monkeypatch.setattr(tv, "_art", lambda: art)
    monkeypatch.setattr(tv_mod, "WAKE_WAIT", 0.3)
    monkeypatch.setattr(tv_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(tv, "_press_power", press)
    return tv, art


def test_set_art_mode_when_service_running(tmp_path, monkeypatch):
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, ready=True, mode="off")
    tv.set_art_mode(True)
    assert art.set_calls == ["on"]
    assert tv.power_presses == 0
    assert art.closed


def test_set_art_mode_already_in_wanted_state(tmp_path, monkeypatch):
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, ready=True, mode="on")
    tv.set_art_mode(True)
    assert art.set_calls == []


def test_set_art_mode_wakes_stopped_service_with_power_key(tmp_path, monkeypatch):
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, ready=False, mode="off")
    tv.set_art_mode(True)
    # Missing service: don't wait before pressing the key.
    assert art.opened_with == (tv_mod.READY_WAIT_BEFORE_WAKE, 0)
    assert tv.power_presses == 1
    assert art.set_calls == ["on"]


def test_set_art_mode_after_power_key_flipped_it_already(tmp_path, monkeypatch):
    # The press itself switched to Art Mode: nothing more to send.
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, ready=False, mode="on")
    tv.set_art_mode(True)
    assert tv.power_presses == 1
    assert art.set_calls == []


def test_set_art_mode_service_never_starts(tmp_path, monkeypatch):
    tv, art = tv_with_fake_art(
        tmp_path, monkeypatch, ready=False, ready_after_power=False
    )
    with pytest.raises(RuntimeError, match="didn't start"):
        tv.set_art_mode(True)
    assert tv.power_presses == 1
    assert art.set_calls == []
    assert art.closed


def test_set_art_mode_gives_friendly_error_when_service_stops_answering(
    tmp_path, monkeypatch
):
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, ready=True, get_fails=True)
    with pytest.raises(RuntimeError, match="stopped answering"):
        tv.set_art_mode(True)
    assert tv.power_presses == 0


def test_set_art_mode_reconnects_when_tv_drops_off_after_power_key(
    tmp_path, monkeypatch
):
    # After the press the TV is briefly unreachable, as seen on a real Frame.
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, ready=False, mode="off")
    original_open = art.open

    def open_then_unreachable(ready_wait, wait_if_absent=None):
        original_open(ready_wait, wait_if_absent)
        if tv.power_presses and art.opens == 2:
            raise ConnectionResetError("connection dropped")

    art.open = open_then_unreachable
    tv.set_art_mode(True)
    assert tv.power_presses == 1
    assert art.opens == 3
    assert art.set_calls == ["on"]


def test_set_art_mode_unreachable_tv_gives_friendly_error(tmp_path, monkeypatch):
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, unreachable_opens=1)
    with pytest.raises(RuntimeError, match="stopped answering"):
        tv.set_art_mode(True)
    assert tv.power_presses == 0


def test_set_art_mode_waits_longer_right_after_waking(tmp_path, monkeypatch):
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, ready=True, mode="off")
    tv.set_art_mode(True, just_woke=True)
    assert art.opened_with == (tv_mod.READY_WAIT, None)


def test_go_to_art_passes_wake_state(tmp_path, monkeypatch):
    tv = make_tv(tmp_path)
    seen = []
    monkeypatch.setattr(tv, "power_state", lambda timeout=8: "on")
    monkeypatch.setattr(tv, "set_art_mode", lambda on, just_woke: seen.append((on, just_woke)))
    tv.go_to_art()
    tv._on_since = tv_mod.time.monotonic()  # the keeper just saw the TV turn on
    tv.go_to_art()
    assert seen == [(True, False), (True, True)]


# --- host detection ---------------------------------------------------------


def test_leaving_art_mode_with_service_stopped_does_not_press_power(
    tmp_path, monkeypatch
):
    # Pressing power here would switch the TV *to* Art Mode and, over
    # HDMI-CEC, put the Apple TV that just woke back to sleep.
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, ready=False, mode="on")
    tv.set_art_mode(False)
    assert tv.power_presses == 0
    assert art.set_calls == []
    assert art.closed


def test_leaving_art_mode_right_after_waking_does_not_press_power(
    tmp_path, monkeypatch
):
    tv, art = tv_with_fake_art(tmp_path, monkeypatch, ready=False)
    tv.set_art_mode(False, just_woke=True)
    assert tv.power_presses == 0


def test_error_reply_from_tv_gives_friendly_error(tmp_path, monkeypatch):
    from samsungtvws import exceptions

    tv, art = tv_with_fake_art(tmp_path, monkeypatch, mode="off")

    def refuse(wanted):
        raise exceptions.ResponseError("error number -1")

    art.set_artmode_confirmed = refuse
    with pytest.raises(RuntimeError, match="refused"):
        tv.set_art_mode(True)
    assert art.closed


# --- ensure_on (Wake-on-LAN) ------------------------------------------------


def wol_tv(tmp_path, monkeypatch, states, mac="AA:BB:CC:DD:EE:FF"):
    tv = FrameTV(TVConfig(host="1.2.3.4", mac=mac, token_file=str(tmp_path / "t")))
    states = iter(states)
    packets = []
    monkeypatch.setattr(tv, "power_state", lambda timeout=8: next(states, "off"))
    monkeypatch.setattr(tv_mod, "send_magic_packet", packets.append)
    monkeypatch.setattr(tv_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(tv_mod, "WOL_TIMEOUT", 0.05)
    return tv, packets


def test_ensure_on_does_nothing_when_tv_is_on(tmp_path, monkeypatch):
    tv, packets = wol_tv(tmp_path, monkeypatch, ["on"])
    assert tv.ensure_on() is False and packets == []


def test_ensure_on_sends_wake_on_lan_until_tv_is_on(tmp_path, monkeypatch):
    tv, packets = wol_tv(tmp_path, monkeypatch, ["standby", "off", "on"])
    assert tv.ensure_on() is True
    assert packets == ["AA:BB:CC:DD:EE:FF"] * 2


def test_ensure_on_gives_up(tmp_path, monkeypatch):
    tv, packets = wol_tv(tmp_path, monkeypatch, [])
    with pytest.raises(RuntimeError, match="did not wake"):
        tv.ensure_on()
    assert packets


def test_ensure_on_needs_a_mac(tmp_path, monkeypatch):
    tv, packets = wol_tv(tmp_path, monkeypatch, ["off"], mac="")
    with pytest.raises(RuntimeError, match="mac"):
        tv.ensure_on()
