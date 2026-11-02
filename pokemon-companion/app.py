#!/usr/bin/env python3
"""Pokémon Companion: an animated Pokémon that lives on the BUSY Bar.

A Pokémon from the first generation walks left and right across the 72x16
front panel and stops to idle now and then. The sprites are the Pokémon
Mystery Dungeon sprites of the VS Code Pokémon extension, shrunk to 16 px for
the LEDs.

    python3 app.py                        # BUSY Bar over USB (10.0.4.20)
    python3 app.py --host 127.0.0.1:8080  # the emulator, or a Wi-Fi bar
    python3 app.py --pokemon charizard
    python3 app.py --order number         # the chooser counts #001, #002, ...
    python3 app.py --list                 # every Pokémon this build ships
    python3 app.py --once                 # draw one frame and exit

On the bar itself, a click of the dial (OK, or START) opens a chooser. The
wheel steps through the Pokémon. A second click keeps the one shown. BACK,
or a few seconds of silence, brings the old one back.

A double click opens the order screen instead. There the wheel flips between
A to Z and Pokédex number. With Pokédex number, the number goes in front of
each name. A click keeps the order. BACK cancels. --order sets the same
order from the command line.

The app saves the choice and the order in companion-state.json next to this
file. On the next start it uses them, unless --pokemon or --order say
otherwise. The bar streams its buttons only over USB (the emulator does
too), so on Wi-Fi the chooser is absent.

The sprites are PNG frames in sprites/<gen>/<pokemon>/<color>/ next to this
file, with the frame timing in sprites/index.json. The app uploads only the
chosen Pokémon's frames to the bar. Then each tick redraws one image element.

A bar with a password-protected HTTP API takes the password from the
BUSY_HTTP_PASSWORD environment variable (or --token). Localhost needs none.
"""

import argparse
import http.client
import json
import os
import queue
import random
import select
import signal
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from pathlib import Path

APP = "pokemon-companion"
W, H = 72, 16

PRIORITY = 30              # the gallery's convention for ambient apps: above
                           # the built-in apps (10), far below a work session
                           # (90), which owns the whole device
ELEMENT_ID = "pk"
ELEMENT_TIMEOUT = 60       # s the device keeps the frame without a repush
REPUSH_EVERY = 20.0        # s: send an unchanged frame again, so an element
                           # that the firmware expired returns

WALK_SPEED = 6.0           # px/s
IDLE_SECONDS = (1.0, 4.0)  # how long one idle pause lasts
WALK_SECONDS = (2.0, 6.0)  # how long one walk lasts
MAX_DT = 1.0               # s of simulated time per step. The step drops a
                           # longer stall. It does not become a teleport.

FACINGS = ("right", "left")
STATES = ("idle", "walk")

MENU_TIMEOUT = 8.0         # s without a click or a turn before the menu closes
DOUBLE_CLICK = 0.4         # s between two clicks that open the order screen
LABEL_ID = "name"
LABEL_FONT = "tiny"        # 5x6 px: room for the sprite and a name on 72 px
LABEL_GAP = 2              # px between the parked sprite and its name
LABEL_SCROLL = 900         # px/min: a name too long for the panel scrolls
LABEL_SCROLL_DELAY = 800   # ms before the scroll starts, to show the start
SELECT_BUTTONS = ("ok", "start")   # either one opens the menu or keeps the
                                   # choice. The docs do not say which one the
                                   # dial's click reports. START is where a
                                   # thumb lands.
CANCEL_BUTTON = "back"
BUTTONS = {0: "ok", 1: "back", 2: "start"}   # ButtonEvent.button
STATE_FILE = "companion-state.json"
ORDERS = ("name", "number")        # how the menu lists the Pokémon
ORDER_TITLES = {"name": "Order: A to Z", "number": "Order: by number"}
INPUT_BACKOFF_MAX = 30.0   # s between attempts to reconnect the input stream
READ_TIMEOUT = 60.0        # s of silence on the input stream before a ping
                           # checks that the bar is still there

OK_STATUSES = (200, 201, 204)
ANY_STATUS = None          # for check(): any answer but a rejected token is fine
DEVICE_BACKOFF_MIN = 1.0   # s before the first retry after the bar stops answering
DEVICE_BACKOFF_MAX = 15.0  # s: the retry delay doubles up to this value
BUSY_RETRY = 2.0           # s between attempts while another app holds the panel
AUTH_ERROR = ("device rejected the token. Set BUSY_HTTP_PASSWORD "
              "or pass --token.")


