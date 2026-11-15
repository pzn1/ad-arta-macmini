# ad-arta (Mac mini fork)

Put a **2020 Samsung The Frame** into **Art Mode** (instead of off) when the
**Apple TV** sleeps, and back to the Apple TV picture when it wakes.
Local fork of [tranquiloctopus/ad-arta](https://github.com/tranquiloctopus/ad-arta),
trimmed to this one job, running on a Mac mini.

```
Siri Remote power button -> Apple TV sleeps -> Frame: Art Mode          (~1.3 s)
Siri Remote any button   -> Apple TV wakes  -> Frame: Apple TV's HDMI   (~6.5 s to picture)
```

The Apple TV can be on any HDMI port: set it with `tv.input_key` (`KEY_HDMI1`
to `KEY_HDMI4`). The examples here use HDMI 2.

## Required settings

- Frame: **Power On with Mobile: On**. Keep **Anynet+ (HDMI-CEC) On** (eARC
  for the Sonos Arc needs it). DHCP reservation for its IP.
- Apple TV: **Settings > Remotes and Devices > Control TVs and Receivers: Off**,
  so the power button only sleeps the Apple TV.

## Install

```sh
cd ~/ad-arta
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'     # editable: code edits only need a restart
cp config.example.toml config.toml     # set TV IP/MAC, Apple TV host
```

## Pair

```sh
.venv/bin/ad-arta pair-tv              # choose Allow on the TV -> tv-token.txt
.venv/bin/atvremote scan               # find the Apple TV identifier
.venv/bin/atvremote --id <identifier> --protocol companion pair
.venv/bin/atvremote --id <identifier> --protocol airplay pair
```

Credentials go to `~/.pyatv.conf`. `scan` and `-v` logs print credentials:
don't share them.

## Test

```sh
.venv/bin/ad-arta status
.venv/bin/ad-arta art-on
.venv/bin/ad-arta art-off
```

## Service (launchd)

Runs as a LaunchDaemon: starts at boot without anyone logging in, as user
the user who installs it (ad-arta must be in `~/ad-arta`). Install once:

```sh
sudo sh deploy/install-daemon.sh
```

```sh
sudo launchctl kickstart -k system/com.user.ad-arta     # restart
sudo launchctl bootout system/com.user.ad-arta          # stop
sudo launchctl bootstrap system /Library/LaunchDaemons/com.user.ad-arta.plist   # start
sudo launchctl print system/com.user.ad-arta | grep -E "state|pid"   # status
```

Logs: `~/ad-arta/ad-arta.log` (rotated at 1 MB, one old file kept). A crash
at startup goes to `ad-arta.crash.log`.

## Config

| Key | Meaning |
|---|---|
| `tv.host` | TV IP address |
| `tv.mac` | TV MAC address, for Wake-on-LAN |
| `tv.token_file` | Pairing token file (relative to the config file) |
| `tv.input_key` | Key for the Apple TV's HDMI port, sent when leaving Art Mode (e.g. `KEY_HDMI2`). Empty: the Frame returns to its last used input |
| `appletv.identifier` | Apple TV identifier from `atvremote scan` |
| `appletv.host` | Apple TV IP, skips discovery |

## How it works

| File | Job |
|---|---|
| `appletv.py` | Watch the Apple TV's power (pyatv), reconnect when lost |
| `bridge.py` | One worker applies the latest wanted state; TV actions never overlap |
| `tv.py` | Art Mode on/off and the input key: kept-open connections, fallbacks |
| `frame.py` | Fixes to samsungtvws's art channel for the 2020 Frame |
| `config.py`, `cli.py` | `config.toml` and the commands |

Choices, from measurements (Nov 2026):

- **Two Apple TV signals.** MRP (over the AirPlay pairing) reports a wake
  ~0.8 s after the press; Companion only 4-15 s later, but it is the
  trustworthy one and reports sleep at once. Sleep uses Companion; wake acts
  on MRP at once, and is undone only if Companion reports nothing within
  60 s. Undoing a real wake is costly: the Frame entering Art Mode puts the
  Apple TV to sleep over HDMI-CEC.
- **Kept-open connections.** Opening a TLS websocket to the Frame takes
  ~1.4 s, so one art and one remote connection stay open; an action takes
  ~0.1 s. When the Frame turns itself off at night they drop; they reopen as
  soon as it is back on (its motion sensor wakes it before the remote press).
  Any problem falls back to fresh connections.
- **No fixed sleeps.** samsungtvws waits 1 s after every command by default;
  here every step waits for the TV's answer instead.
- **Wake order.** The input key is sent before Art Mode off, so the HDMI handshake
  starts during the Frame's transition. The remaining ~5 s of black (or a
  short "No signal") is the Apple TV waking up.
- **Fallbacks.** TV fully off: Wake-on-LAN, but only once Companion has
  confirmed the wake; on sleep it is left off. A failed action is retried once. Art service stopped (the Frame
  does this after a while on HDMI): a short power key press restarts it.

## Troubleshooting

- **Nothing happens on sleep:** stop the service, run `.venv/bin/ad-arta -v run`,
  check for "Connected to ..." and "Apple TV turned off". If missing, pair
  both protocols again.
- **TV asks to Allow again:** on the TV, Device Connection Manager > Access
  Notification: First Time Only, delete the "Smart Device" entries, delete
  `tv-token.txt` and run `pair-tv` again.
- **"Art Mode service isn't running":** the Frame stops its art service after
  a while on HDMI; ad-arta wakes it with a short power key press.
- **Art Mode turns off after a few minutes at night:** the Frame's own Sleep
  After / motion sensor setting.

## Development

```sh
.venv/bin/pytest
```

Built on [pyatv](https://pyatv.dev) and
[samsungtvws](https://github.com/xchwarze/samsung-tv-ws-api). GPL-3.0.
