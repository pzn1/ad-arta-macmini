import pytest
import websocket

from ad_arta import frame as tv_mod


class FakeConn:
    def __init__(self):
        self.timeouts = []
        self.closed = False

    def gettimeout(self):
        return 8

    def settimeout(self, t):
        self.timeouts.append(t)

    def close(self):
        self.closed = True


def make_art(monkeypatch, tmp_path, frames):
    """FrameArt whose base open() is faked and whose frames are scripted."""
    from samsungtvws import exceptions
    from samsungtvws.connection import SamsungTVWSConnection

    conn = FakeConn()

    def fake_open(self):
        self.connection = conn
        return conn

    monkeypatch.setattr(SamsungTVWSConnection, "open", fake_open)
    art = tv_mod.FrameArt("1.2.3.4", port=8002, token_file=str(tmp_path / "t"))
    seq = iter(frames)

    def fake_recv():
        item = next(seq)
        if item == "timeout":
            raise exceptions.ConnectionFailure("Websocket Time out")
        return item, {"event": item}

    monkeypatch.setattr(art, "_recv_frame", fake_recv)
    return art, conn


def test_frameart_ignores_client_disconnect(monkeypatch, tmp_path):
    art, conn = make_art(
        monkeypatch,
        tmp_path,
        ["ms.channel.clientDisconnect", "ms.channel.ready"],
    )
    assert art.open() is conn
    assert conn.timeouts[-1] == 8  # restored


def test_frameart_tolerates_missing_ready(monkeypatch, tmp_path):
    art, conn = make_art(monkeypatch, tmp_path, ["timeout"])
    assert art.open() is conn
    assert not conn.closed


def test_frameart_raises_on_error_event(monkeypatch, tmp_path):
    from samsungtvws import exceptions

    art, conn = make_art(monkeypatch, tmp_path, ["ms.error"])
    with pytest.raises(exceptions.ConnectionFailure):
        art.open()
    assert conn.closed


# --- FrameArt._recv_frame --------------------------------------------------


class Frame:
    def __init__(self, data):
        self.data = data


class PingingConn:
    """Answers every read with a ping, like a TV whose art service is silent."""

    def __init__(self, frames=None):
        self.timeout = 0.3
        self.frames = list(frames or [])

    def gettimeout(self):
        return self.timeout

    def settimeout(self, t):
        self.timeout = t

    def recv_data_frame(self, control_frame=False):
        import time

        if self.frames:
            return self.frames.pop(0)
        time.sleep(0.05)
        return websocket.ABNF.OPCODE_PING, Frame(b"\x00")


def art_with_conn(tmp_path, conn):
    art = tv_mod.FrameArt("1.2.3.4", port=8002, token_file=str(tmp_path / "t"))
    art.connection = conn
    return art


def test_recv_frame_times_out_despite_pings(tmp_path):
    import time

    from samsungtvws import exceptions

    conn = PingingConn()
    art = art_with_conn(tmp_path, conn)
    start = time.monotonic()
    with pytest.raises(exceptions.ConnectionFailure):
        art._recv_frame()
    assert time.monotonic() - start < 1
    assert conn.timeout == 0.3  # restored


def test_recv_frame_skips_pings_and_returns_event(tmp_path):
    frames = [
        (websocket.ABNF.OPCODE_PING, Frame(b"\x00")),
        (websocket.ABNF.OPCODE_TEXT, Frame(b'{"event": "d2d_service_message"}')),
    ]
    art = art_with_conn(tmp_path, PingingConn(frames))
    event, frame = art._recv_frame()
    assert event == "d2d_service_message"


def d2d(payload):
    import json

    return "d2d_service_message", {
        "event": "d2d_service_message",
        "data": json.dumps(payload),
    }


def scripted_art(tmp_path, monkeypatch, frames):
    """FrameArt on a fake connection that returns `frames`, then times out."""
    from samsungtvws import exceptions

    art = tv_mod.FrameArt(
        "1.2.3.4", port=8002, token_file=str(tmp_path / "t"), timeout=8
    )
    art.connection = FakeConn()
    sent = []
    seq = iter(frames)

    def fake_send(request_data, wait_for_event=None, request_uuid=None, **kw):
        sent.append((request_data, wait_for_event, request_uuid))

    def fake_recv():
        try:
            item = next(seq)
        except StopIteration:
            raise exceptions.ConnectionFailure("Websocket Time out")
        return item(sent[-1][2]) if callable(item) else item

    monkeypatch.setattr(art, "_send_art_request", fake_send)
    monkeypatch.setattr(art, "_recv_frame", fake_recv)
    return art, sent