# ---------------------------------------------------------------------------
# Sprites
# ---------------------------------------------------------------------------


class SpriteState:
    """One animation (e.g. walk_left): frame file names and their durations."""

    def __init__(self, frames, durations_ms):
        self.frames = frames
        self.durations_ms = durations_ms


class Sprite:
    """Every frame of one Pokémon in one colour, all the same size."""

    def __init__(self, slug, name, color, directory, w, h, states):
        self.slug = slug
        self.name = name
        self.color = color
        self.dir = directory
        self.w = w
        self.h = h
        self.states = states


def load_index(sprites_dir):
    with open(Path(sprites_dir) / "index.json", encoding="utf-8") as f:
        return json.load(f)


def sprite_for(index, sprites_dir, slug, color="default"):
    """Raises KeyError when the Pokémon or colour is not in the index."""
    entry = index[slug]
    info = entry["colors"][color]
    directory = Path(sprites_dir) / f"gen{entry['gen']}" / slug / color
    states = {name: SpriteState(s["frames"], s["durations_ms"]) for name, s in info["states"].items()}
    return Sprite(slug, entry["name"], color, directory, info["w"], info["h"], states)


# ---------------------------------------------------------------------------
# The companion
# ---------------------------------------------------------------------------


class Companion:
    """Where the Pokémon is and what it is doing.

    Every method that carries time takes `now`. So in a test, a minute of
    wandering is a loop over a list of floats, not a minute of waiting.
    """

    def __init__(self, sprite, rng, now):
        self.sprite = sprite
        self.rng = rng
        self.x = float(rng.randint(0, W - sprite.w))
        self.facing = rng.choice(FACINGS)
        self._last = now
        self._begin(rng.choice(STATES), now)

    def _begin(self, state, now):
        self.state = state
        lo, hi = IDLE_SECONDS if state == "idle" else WALK_SECONDS
        self.state_until = now + self.rng.uniform(lo, hi)
        self.frame = 0
        self.frame_at = now

    @property
    def animation(self):
        return self.sprite.states[f"{self.state}_{self.facing}"]

    def park(self, now):
        """Stand still at the left edge, facing right, with no end time. The
        menu shows the Pokémon there, with its name beside it."""
        self.x = 0.0
        self.facing = "right"
        self._begin("idle", now)
        self.state_until = float("inf")

    def hold(self, now):
        """Let wall-clock time pass without simulating any of it. So a pause
        while the display is blocked or the bar is away changes nothing."""
        held = now - self._last
        self.state_until += held
        self.frame_at += held
        self._last = now

    def step(self, now):
        """Advance to `now` and return the element to draw."""
        dt = min(MAX_DT, max(0.0, now - self._last))
        self._last = now

        if now >= self.state_until:
            self._begin("walk" if self.state == "idle" else "idle", now)

        if self.state == "walk":
            self.x += WALK_SPEED * dt * (1 if self.facing == "right" else -1)
            if self.x > W - self.sprite.w:
                self.x = float(W - self.sprite.w)
                self.facing = "left"
            elif self.x < 0:
                self.x = 0.0
                self.facing = "right"

        durations = self.animation.durations_ms
        while now - self.frame_at >= durations[self.frame] / 1000.0:
            self.frame_at += durations[self.frame] / 1000.0
            self.frame = (self.frame + 1) % len(durations)

        return {
            "id": ELEMENT_ID,
            "type": "image",
            "path": self.animation.frames[self.frame],
            "x": int(round(self.x)),
            "y": H - self.sprite.h,
            "timeout": ELEMENT_TIMEOUT,
        }

    def next_deadline(self, now):
        """Seconds until something on screen can change."""
        frame_due = self.frame_at + self.animation.durations_ms[self.frame] / 1000.0
        candidates = [frame_due - now, self.state_until - now]
        if self.state == "walk":
            candidates.append(1.0 / WALK_SPEED)
        return max(0.01, min(candidates))


# ---------------------------------------------------------------------------
# BUSY Bar HTTP API (stdlib only. Docs: http://10.0.4.20/docs)
# ---------------------------------------------------------------------------


