"""Switch the Frame between Art Mode and HDMI, fast and reliably.

Fast path: one art and one remote websocket are kept open (opening one costs
~1.3-1.5 s of TLS), so an action takes ~0.1 s plus the TV's own transition.
Fallback on any problem: fresh connections, Wake-on-LAN if the TV is off, and
a power key press if the Frame has stopped its art service.
"""

from __future__ import annotations

import logging
import threading
import time

import urllib3
import websocket
from samsungtvws import SamsungTVWS, exceptions
from wakeonlan import send_magic_packet

from .config import TVConfig
from .frame import KEY_PRESS_DELAY, READY_WAIT, FrameArt, drain

log = logging.getLogger(__name__)

CLIENT_NAME = "ad-arta"

# The TV uses a self-signed certificate; that is expected, so don't warn.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Art service stopped (the Frame does that after a while on HDMI): how long
# to wait for it before pressing the power key, and after pressing it (it
# rejoined ~7 s after a press). The wait before pressing only applies while
# the service is on the channel but not ready; when it is missing altogether
# the key is pressed at once.
READY_WAIT_BEFORE_WAKE = 8.0
WAKE_WAIT = 20.0
REMOTE_WAKE_TIMEOUT = 20.0
WOL_TIMEOUT = 30.0            # keep sending Wake-on-LAN this long

# Kept-open connections.
KEEP_EVERY = 2.0              # keeper loop: answer pings, reopen dead ones
WARM_REPLY_TIMEOUT = 0.5      # get_artmode on a warm connection (normally ~20 ms)
WARM_READY_WAIT = 2.0         # opening one: don't hold up the keeper for a late art service
REOPEN_BACKOFF = (5.0, 60.0)  # first and max wait after a failed open
# While a connection is down, the keeper checks the TV's power (REST, ~0.1 s)
# and reopens as soon as it turns on. The Frame turns itself off at night and
# its motion sensor turns it back on seconds before the remote is pressed.
POWER_PROBE_TIMEOUT = 1.0
# For this long after the keeper saw the TV turn on, its art service may
# still be joining (7 s seen): wait for it like after a Wake-on-LAN.
JUST_ON = 20.0

NOT_RUNNING = (
    "The TV's Art Mode service isn't running right now (the TV stops it after "
    "a while). art-on and art-off start it again by pressing the power key."
)
DID_NOT_START = (
    "The TV's Art Mode service didn't start, even after pressing the power "
    "key. Restarting the TV (unplug it for a minute) usually fixes this."
)
NO_ART_ANSWER = (
    "The TV's Art Mode service stopped answering. Try again; if it keeps "
    "happening, restart the TV (unplug it for a minute)."
)

_NETWORK_ERRORS = (exceptions.ConnectionFailure, websocket.WebSocketException, OSError)


class _Warm:
    """One kept-open connection. Use `client` only with `lock` held."""

    def __init__(self, name: str, opener) -> None:
        self.name = name
        self._opener = opener  # returns an opened client, or raises
        self.lock = threading.Lock()
        self.client = None
        self._next_try = 0.0
        self._backoff = REOPEN_BACKOFF[0]

    def retry_now(self) -> None:
        """Forget the backoff: the next keep() tries to open right away."""
        self._next_try = 0.0
        self._backoff = REOPEN_BACKOFF[0]

    def drop(self, why: str) -> None:
        """Close the client. Call with self.lock held."""
        if self.client is not None:
            log.debug("Dropping warm %s connection: %s", self.name, why)
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

    def keep(self) -> None:
        """Answer pings on the open connection, or open a new one."""
        with self.lock:
            if self.client is not None:
                if not drain(self.client.connection):
                    self.drop("closed by TV")
                return
        if time.monotonic() < self._next_try:
            return
        try:
            client = self._opener()
        except Exception as err:
            log.debug("Opening warm %s connection failed: %s", self.name, err)
            self._next_try = time.monotonic() + self._backoff
            self._backoff = min(self._backoff * 2, REOPEN_BACKOFF[1])
            return
        self._backoff = REOPEN_BACKOFF[0]
        with self.lock:
            if self.client is None:
                self.client = client
                return
        client.close()  # someone else installed one meanwhile