def test_set_artmode_accepts_art_mode_changed_broadcast(tmp_path, monkeypatch):
    # What a 2020 Frame sends when turning Art Mode on: no request id.
    art, sent = scripted_art(
        tmp_path,
        monkeypatch,
        [d2d({"event": "art_mode_changed", "status": "on"})],
    )
    art.set_artmode_confirmed("on")
    request, wait_for_event, _ = sent[0]
    assert request == {"request": "set_artmode_status", "value": "on"}
    assert wait_for_event is None


def test_set_artmode_accepts_reply_with_request_id(tmp_path, monkeypatch):
    art, _ = scripted_art(
        tmp_path,
        monkeypatch,
        [lambda rid: d2d({"event": "set_artmode_status", "id": rid})],
    )
    art.set_artmode_confirmed("off")


def test_set_artmode_ignores_unrelated_messages(tmp_path, monkeypatch):
    from samsungtvws import exceptions

    art, _ = scripted_art(
        tmp_path,
        monkeypatch,
        [
            ("ms.channel.clientConnect", {"event": "ms.channel.clientConnect"}),
            d2d({"event": "image_selected", "content_id": "MY_F0025"}),
            d2d({"event": "art_mode_changed", "status": "off"}),
            d2d({"event": "set_artmode_status", "id": "someone-else"}),
        ],
    )
    with pytest.raises(exceptions.ConnectionFailure):
        art.set_artmode_confirmed("on")
    assert art.connection.timeouts[-1] == 8  # restored


def test_set_artmode_raises_on_error_reply(tmp_path, monkeypatch):
    from samsungtvws import exceptions

    art, _ = scripted_art(
        tmp_path, monkeypatch, [d2d({"event": "error", "error_code": "-1"})]
    )
    with pytest.raises(exceptions.ResponseError):
        art.set_artmode_confirmed("on")


def test_open_sets_ready_flag(monkeypatch, tmp_path):
    art, _ = make_art(monkeypatch, tmp_path, ["ms.channel.ready"])
    art.open()
    assert art.ready


def test_open_without_ready_then_wait_ready(monkeypatch, tmp_path):
    art, _ = make_art(
        monkeypatch,
        tmp_path,
        ["timeout", "ms.channel.clientConnect", "ms.channel.ready"],
    )
    art.open()
    assert not art.ready
    assert art.wait_ready(5)
    assert art.ready


# --- FrameTV.set_art_mode ----------------------------------------------------


def test_recv_frame_turns_dropped_connection_into_connection_failure(tmp_path):
    from samsungtvws import exceptions

    class DroppedConn(PingingConn):
        def recv_data_frame(self, control_frame=False):
            raise websocket.WebSocketConnectionClosedException("closed")

    art = art_with_conn(tmp_path, DroppedConn())
    with pytest.raises(exceptions.ConnectionFailure, match="Lost connection"):
        art._recv_frame()


def connect_event(*clients):
    return {"event": "ms.channel.connect", "data": {"clients": list(clients), "id": "x"}}


def test_host_present_from_connect_list(tmp_path):
    art = tv_mod.FrameArt("1.2.3.4", port=8002, token_file=str(tmp_path / "t"))
    art._websocket_event(
        "ms.channel.connect",
        connect_event({"isHost": False}, {"isHost": True, "deviceName": "Smart Device"}),
    )
    assert art.host_present


def test_host_absent_from_connect_list(tmp_path):
    art = tv_mod.FrameArt("1.2.3.4", port=8002, token_file=str(tmp_path / "t"))
    art._websocket_event("ms.channel.connect", connect_event({"isHost": False}))
    assert not art.host_present
    art._websocket_event(
        "ms.channel.clientConnect",
        {"event": "ms.channel.clientConnect", "data": {"isHost": True}},
    )
    assert art.host_present


def test_open_returns_at_once_when_service_absent(monkeypatch, tmp_path):
    from samsungtvws.connection import SamsungTVWSConnection

    conn = FakeConn()

    def fake_open(self):
        self.connection = conn
        self._websocket_event("ms.channel.connect", connect_event({"isHost": False}))
        return conn

    monkeypatch.setattr(SamsungTVWSConnection, "open", fake_open)
    art = tv_mod.FrameArt("1.2.3.4", port=8002, token_file=str(tmp_path / "t"))

    def no_recv():
        raise AssertionError("should not wait for frames")

    monkeypatch.setattr(art, "_recv_frame", no_recv)
    art.open(ready_wait=8, wait_if_absent=0)
    assert not art.ready
    assert not art.host_present