class DeviceError(RuntimeError):
    """The bar did not answer at all, as opposed to answering with a status.
    The app retries this forever. A bar on the other end of a flaky USB link
    disconnects and reconnects all the time."""


class Bar:
    def __init__(self, host, token=None):
        self.host = host.replace("http://", "").replace("https://", "").rstrip("/")
        self.base = "http://" + self.host
        self.token = token

    def request(self, method, path, query=None, data=None, content_type=None):
        """Returns the HTTP status. Raises DeviceError when the bar is unreachable."""
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if self.token:
            headers["X-API-Token"] = self.token
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.getcode()
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise DeviceError(str(exc)) from exc

    def clear(self):
        return self.request("DELETE", "/api/display/draw", {"application_name": APP})

    def clear_assets(self):
        return self.request("DELETE", "/api/assets/upload", {"application_name": APP})

    def upload(self, name, data):
        return self.request("POST", "/api/assets/upload", {"application_name": APP, "file": name},
                            data, "application/octet-stream")

    def draw(self, elements, priority):
        body = {"application_name": APP, "priority": priority, "elements": elements}
        return self.request("POST", "/api/display/draw", None,
                            json.dumps(body).encode(), "application/json")


def check(status, what, ok=OK_STATUSES):
    """Anything but success ends the app. There is no fallback for a bar that
    rejects the token or answers something this app does not understand."""
    if status in (401, 403):
        sys.exit(AUTH_ERROR)
    if ok is not ANY_STATUS and status not in ok:
        sys.exit("%s: unexpected HTTP %d from the device" % (what, status))


def install(bar, sprite):
    """Start from a clean slate and upload every frame of this sprite."""
    # Only a rejected token is fatal here. The device answers 404 (or 400 or
    # 503, by its API description) when there is nothing of ours to clear.
    # That already is the clean slate.
    check(bar.clear(), "clear display", ok=ANY_STATUS)
    check(bar.clear_assets(), "clear assets", ok=ANY_STATUS)
    for state in sprite.states.values():
        for name in state.frames:
            with open(sprite.dir / name, "rb") as f:
                check(bar.upload(name, f.read()), "upload " + name)


class Pusher:
    """Hands frames to the bar and derives a retry policy from its answers.

    A frame is a complete snapshot. So a bar that is away or busy gets the
    current frame when it returns, never a queue of old ones. The policy is
    the gallery's (see pixel-runner):

    - announce an outage once
    - raise the retry delay up to 15 s
    - wait while another app owns the panel
    - exit on auth failure
    """

    def __init__(self, bar, sprite, priority):
        self.bar = bar
        self.sprite = sprite
        self.priority = priority
        self.installed = False
        self.last_sent = None        # the last elements the device accepted
        self.last_push = 0.0
        self.retry_at = 0.0          # no push before this, after a gone or busy bar
        self.backoff = DEVICE_BACKOFF_MIN
        self.offline = None          # the outage, announced once, not per retry
        self.blocked = False         # 409: a work session or another app owns it

    def use(self, sprite):
        """Show another Pokémon. Before the next draw, the pusher clears the
        bar and uploads the new frames. The clear also drops any element the
        menu left."""
        self.sprite = sprite
        self.installed = False
        self.last_sent = None

    def step(self, elements, now):
        """One push decision. It takes `now`, so the whole policy is testable
        without a sleep or a bar on the desk. Returns whether `elements` are
        on the bar."""
        if now < self.retry_at:
            return False
        if elements == self.last_sent and now - self.last_push < REPUSH_EVERY:
            return True
        try:
            if not self.installed:
                install(self.bar, self.sprite)
                self.installed = True
            status = self.bar.draw(elements, self.priority)
        except DeviceError as exc:
            # A bar that stopped answering may return rebooted, without our
            # frames. So the next attempt starts again from the upload.
            self.installed = False
            self.retry_at = now + self.backoff
            self.backoff = min(DEVICE_BACKOFF_MAX, self.backoff * 2)
            if self.offline is None:
                # Announced once. Otherwise a bar that is off for an hour would
                # write a line every retry. The log is how someone learns that
                # it returned.
                self.offline = str(exc)
                print("%s: device unreachable (%s). Retrying." % (APP, exc))
            return False

        # Any answer at all means the bar is back, even a busy one.
        if self.offline is not None:
            self.offline = None
            print("%s: device back." % APP)
        self.backoff = DEVICE_BACKOFF_MIN

        if status in OK_STATUSES:
            self.last_sent, self.last_push = elements, now
            if self.blocked:
                self.blocked = False
                print("%s: display back." % APP)
            return True
        if status == 409:
            self.retry_at = now + BUSY_RETRY
            if not self.blocked:
                self.blocked = True
                print("%s: display busy, waiting..." % APP)
            return False
        check(status, "draw")