class FrameTV:
    """Blocking client. Run its methods in a worker thread from async code."""

    def __init__(self, cfg: TVConfig) -> None:
        self.cfg = cfg
        self._warm_art = _Warm("art", self._open_warm_art)
        self._warm_remote = _Warm("remote", self._open_warm_remote)
        self._warm_started = False
        self._probed_power: str | None = None
        self._on_since = float("-inf")  # when the keeper saw the TV turn on

    # --- actions -----------------------------------------------------------

    def go_to_art(self) -> None:
        if self._set_art_mode_warm("on"):
            return
        if self.power_state() != "on":
            # Off is already dark: don't wake the TV just to show art.
            log.info("TV is off, leaving it off")
            return
        self.set_art_mode(True, just_woke=self._just_turned_on())

    def go_to_tv(self, wake: bool = True) -> bool:
        """Leave Art Mode for the Apple TV's input.

        With wake=False a TV that is off is left off (the Apple TV's wake
        may be a false alarm): return False, to be called again with
        wake=True once the wake is confirmed.

        The input key goes first, so the HDMI handshake starts while the
        Frame leaves Art Mode. On a warm connection it takes ~10 ms; a fresh
        one takes ~1.4 s to open, so then it runs alongside instead. A key
        that can't be sent is only logged: Art Mode is turned off regardless.
        """
        if not wake and self._warm_art.client is None and self.power_state() != "on":
            return False
        sender = None
        if self.cfg.input_key and not self._send_key_warm(self.cfg.input_key):
            sender = self._start_key_sender()
        try:
            if not self._set_art_mode_warm("off"):
                woke = self.ensure_on()
                if woke and sender is not None:
                    sender.join()  # that key went to a TV that was off: again
                    sender = self._start_key_sender()
                self.set_art_mode(False, just_woke=woke or self._just_turned_on())
            return True
        finally:
            if sender is not None:
                sender.join()

    def _just_turned_on(self) -> bool:
        return time.monotonic() - self._on_since < JUST_ON

    # --- warm connections ----------------------------------------------------

    def start_warm(self) -> None:
        """Keep art and remote connections open in a background thread."""
        if self._warm_started:
            return
        self._warm_started = True

        def loop() -> None:
            while True:
                try:
                    self._watch_power()
                    # Remote first: it opens fast and carries the input key.
                    for warm in (self._warm_remote, self._warm_art):
                        warm.keep()
                except Exception:  # never let the keeper die
                    log.exception("Warm connection keeper failed")
                    time.sleep(REOPEN_BACKOFF[1])  # a bug: don't log it every 2 s
                time.sleep(KEEP_EVERY)

        threading.Thread(target=loop, name="tv-warm", daemon=True).start()

    def _watch_power(self) -> None:
        """While a warm connection is down, reopen as soon as the TV turns on."""
        warms = (self._warm_art, self._warm_remote)
        if all(w.client is not None for w in warms):
            self._probed_power = "on"
            return
        state = self.power_state(timeout=POWER_PROBE_TIMEOUT)
        if state == "on" and self._probed_power != "on":
            log.debug("TV is on again (was %s), reopening connections", self._probed_power)
            if self._probed_power is not None:  # not at startup: a real turn-on
                self._on_since = time.monotonic()
            for w in warms:
                if w.client is None:
                    w.retry_now()
        self._probed_power = state

    def _open_warm_art(self) -> FrameArt:
        art = self._art()
        try:
            art.open(ready_wait=WARM_READY_WAIT, wait_if_absent=0)
        except Exception:
            art.close()
            raise
        if not art.ready:
            # Art service stopped: the fresh path presses power when needed.
            art.close()
            raise RuntimeError("art service not running")
        return art

    def _open_warm_remote(self) -> SamsungTVWS:
        remote = self._remote()
        try:
            remote.open()
        except Exception:
            remote.close()
            raise
        return remote

    def _set_art_mode_warm(self, wanted: str) -> bool:
        """Set Art Mode on the warm connection. False: use the fresh path."""
        warm = self._warm_art
        with warm.lock:
            art = warm.client
            if art is None:
                return False
            try:
                if not drain(art.connection):
                    raise exceptions.ConnectionFailure("closed by TV")
                art.connection.settimeout(WARM_REPLY_TIMEOUT)
                current = art.get_artmode()
                art.connection.settimeout(art.timeout)
                self._apply(art, current, wanted)
                return True
            except Exception as err:  # noqa: BLE001 - any problem: fresh path
                warm.drop(f"{type(err).__name__}: {err}")
                return False

    def _send_key_warm(self, key: str, **send_args) -> bool:
        """Send a key on the warm connection. False: use a fresh one."""
        warm = self._warm_remote
        with warm.lock:
            if warm.client is None:
                return False
            try:
                if not drain(warm.client.connection):
                    raise exceptions.ConnectionFailure("closed by TV")
                warm.client.send_key(key, **send_args)
                return True
            except Exception as err:  # noqa: BLE001 - any problem: fresh
                warm.drop(f"{type(err).__name__}: {err}")
                return False

    def _start_key_sender(self) -> threading.Thread:
        sender = threading.Thread(target=self._send_input_key_fresh, daemon=True)
        sender.start()
        return sender

    def _send_input_key_fresh(self) -> None:
        """Runs in its own thread, so it logs instead of raising."""
        remote = self._remote()
        try:
            remote.send_key(self.cfg.input_key)
        except Exception as err:  # noqa: BLE001
            log.warning("Could not send %s: %s", self.cfg.input_key, err)
        finally:
            remote.close()

    # --- fresh connections -----------------------------------------------------

    def _remote(self, timeout: float = 8, key_press_delay: float = KEY_PRESS_DELAY) -> SamsungTVWS:
        return SamsungTVWS(
            self.cfg.host,
            port=8002,
            token_file=self.cfg.token_file,
            timeout=timeout,
            key_press_delay=key_press_delay,
            name=CLIENT_NAME,
        )

    def _art(self, timeout: float = 8) -> FrameArt:
        return FrameArt(
            self.cfg.host,
            port=8002,
            token_file=self.cfg.token_file,
            timeout=timeout,
            key_press_delay=KEY_PRESS_DELAY,
            name=CLIENT_NAME,
        )

    def power_state(self, timeout: float = 8) -> str:
        """Return "on", "standby" or "off" (unreachable)."""
        try:
            info = self._remote(timeout=timeout).rest_device_info()
        except Exception:  # network errors, timeouts
            return "off"
        state = str(info.get("device", {}).get("PowerState", "on")).lower()
        return "standby" if state == "standby" else "on"

    def art_mode(self) -> str | None:
        """Return "on"/"off", or None if it can't be read."""
        art = self._art()
        try:
            return art.get_artmode()
        except exceptions.ConnectionFailure:
            log.warning(NOT_RUNNING)
            return None
        finally:
            art.close()

    def ensure_on(self) -> bool:
        """Wake the TV with Wake-on-LAN if it is off or in standby.

        Return True if it had to be woken.
        """
        if self.power_state() == "on":
            return False
        if not self.cfg.mac:
            raise RuntimeError("TV is off and no [tv] mac is set, so it can't be woken up")
        log.info("TV is off, sending Wake-on-LAN")
        deadline = time.monotonic() + WOL_TIMEOUT
        while time.monotonic() < deadline:
            send_magic_packet(self.cfg.mac)
            time.sleep(2)
            if self.power_state() == "on":
                return True  # set_art_mode(just_woke=True) waits for the art service
        raise RuntimeError("TV did not wake up")

    def _press_power(self) -> None:
        # On The Frame a short power press switches between TV and Art Mode
        # (it does not turn the TV off) and restarts a stopped art service.
        # While the art service is stopped the remote channel can take 10 s+
        # to answer, so give a fresh one longer than the usual 8 s. Keep the
        # library's 1 s pause after the key on this rare path.
        if self._send_key_warm("KEY_POWER", key_press_delay=1):
            return
        remote = self._remote(timeout=REMOTE_WAKE_TIMEOUT, key_press_delay=1)
        try:
            remote.send_key("KEY_POWER")
        finally:
            remote.close()

    def _open_art_when_ready(self, seconds: float) -> FrameArt | None:
        """Keep connecting to the art channel until the service is there.

        After a power key press the TV may drop connections and be briefly
        unreachable, so reconnect instead of waiting on one connection.
        """
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            art = self._art()
            try:
                art.open(ready_wait=min(remaining, READY_WAIT_BEFORE_WAKE))
                if art.ready:
                    return art
            except _NETWORK_ERRORS as err:
                log.debug("Art channel not available yet: %s", err)
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            art.close()

    def set_art_mode(self, on: bool, just_woke: bool = False) -> None:
        """Set Art Mode over a fresh connection, waking the art service if needed."""
        wanted = "on" if on else "off"
        art = None
        try:
            if just_woke:
                # Right after turning on, the TV may refuse connections for a
                # moment and the art service joins late: keep reconnecting.
                art = self._open_art_when_ready(READY_WAIT)
            if art is None:
                art = self._art()
                # Missing altogether means the TV stopped it: press at once.
                # (After waking, the wait above already gave it its time.)
                art.open(ready_wait=0 if just_woke else READY_WAIT_BEFORE_WAKE, wait_if_absent=0)
            if not art.ready and not on:
                # Art Mode needs the art service, so with the service stopped
                # the TV isn't showing art. Pressing power here would switch
                # it *to* Art Mode, and the TV would then put the Apple TV to
                # sleep over HDMI-CEC.
                log.info("Art Mode service isn't running, so Art Mode is already off")
                return
            if not art.ready:
                # The power press may flip Art Mode the wrong way; the state is
                # read and set explicitly below, so it ends up right either way.
                log.info("Art Mode service isn't running, pressing power to wake it")
                art.close()
                self._press_power()
                art = self._open_art_when_ready(WAKE_WAIT)
                if art is None:
                    raise RuntimeError(DID_NOT_START)
            self._apply(art, art.get_artmode(), wanted)
        except _NETWORK_ERRORS as err:
            raise RuntimeError(NO_ART_ANSWER) from err
        except exceptions.ResponseError as err:
            raise RuntimeError(f"The TV refused the Art Mode request: {err}") from err
        finally:
            if art is not None:
                art.close()

    @staticmethod
    def _apply(art: FrameArt, current: str, wanted: str) -> None:
        if current == wanted:
            log.info("Art Mode already %s", wanted)
            return
        log.info("Turning Art Mode %s", wanted)
        art.set_artmode_confirmed(wanted)

    # --- pairing -------------------------------------------------------------

    def pair(self) -> None:
        """Open a connection so the TV shows its "Allow" prompt."""
        wait = 60
        try:
            remote = self._remote(timeout=wait)
            remote.open()
            remote.close()
        except (websocket.WebSocketTimeoutException, TimeoutError) as err:
            raise RuntimeError(
                f"No answer on the TV within {wait} seconds. Run `ad-arta pair-tv` "
                "again and choose Allow on the TV (if you already pressed Allow, "
                "the second run usually succeeds right away)."
            ) from err
        except OSError as err:  # includes ConnectionError
            raise RuntimeError(
                f"Couldn't reach the TV at {self.cfg.host}. Check the IP address and "
                "that the TV is on."
            ) from err
