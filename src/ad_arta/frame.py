"""Fixes to samsungtvws's art channel for a 2020 Frame (protocol level only).

What ad-arta does with the TV lives in tv.py; this module only makes the
art websocket behave: tolerate the Frame's real message order, time out
properly despite pings, and confirm Art Mode changes.
"""

from __future__ import annotations

import logging
import time

import websocket
from samsungtvws import exceptions, helper
from samsungtvws.art import SamsungTVArt
from samsungtvws.connection import SamsungTVWSConnection
from samsungtvws.event import (
    D2D_SERVICE_MESSAGE_EVENT,
    MS_CHANNEL_CLIENT_CONNECT_EVENT,
    MS_CHANNEL_CONNECT_EVENT,
    MS_CHANNEL_READY_EVENT,
)

log = logging.getLogger(__name__)

# Seconds to wait for ms.channel.ready. Right after a restart or wake the art
# service can join the channel several seconds late (7 s seen), and requests
# sent before then are lost. Ready usually arrives at once.
READY_WAIT = 15.0

# samsungtvws sleeps key_press_delay (default 1 s) after *every* command,
# art requests included. We wait for the TV's answer instead.
KEY_PRESS_DELAY = 0.0


def drain(ws) -> bool:
    """Read and discard what's waiting on `ws`, answering pings.

    Return False if the connection is dead.
    """
    if ws is None:
        return False
    old = ws.gettimeout()
    try:
        ws.settimeout(0.01)
        while True:
            try:
                opcode, _ = ws.recv_data_frame(control_frame=True)
            except websocket.WebSocketTimeoutException:
                return True
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                return False
    except (websocket.WebSocketException, OSError):
        return False
    finally:
        try:
            ws.settimeout(old)
        except Exception:
            pass


class FrameArt(SamsungTVArt):
    """SamsungTVArt that tolerates broadcasts before ms.channel.ready.

    The stock open() reads exactly one frame after connecting and fails unless
    it is ms.channel.ready. A real TV may first send e.g.
    ms.channel.clientDisconnect (another client left), which is not an error.
    ms.channel.ready only arrives once the TV's art service is on the channel.
    The Frame stops that service after a while; `ready` says whether it is
    there, `host_present` whether it is on the channel at all.
    """

    ready = False
    host_present = False

    def _websocket_event(self, event, response):
        super()._websocket_event(event, response)
        data = response.get("data") or {}
        if event == MS_CHANNEL_CONNECT_EVENT and isinstance(data, dict):
            # The TV lists everyone on the channel; its art service is the host.
            self.host_present = any(
                c.get("isHost") for c in data.get("clients", []) if isinstance(c, dict)
            )
        elif event == MS_CHANNEL_CLIENT_CONNECT_EVENT and isinstance(data, dict):
            if data.get("isHost"):
                self.host_present = True

    def _recv_frame(self):
        """Receive one frame, giving up after the socket timeout in total.

        websocket-client's recv() answers pings itself and then starts a fresh
        read with the full timeout. The Frame pings every ~6 s, so with an 8 s
        timeout recv() never times out when the art service stays silent.
        Here pings are handled one by one against a single deadline.
        """
        conn = self.connection
        assert conn
        budget = conn.gettimeout()
        deadline = None if budget is None else time.monotonic() + budget
        try:
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise exceptions.ConnectionFailure("Websocket Time out")
                    conn.settimeout(remaining)
                try:
                    opcode, frame = conn.recv_data_frame(control_frame=True)
                except websocket.WebSocketTimeoutException as err:
                    raise exceptions.ConnectionFailure(f"Websocket Time out: {err}") from err
                except (websocket.WebSocketException, OSError) as err:
                    # e.g. the TV drops connections while switching modes
                    raise exceptions.ConnectionFailure(f"Lost connection to the TV: {err}") from err
                if opcode in (websocket.ABNF.OPCODE_PING, websocket.ABNF.OPCODE_PONG):
                    continue  # already answered by websocket-client
                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    raise exceptions.ConnectionFailure("TV closed the connection")
                raw = frame.data
                if opcode == websocket.ABNF.OPCODE_TEXT and isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                break
        finally:
            if self.connection is not None:
                try:
                    self.connection.settimeout(budget)
                except Exception:
                    pass
        response = helper.process_api_response(raw)
        event = response.get("event", "*")
        self._websocket_event(event, response)
        return event, response

    def open(self, ready_wait: float = READY_WAIT, wait_if_absent: float | None = None):
        """Connect and wait up to `ready_wait` for ms.channel.ready.

        If the art service isn't on the channel at all, wait `wait_if_absent`
        instead (defaults to `ready_wait`; 0 returns at once).
        """
        # Skip SamsungTVArt.open (the strict ready check), use the base one.
        self.ready = False
        self.host_present = False
        SamsungTVWSConnection.open(self)
        wait = ready_wait
        if not self.host_present and wait_if_absent is not None:
            wait = wait_if_absent
        self.wait_ready(wait)
        return self.connection

    def wait_ready(self, seconds: float) -> bool:
        """Wait up to `seconds` for ms.channel.ready. Return self.ready."""
        if self.ready:
            return True
        conn = self.connection
        old_timeout = conn.gettimeout()
        deadline = time.monotonic() + seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                conn.settimeout(remaining)
                try:
                    event, frame = self._recv_frame()
                except exceptions.ConnectionFailure:  # websocket timeout
                    break
                if event == MS_CHANNEL_READY_EVENT:
                    self.ready = True
                    return True
                lowered = str(event).lower()
                if "error" in lowered or "unauthorized" in lowered:
                    self.close()
                    raise exceptions.ConnectionFailure(frame)
                # clientConnect/clientDisconnect and others: keep waiting
                log.debug("Ignoring event while waiting for ready: %s", event)
            log.debug("No ms.channel.ready within %.0fs, continuing", seconds)
            return False
        finally:
            if self.connection is not None:
                try:
                    self.connection.settimeout(old_timeout)
                except Exception:  # connection may have been closed above
                    pass

    def set_artmode_confirmed(self, wanted: str) -> None:
        """Set Art Mode and wait until the TV confirms it.

        Turning Art Mode on, the Frame sends no reply carrying our request id,
        only an art_mode_changed broadcast, so the stock set_artmode() waits
        until it times out. Accept either as confirmation.
        """
        req_id = self._new_request_uuid()
        self._send_art_request(
            {"request": "set_artmode_status", "value": wanted},
            wait_for_event=None,
            request_uuid=req_id,
        )
        conn = self.connection
        old_timeout = conn.gettimeout()
        deadline = time.monotonic() + (self.timeout or READY_WAIT)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise exceptions.ConnectionFailure("Websocket Time out")
                conn.settimeout(remaining)
                event, frame = self._recv_frame()
                if event != D2D_SERVICE_MESSAGE_EVENT:
                    continue
                payload = self._decode_d2d_payload(frame) or {}
                sub_event = payload.get("event")
                if sub_event == "error":
                    raise exceptions.ResponseError(
                        "set_artmode_status failed with error number "
                        f"{payload.get('error_code', 'unknown')}"
                    )
                if payload.get("request_id", payload.get("id")) == req_id:
                    return
                if sub_event == "art_mode_changed" and payload.get("status") == wanted:
                    return
        finally:
            if self.connection is not None:
                try:
                    self.connection.settimeout(old_timeout)
                except Exception:
                    pass