# ---------------------------------------------------------------------------
# The dial and buttons, streamed as protobuf over /api/status/ws
# ---------------------------------------------------------------------------
#
# busylib's busy.proto, the fields this app reads:
#   State.updates            = 2, repeated StateUpdate
#   StateUpdate.input        = 11, InputEvent
#   InputEvent.button_event  = 1   ButtonEvent{button = 1 (OK 0, BACK 1,
#                                  START 2), action = 2 (PRESS 0, RELEASE 1)}
#   InputEvent.encoder_event = 3   EncoderEvent{delta = 1, sint32}
# proto3 omits zero-valued scalars from the wire, so an OK press is an
# empty ButtonEvent. The decoder is the gallery's (see the counter app).


def _read_varint(buf, pos):
    value, shift = 0, 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint too long")
    raise ValueError("truncated protobuf varint")


def _iter_fields(buf):
    """(field number, wire type, value) for each field of one message."""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield field, wire, value
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            end = pos + length
            if end > len(buf):
                raise ValueError("truncated protobuf message")
            yield field, wire, buf[pos:end]
            pos = end
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if pos + width > len(buf):
                raise ValueError("truncated protobuf message")
            yield field, wire, buf[pos:pos + width]
            pos += width
        else:
            raise ValueError("unknown protobuf wire type %d" % wire)


def _zigzag32(value):
    return (value >> 1) ^ -(value & 1)


def _submessages(buf, wanted):
    return (value for field, wire, value in _iter_fields(buf) if field == wanted and wire == 2)


def input_events(frame):
    """Every ("button", name) press and ("wheel", delta) in one status frame.
    Raises ValueError on a frame that is not a protobuf State."""
    events = []
    for update in _submessages(frame, 2):                # State.updates
        for event in _submessages(update, 11):           # StateUpdate.input
            for field, wire, message in _iter_fields(event):
                if wire != 2:
                    continue
                scalars = {f: v for f, w, v in _iter_fields(message) if w == 0}
                if field == 1 and scalars.get(2, 0) == 0:         # a press
                    events.append(("button", BUTTONS.get(scalars.get(1, 0), "other")))
                elif field == 3:
                    delta = _zigzag32(scalars.get(1, 0))
                    if delta:
                        events.append(("wheel", delta))
    return events


class InputListener(threading.Thread):
    """A minimal RFC 6455 client for /api/status/ws in a daemon thread. It
    hands input events to the main loop through a queue.

    A lost stream is never fatal. The companion keeps walking. The thread
    reports the loss and keeps trying with a backoff. The reason: the firmware
    serves this socket only over USB, and a cable disconnects and reconnects.
    The bar sends nothing while nobody touches it. So the thread answers
    silence with a ping, and only an unanswered ping counts as a lost
    stream."""

    def __init__(self, host, token=None):
        super().__init__(daemon=True)
        self.host = host
        self.token = token
        self.events = queue.Queue()
        self._stop = threading.Event()
        self._complained = False

    def stop(self):
        self._stop.set()

    def wait(self, timeout):
        """Every event that arrives within `timeout` seconds. Blocks only
        until the first one, so a turn of the wheel arrives at once."""
        out = []
        try:
            out.append(self.events.get(timeout=timeout))
            while True:
                out.append(self.events.get_nowait())
        except queue.Empty:
            return out

    def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except OSError as exc:
                if not self._complained:
                    print("%s: no input stream (%s). The dial is off. Retrying "
                          "in the background." % (APP, exc))
                    self._complained = True
                backoff = min(INPUT_BACKOFF_MAX, backoff * 2)
            self._stop.wait(backoff)

    def _session(self):
        host, _, port = self.host.partition(":")
        sock = socket.create_connection((host, int(port or 80)), timeout=8)
        try:
            sock.settimeout(READ_TIMEOUT)
            token = "X-API-Token: %s\r\n" % self.token if self.token else ""
            sock.sendall((
                "GET /api/status/ws HTTP/1.1\r\n"
                "Host: %s\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: %s\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "%s\r\n" % (self.host, b64encode(os.urandom(16)).decode(), token)).encode())
            # Unbuffered, so that select() below sees every byte not yet read.
            stream = sock.makefile("rb", buffering=0)
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                header += self._exactly(stream, 1)
                if len(header) > 8192:
                    raise OSError("handshake answer too long")
            status = header.split(b"\r\n", 1)[0]
            if b" 101 " not in status:
                raise OSError("handshake refused: %s" % status.decode(errors="replace"))
            self._send(sock, 0x1, json.dumps({"enable": True}).encode())
            print("%s: dial connected" % APP)
            self._complained = False

            payload = b""
            kind = 0                 # opcode of the message under assembly
            pinged = False
            while not self._stop.is_set():
                if not select.select([sock], [], [], READ_TIMEOUT)[0]:
                    if pinged:
                        raise OSError("no answer to a ping")
                    self._send(sock, 0x9, b"")
                    pinged = True
                    continue
                fin, opcode, chunk = self._read_frame(stream)
                pinged = False                           # any frame proves life
                if opcode == 0x8:                        # close
                    return
                if opcode == 0x9:                        # ping
                    self._send(sock, 0xA, chunk)
                    continue
                if opcode == 0xA:                        # pong
                    continue
                if opcode:                               # first frame of a message
                    payload, kind = chunk, opcode
                else:                                    # continuation
                    payload += chunk
                if not fin:
                    continue
                if kind == 0x2:
                    try:
                        for event in input_events(payload):
                            # Stamped on arrival. The menu judges a double
                            # click by how far apart the clicks came, not by
                            # when the main loop reads them. The loop may be
                            # busy with the bar.
                            self.events.put(event + (time.monotonic(),))
                    except ValueError:
                        pass                             # a malformed frame costs one event
                payload = b""
        finally:
            sock.close()

    @staticmethod
    def _exactly(stream, n):
        data = b""
        while len(data) < n:
            chunk = stream.read(n - len(data))
            if not chunk:
                raise OSError("stream closed mid-frame")
            data += chunk
        return data

    @classmethod
    def _read_frame(cls, stream):
        head = cls._exactly(stream, 2)
        fin, opcode = head[0] & 0x80, head[0] & 0x0F
        masked, length = head[1] & 0x80, head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", cls._exactly(stream, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", cls._exactly(stream, 8))[0]
        mask = cls._exactly(stream, 4) if masked else b""
        body = cls._exactly(stream, length)
        if mask:
            body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
        return fin, opcode, body

    @staticmethod
    def _send(sock, opcode, data):
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        size = len(data)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        header += mask
        header += bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        sock.sendall(bytes(header))


# ---------------------------------------------------------------------------
# Choosing a Pokémon on the bar
# ---------------------------------------------------------------------------


def load_state(path):
    """What the menu saved last time ("pokemon", "order"), or {} when there
    is nothing usable."""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    return {k: v for k, v in state.items() if k in ("pokemon", "order") and isinstance(v, str)}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def ordered(index, order):
    """The slugs A to Z, or by Pokédex number with a variant right after its
    base form."""
    if order == "number":
        return sorted(index, key=lambda slug: (index[slug]["number"], slug))
    return sorted(index)


def title(index, slug, order):
    """The name as the menu shows it. By number, the number goes in front."""
    name = index[slug]["name"]
    return "#%03d %s" % (index[slug]["number"], name) if order == "number" else name


class Menu:
    """The chooser. A click opens it on the current Pokémon. The wheel steps
    through the list. A click keeps the one shown. BACK or silence brings the
    old one back.

    A double click opens the order screen instead. There the wheel flips
    between A to Z and Pokédex number, with the same keep and cancel. Like
    Companion, the menu takes `now`, so the timing is testable."""

    def __init__(self, index, sprites_dir, slug, state_path, color="default", order="name"):
        self.index = index
        self.sprites_dir = sprites_dir
        self.state_path = state_path
        self.color = color
        self.order = order
        self.slugs = ordered(index, order)
        self.sprite = sprite_for(index, sprites_dir, slug, color)
        self.open_until = None       # None while closed
        self.screen = None           # "choose" or "order" while open
        self.before = None           # (slug, order) from before the menu opened
        self.last_click = None       # when the last click came, for double clicks
        self.save_failed = False     # reported once. The choice still holds this run.

    @property
    def open(self):
        return self.open_until is not None

    def _show(self, slug):
        self.sprite = sprite_for(self.index, self.sprites_dir, slug, self.color)
        return self.sprite

    def _open(self, screen, now):
        """Returns the sprite when it has to be parked, that is, when the menu
        was closed. A move from one screen to the other leaves the sprite
        where it is."""
        parked = self.open
        if not parked:
            self.before = (self.sprite.slug, self.order)
        self.open_until = now + MENU_TIMEOUT
        self.screen = screen
        return None if parked else self.sprite

    def _set_order(self, order):
        self.order = order
        self.slugs = ordered(self.index, order)

    def _close(self, slug, order):
        """Close with the companion on `slug` and the list in `order`. Always
        returns the sprite, because the label has to go."""
        self._set_order(order)
        if slug != self.sprite.slug:
            self._show(slug)
        self.open_until = None
        self.screen = None
        return self.sprite

    def _keep(self):
        """The chooser keeps the Pokémon shown. The order screen keeps the
        order shown and forgets any browsing that preceded it."""
        slug = self.sprite.slug if self.screen == "choose" else self.before[0]
        shown = self._close(slug, self.order)
        self._save()
        return shown

    def _click(self, at, now):
        """A click keeps what is shown, except a quick second click, which
        opens the order screen instead."""
        double = self.last_click is not None and at - self.last_click <= DOUBLE_CLICK
        self.last_click = at
        if double and self.screen != "order":
            return self._open("order", now)
        if not self.open:
            return self._open("choose", now)
        return self._keep()

    def update(self, events, now):
        """Apply the input, one (kind, value, arrival time) triple per event.
        Return the Sprite to show from now on, or None when the sprite on the
        panel stays the same. The loop reads the label again either way."""
        shown = None
        for kind, value, at in events:
            if kind == "button" and value in SELECT_BUTTONS:
                # A click that changes nothing on the panel (e.g. the second
                # of a double click) must not undo what an earlier one did.
                shown = self._click(at, now) or shown
                continue
            if not self.open:
                continue
            self.open_until = now + MENU_TIMEOUT
            if kind == "wheel" and self.screen == "choose":
                i = (self.slugs.index(self.sprite.slug) + value) % len(self.slugs)
                shown = self._show(self.slugs[i])
            elif kind == "wheel":
                self._set_order(ORDERS[(ORDERS.index(self.order) + value) % len(ORDERS)])
            elif value == CANCEL_BUTTON:
                shown = self._close(*self.before)
        if self.open and now >= self.open_until:
            shown = self._close(*self.before)
        return shown

    def _save(self):
        # A read-only app folder (the gallery installs one) is no reason to
        # lose the menu. The menu keeps the choice for this run and reports
        # the failure once.
        try:
            save_state(self.state_path, {"pokemon": self.sprite.slug, "order": self.order})
        except OSError as exc:
            if not self.save_failed:
                print("%s: cannot save the choice (%s). It holds until restart." % (APP, exc))
                self.save_failed = True

    def deadline(self, now):
        """Seconds until the menu times out, or None while it is closed."""
        return None if not self.open else max(0.0, self.open_until - now)

    def label(self):
        """The text beside the parked sprite while the menu is open: the
        Pokémon's name, or the order screen's title."""
        if self.screen == "order":
            text = ORDER_TITLES[self.order]
        else:
            text = title(self.index, self.sprite.slug, self.order)
        return {
            "id": LABEL_ID,
            "type": "text",
            "text": text,
            "font": LABEL_FONT,
            "x": self.sprite.w + LABEL_GAP,
            "y": H // 2,
            "align": "mid_left",
            "width": W - (self.sprite.w + LABEL_GAP),
            "scroll_rate": LABEL_SCROLL,
            "scroll_start_delay": LABEL_SCROLL_DELAY,
            "timeout": ELEMENT_TIMEOUT,
        }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _sigterm(signum, frame):
    # launchd, systemd and a bare `kill` all send SIGTERM. Raising here puts
    # them on the Ctrl-C path, so there is one exit that clears the display.
    raise KeyboardInterrupt


def run(bar, sprite, priority, once=False, menu=None, inputs=None):
    """The loop. `menu` and `inputs` come together or not at all."""
    rng = random.Random()
    companion = Companion(sprite, rng, time.monotonic())
    pusher = Pusher(bar, sprite, priority)
    pending = []                 # input that arrived while the loop slept
    while True:
        now = time.monotonic()
        if now < pusher.retry_at:
            # The bar is away, or another app owns the panel. Freeze the
            # companion instead of walking it where nobody can see it. Drop
            # any clicks, because nobody could see the menu either.
            if inputs:
                inputs.wait(pusher.retry_at - now)
            else:
                time.sleep(pusher.retry_at - now)
            companion.hold(time.monotonic())
            continue

        if menu:
            shown, pending = menu.update(pending, now), []
            if shown is not None:
                companion = Companion(shown, rng, now)
                if menu.open:
                    companion.park(now)
                if shown is not pusher.sprite or not menu.open:
                    # Another Pokémon, or the menu closing (its label goes
                    # only with a clean slate). Opening the menu only parks.
                    pusher.use(shown)
        elements = [companion.step(now)]
        if menu and menu.open:
            elements.append(menu.label())

        if not pusher.step(elements, now):
            if once and pusher.offline is not None:
                sys.exit("%s: device unreachable (%s)" % (APP, pusher.offline))
            continue
        if once:
            return
        delay = companion.next_deadline(now)
        if menu and menu.open:
            delay = min(delay, menu.deadline(now))
        if inputs:
            pending = inputs.wait(delay)     # the dial wakes the loop early
        else:
            time.sleep(delay)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="10.0.4.20", help="bar host[:port] (default: USB)")
    p.add_argument("--pokemon", help="which Pokémon (see --list). Default: the one last "
                   "chosen on the bar, else bulbasaur")
    p.add_argument("--order", choices=ORDERS, help="how the chooser lists them: by name "
                   "(A to Z) or by Pokédex number. Default: as last chosen, else name")
    p.add_argument("--priority", type=int, default=PRIORITY, help="draw priority 1-100")
    p.add_argument("--token", default=os.environ.get("BUSY_HTTP_PASSWORD"),
                   help="API password for a protected bar (default: $BUSY_HTTP_PASSWORD)")
    p.add_argument("--sprites", default=str(Path(__file__).resolve().parent / "sprites"),
                   help=argparse.SUPPRESS)
    p.add_argument("--state", default=str(Path(__file__).resolve().parent / STATE_FILE),
                   help=argparse.SUPPRESS)
    p.add_argument("--once", "--test", dest="once", action="store_true",
                   help="draw one frame and exit (waits while another app owns the display)")
    p.add_argument("--list", action="store_true", help="list available Pokémon and exit")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        index = load_index(args.sprites)
    except OSError as exc:
        print("cannot read the sprites next to app.py: %s" % exc)
        return 2
    if args.list:
        for slug in sorted(index):
            print(slug)
        return 0
    saved = load_state(args.state)
    slug = args.pokemon or saved.get("pokemon") or "bulbasaur"
    if not args.pokemon and slug not in index:
        print("%s: the saved Pokémon %r is not in this build. Using bulbasaur." % (APP, slug))
        slug = "bulbasaur"
    order = args.order or saved.get("order")
    if order not in ORDERS:
        order = "name"
    try:
        sprite = sprite_for(index, args.sprites, slug)
    except KeyError:
        print("unknown Pokémon %r. See --list." % slug)
        return 2
    bar = Bar(args.host, args.token)
    print("%s: %s -> %s  (Ctrl-C to stop)" % (APP, title(index, slug, order), bar.base))
    signal.signal(signal.SIGTERM, _sigterm)
    menu = inputs = None
    if not args.once:
        menu = Menu(index, args.sprites, slug, args.state, order=order)
        sprite = menu.sprite             # one object, so the loop can recognise it
        inputs = InputListener(bar.host, args.token)
        inputs.start()
    try:
        run(bar, sprite, args.priority, args.once, menu, inputs)
    except KeyboardInterrupt:
        try:
            bar.clear()
        except (DeviceError, KeyboardInterrupt):
            pass    # the bar is gone, or a second Ctrl-C. Nothing left to do.
        print("\nstopped.")
    finally:
        if inputs:
            inputs.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
