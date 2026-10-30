"""Unit tests for the companion logic in pokemon-companion/app.py.

The tests inject time (`now`) and seed the randomness. The sprites come from
the converted set in pokemon-companion/sprites. So these tests run without a
bar.
"""

import io
import random
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import pytest

from conftest import APP_DIR, STRICT_PYTHON

sys.path.insert(0, str(APP_DIR))
import app  # noqa: E402

ID_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
ASSET_RE = re.compile(r"^[a-zA-Z0-9._/-]{1,64}$")


@pytest.fixture(scope="module")
def index():
    return app.load_index(APP_DIR / "sprites")


@pytest.fixture
def sprite(index):
    return app.sprite_for(index, APP_DIR / "sprites", "pikachu")


def make(sprite, seed=1, now=0.0):
    return app.Companion(sprite, random.Random(seed), now)


# --- sprite loading ----------------------------------------------------------


def test_sprite_for_resolves_files_on_disk(sprite):
    assert sprite.w <= app.W and sprite.h <= app.H
    assert set(sprite.states) == {"idle_right", "idle_left", "walk_right", "walk_left"}
    for state in sprite.states.values():
        assert len(state.frames) == len(state.durations_ms) >= 1
        for f in state.frames:
            assert (sprite.dir / f).is_file()


def test_sprite_for_unknown_slug_raises(index):
    with pytest.raises(KeyError):
        app.sprite_for(index, APP_DIR / "sprites", "missingno")


# --- element payload ---------------------------------------------------------


def test_element_is_valid_for_the_firmware(sprite):
    el = make(sprite).step(0.0)
    assert el["type"] == "image"
    assert ID_RE.match(el["id"]) and ASSET_RE.match(el["path"])
    assert el["path"] in {f for s in sprite.states.values() for f in s.frames}
    assert 0 <= el["x"] <= app.W - sprite.w
    assert el["y"] == app.H - sprite.h
    assert el["timeout"] == app.ELEMENT_TIMEOUT


def test_element_path_matches_state_and_facing(sprite):
    c = make(sprite)
    c.state, c.facing, c.frame = "walk", "left", 1
    el = c.step(0.0)
    assert el["path"] == sprite.states["walk_left"].frames[1]


# --- movement ----------------------------------------------------------------


def test_walking_moves_at_walk_speed(sprite):
    c = make(sprite)
    c.state, c.facing, c.x = "walk", "right", 10.0
    c.state_until = 100.0
    c.step(0.0)
    el = c.step(1.0)
    assert el["x"] == 10 + round(app.WALK_SPEED)


def test_idle_does_not_move(sprite):
    c = make(sprite)
    c.state, c.x = "idle", 10.0
    c.state_until = 100.0
    c.step(0.0)
    assert c.step(1.0)["x"] == 10


def test_turns_around_at_the_right_edge(sprite):
    c = make(sprite)
    c.state, c.facing, c.x = "walk", "right", app.W - sprite.w - 1
    c.state_until = 100.0
    c.step(0.0)
    el = c.step(1.0)
    assert el["x"] <= app.W - sprite.w
    assert c.facing == "left"


def test_turns_around_at_the_left_edge(sprite):
    c = make(sprite)
    c.state, c.facing, c.x = "walk", "left", 1.0
    c.state_until = 100.0
    c.step(0.0)
    el = c.step(1.0)
    assert el["x"] >= 0
    assert c.facing == "right"


def test_sprite_always_stays_inside_the_panel(sprite):
    c = make(sprite, seed=7)
    for i in range(1, 2000):
        el = c.step(i * 0.05)
        assert 0 <= el["x"] <= app.W - sprite.w, (i, el)


# --- animation and state timing ----------------------------------------------


def test_frames_advance_on_the_gif_durations(sprite):
    c = make(sprite)
    c.state, c.state_until = "idle", 100.0
    durations = sprite.states["idle_right"].durations_ms
    assert durations == [300, 300]
    assert c.step(0.0)["path"].endswith("_0.png")
    assert c.step(0.29)["path"].endswith("_0.png")
    assert c.step(0.31)["path"].endswith("_1.png")
    assert c.step(0.61)["path"].endswith("_0.png")


def test_state_segments_alternate_and_last_the_configured_ranges(sprite):
    c = make(sprite, seed=3)
    c.step(0.0)
    first, first_until = c.state, c.state_until
    lo, hi = app.IDLE_SECONDS if first == "idle" else app.WALK_SECONDS
    assert lo <= first_until <= hi
    c.step(first_until + 0.01)
    assert c.state != first


def test_next_deadline_is_the_sooner_of_frame_and_pixel(sprite):
    c = make(sprite)
    c.state, c.facing, c.x, c.state_until = "walk", "right", 10.0, 100.0
    c.step(0.0)
    assert 0 < c.next_deadline(0.0) <= min(0.3, 1.0 / app.WALK_SPEED) + 1e-9


def test_hold_freezes_the_companion(sprite):
    c = make(sprite)
    c.state, c.facing, c.x, c.state_until = "walk", "right", 10.0, 100.0
    c.step(0.0)
    c.hold(5.0)
    assert c.step(5.0)["x"] == 10


# --- command line ------------------------------------------------------------


def test_host_defaults_to_usb():
    assert app.parse_args([]).host == "10.0.4.20"


def test_list_prints_available_slugs():
    out = subprocess.run(STRICT_PYTHON + [str(APP_DIR / "app.py"), "--list"],
                         capture_output=True, text=True, check=True)
    assert "pikachu" in out.stdout.split()
    assert out.stderr == ""


# --- push policy ---------------------------------------------------------------


class FakeBar:
    """Scripted answers for the push-policy tests. Unit tests only: the
    end-to-end suite talks to the real emulator."""

    def __init__(self, draws=(), clear=200, clear_assets=200, upload=200, upload_errors=0):
        self.draws = list(draws)
        self.calls = []
        self.clear_status = clear
        self.clear_assets_status = clear_assets
        self.upload_status = upload
        self.upload_errors = upload_errors

    def clear(self):
        self.calls.append("clear")
        return self.clear_status

    def clear_assets(self):
        self.calls.append("clear_assets")
        return self.clear_assets_status

    def upload(self, name, data):
        self.calls.append("upload:" + name)
        if self.upload_errors:
            self.upload_errors -= 1
            raise app.DeviceError("Connection refused")
        return self.upload_status

    def draw(self, elements, priority):
        self.calls.append("draw")
        answer = self.draws.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def el(x):
    return {"id": "pk", "type": "image", "path": "idle_right_0.png", "x": x, "y": 0, "timeout": 60}


def pusher(sprite, **kw):
    bar = FakeBar(**kw)
    return bar, app.Pusher(bar, sprite, app.PRIORITY)


def test_pusher_installs_once_then_draws(sprite):
    bar, p = pusher(sprite, draws=[200, 200])
    assert p.step([el(1)], 0.0) is True
    assert p.step([el(2)], 1.0) is True
    uploads = [c for c in bar.calls if c.startswith("upload:")]
    assert len(uploads) == sum(len(s.frames) for s in sprite.states.values())
    assert bar.calls[:2] == ["clear", "clear_assets"]
    assert bar.calls.count("draw") == 2


def test_pusher_tolerates_nothing_to_clear(sprite):
    bar, p = pusher(sprite, draws=[200], clear=404, clear_assets=503)
    assert p.step([el(1)], 0.0) is True


def test_pusher_exits_when_the_startup_clear_is_rejected(sprite):
    bar, p = pusher(sprite, draws=[200], clear=403)
    with pytest.raises(SystemExit) as exc:
        p.step([el(1)], 0.0)
    assert app.AUTH_ERROR in str(exc.value)


def test_pusher_exits_when_an_upload_fails(sprite):
    bar, p = pusher(sprite, draws=[200], upload=413)
    with pytest.raises(SystemExit) as exc:
        p.step([el(1)], 0.0)
    assert "upload" in str(exc.value) and "413" in str(exc.value)


def test_pusher_reinstalls_after_an_outage(sprite):
    # the bar may have rebooted and lost our frames while it was away
    bar, p = pusher(sprite, draws=[200, app.DeviceError("x"), 200])
    p.step([el(1)], 0.0)
    p.step([el(2)], 1.0)
    assert p.step([el(3)], p.retry_at) is True
    assert bar.calls.count("clear") == 2


def test_test_is_an_alias_for_once():
    assert app.parse_args(["--test"]).once is True


def test_pusher_an_outage_during_install_is_retried(sprite):
    bar, p = pusher(sprite, draws=[200], upload_errors=1)
    assert p.step([el(1)], 0.0) is False
    assert p.retry_at > 0.0
    assert p.step([el(1)], p.retry_at) is True
    assert bar.calls.count("clear") == 2        # installed from scratch again


def test_pusher_does_not_resend_an_unchanged_element_until_repush(sprite):
    bar, p = pusher(sprite, draws=[200, 200])
    assert p.step([el(1)], 0.0) is True
    assert p.step([el(1)], 5.0) is True
    assert bar.calls.count("draw") == 1
    p.step([el(1)], app.REPUSH_EVERY + 0.1)
    assert bar.calls.count("draw") == 2


def test_pusher_announces_an_outage_once_and_backs_off(sprite, capsys):
    bar, p = pusher(sprite, draws=[app.DeviceError("Connection refused")] * 6)
    waits = []
    now = 0.0
    for _ in range(6):
        assert p.step([el(1)], now) is False
        waits.append(p.retry_at - now)
        now = p.retry_at
    assert waits == [1.0, 2.0, 4.0, 8.0, 15.0, 15.0]
    out = capsys.readouterr().out
    assert out.count("unreachable") == 1 and "Connection refused" in out


def test_pusher_holds_while_backing_off(sprite):
    bar, p = pusher(sprite, draws=[app.DeviceError("x"), 200])
    p.step([el(1)], 0.0)
    assert p.step([el(2)], 0.5) is False
    assert bar.calls.count("draw") == 1


def test_pusher_reports_the_device_back_and_resets_backoff(sprite, capsys):
    bar, p = pusher(sprite, draws=[app.DeviceError("x"), app.DeviceError("x"), 200, app.DeviceError("x")])
    p.step([el(1)], 0.0)
    p.step([el(1)], 1.0)
    assert p.step([el(1)], 3.0) is True
    assert "device back." in capsys.readouterr().out
    p.step([el(2)], 4.0)
    assert p.retry_at - 4.0 == app.DEVICE_BACKOFF_MIN


def test_pusher_waits_out_a_busy_display(sprite, capsys):
    bar, p = pusher(sprite, draws=[409, 409, 200])
    assert p.step([el(1)], 0.0) is False
    assert p.retry_at == app.BUSY_RETRY
    assert p.step([el(1)], p.retry_at) is False
    assert p.step([el(1)], 2 * app.BUSY_RETRY) is True
    out = capsys.readouterr().out
    assert out.count("display busy") == 1 and out.count("display back.") == 1


def test_pusher_a_busy_answer_still_means_the_device_is_back(sprite, capsys):
    bar, p = pusher(sprite, draws=[app.DeviceError("x"), 409])
    p.step([el(1)], 0.0)
    p.step([el(1)], 1.0)
    assert "device back." in capsys.readouterr().out
    assert p.offline is None
    assert p.backoff == app.DEVICE_BACKOFF_MIN


def test_pusher_exits_on_auth_failure(sprite):
    bar, p = pusher(sprite, draws=[403])
    with pytest.raises(SystemExit) as exc:
        p.step([el(1)], 0.0)
    assert app.AUTH_ERROR in str(exc.value)


def test_pusher_exits_on_an_unexpected_status(sprite):
    bar, p = pusher(sprite, draws=[500])
    with pytest.raises(SystemExit) as exc:
        p.step([el(1)], 0.0)
    assert "500" in str(exc.value)


def test_unknown_pokemon_exits_with_a_hint():
    out = subprocess.run(STRICT_PYTHON + [str(APP_DIR / "app.py"), "--pokemon", "missingno"],
                         capture_output=True, text=True)
    assert out.returncode == 2
    assert "See --list" in out.stdout and out.stderr == ""


def test_missing_sprites_dir_exits_cleanly(tmp_path):
    out = subprocess.run(STRICT_PYTHON + [str(APP_DIR / "app.py"), "--sprites", str(tmp_path / "nowhere"), "--list"],
                         capture_output=True, text=True)
    assert out.returncode == 2
    assert "nowhere" in out.stdout and out.stderr == ""


# --- input: the protobuf frames from /api/status/ws ---------------------------


def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number, payload):
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _scalar(number, value):
    return _varint(number << 3) + _varint(value)


def _state(input_field, message):
    """State.updates[0].input.<input_field> = message, as the bar sends it."""
    return _field(2, _field(11, _field(input_field, message)))


def button_frame(button, release=False):
    # proto3 omits zero-valued scalars from the wire: OK is 0, PRESS is 0.
    msg = (_scalar(1, button) if button else b"") + (_scalar(2, 1) if release else b"")
    return _state(1, msg)


def wheel_frame(delta):
    zigzag = (delta << 1) ^ (delta >> 31)
    return _state(3, _scalar(1, zigzag & 0xFFFFFFFF))


def test_input_events_decodes_presses_of_every_button():
    assert app.input_events(button_frame(0)) == [("button", "ok")]
    assert app.input_events(button_frame(1)) == [("button", "back")]
    assert app.input_events(button_frame(2)) == [("button", "start")]


def test_input_events_ignores_releases():
    assert app.input_events(button_frame(0, release=True)) == []
    assert app.input_events(button_frame(2, release=True)) == []


def test_input_events_decodes_the_wheel_in_both_directions():
    assert app.input_events(wheel_frame(1)) == [("wheel", 1)]
    assert app.input_events(wheel_frame(-1)) == [("wheel", -1)]
    assert app.input_events(wheel_frame(3)) == [("wheel", 3)]


def test_input_events_ignores_other_status_updates():
    switch = _state(2, _scalar(1, 3))
    battery = _field(2, _field(1, _scalar(1, 80)))
    assert app.input_events(switch) == []
    assert app.input_events(battery) == []
    assert app.input_events(b"") == []


def test_input_events_rejects_a_truncated_frame():
    with pytest.raises(ValueError):
        app.input_events(b"\x12\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff")


# --- the chooser menu --------------------------------------------------------


def menu(index, tmp_path, slug="pikachu"):
    return app.Menu(index, APP_DIR / "sprites", slug, tmp_path / "state.json")


def stamped(events, at):
    """Events as the listener delivers them: with their arrival time."""
    return [e + (at,) for e in events]


def test_menu_starts_closed_on_the_given_pokemon(index, tmp_path):
    m = menu(index, tmp_path)
    assert not m.open and m.screen is None
    assert m.sprite.slug == "pikachu"
    assert m.update(stamped([], 0.0), 0.0) is None


def test_menu_opens_on_a_click_and_labels_the_pokemon(index, tmp_path):
    m = menu(index, tmp_path)
    assert m.update(stamped([("button", "ok")], 0.0), 0.0) is m.sprite
    assert m.open and m.screen == "choose"
    label = m.label()
    assert label["type"] == "text" and label["text"] == "Pikachu"
    assert ID_RE.match(label["id"]) and label["id"] != app.ELEMENT_ID
    assert label["x"] == m.sprite.w + app.LABEL_GAP
    assert label["font"] == app.LABEL_FONT
    # A long name scrolls inside what is left of the panel.
    assert label["width"] == app.W - label["x"]
    assert label["scroll_rate"] > 0


def test_menu_start_also_opens_it(index, tmp_path):
    m = menu(index, tmp_path)
    m.update(stamped([("button", "start")], 0.0), 0.0)
    assert m.open


def test_menu_wheel_steps_through_the_list_and_wraps(index, tmp_path):
    slugs = sorted(index)
    m = menu(index, tmp_path, slugs[-1])
    m.update(stamped([("button", "ok")], 0.0), 0.0)
    assert m.update(stamped([("wheel", 1)], 1.0), 1.0).slug == slugs[0]
    assert m.update(stamped([("wheel", -1)], 2.0), 2.0).slug == slugs[-1]
    assert m.update(stamped([("wheel", -2)], 3.0), 3.0).slug == slugs[-3]
    assert m.label()["text"] == index[slugs[-3]]["name"]


def test_menu_wheel_does_nothing_while_closed(index, tmp_path):
    m = menu(index, tmp_path)
    assert m.update(stamped([("wheel", 1)], 0.0), 0.0) is None
    assert m.sprite.slug == "pikachu"


def test_menu_click_keeps_the_shown_pokemon_and_saves_it(index, tmp_path):
    m = menu(index, tmp_path)
    m.update(stamped([("button", "ok"), ("wheel", 1)], 0.0), 0.0)
    shown = m.sprite
    assert m.update(stamped([("button", "ok")], 1.0), 1.0) is shown
    assert not m.open
    assert app.load_state(tmp_path / "state.json") == {"pokemon": shown.slug, "order": "name"}


def test_menu_back_restores_the_old_pokemon(index, tmp_path):
    m = menu(index, tmp_path)
    m.update(stamped([("button", "ok"), ("wheel", 2)], 0.0), 0.0)
    restored = m.update(stamped([("button", "back")], 1.0), 1.0)
    assert restored.slug == "pikachu" and not m.open
    assert app.load_state(tmp_path / "state.json") == {}


def test_menu_silence_restores_the_old_pokemon(index, tmp_path):
    m = menu(index, tmp_path)
    m.update(stamped([("button", "ok")], 0.0), 0.0)
    m.update(stamped([("wheel", 1)], app.MENU_TIMEOUT - 0.5), app.MENU_TIMEOUT - 0.5)
    assert m.update(stamped([], app.MENU_TIMEOUT + 0.5), app.MENU_TIMEOUT + 0.5) is None       # the wheel kept it open
    assert m.open
    assert m.update(stamped([], app.MENU_TIMEOUT * 2), app.MENU_TIMEOUT * 2).slug == "pikachu"
    assert not m.open


def test_menu_deadline_is_when_it_times_out(index, tmp_path):
    m = menu(index, tmp_path)
    assert m.deadline(5.0) is None
    m.update(stamped([("button", "ok")], 5.0), 5.0)
    assert m.deadline(6.0) == pytest.approx(app.MENU_TIMEOUT - 1.0)


def test_load_state_tolerates_a_missing_or_broken_file(tmp_path):
    assert app.load_state(tmp_path / "nope.json") == {}
    (tmp_path / "bad.json").write_text("{")
    assert app.load_state(tmp_path / "bad.json") == {}
    (tmp_path / "list.json").write_text("[1]")
    assert app.load_state(tmp_path / "list.json") == {}
    (tmp_path / "types.json").write_text('{"pokemon": [1], "order": "number", "extra": 2}')
    assert app.load_state(tmp_path / "types.json") == {"order": "number"}


def test_saved_choice_is_the_default_pokemon(tmp_path):
    state = tmp_path / "state.json"
    app.save_state(state, {"pokemon": "mew"})
    out = subprocess.run(STRICT_PYTHON + [str(APP_DIR / "app.py"), "--host", "127.0.0.1:1",
                                         "--state", str(state), "--once"],
                         capture_output=True, text=True, timeout=30)
    assert "Mew ->" in out.stdout


def test_a_saved_pokemon_that_is_gone_falls_back_with_a_note(tmp_path):
    state = tmp_path / "state.json"
    app.save_state(state, {"pokemon": "missingno"})
    out = subprocess.run(STRICT_PYTHON + [str(APP_DIR / "app.py"), "--host", "127.0.0.1:1",
                                         "--state", str(state), "--once"],
                         capture_output=True, text=True, timeout=30)
    assert "missingno" in out.stdout and "Bulbasaur ->" in out.stdout


def test_a_flag_beats_the_saved_choice(tmp_path):
    state = tmp_path / "state.json"
    app.save_state(state, {"pokemon": "mew"})
    out = subprocess.run(STRICT_PYTHON + [str(APP_DIR / "app.py"), "--host", "127.0.0.1:1",
                                         "--state", str(state), "--pokemon", "ditto", "--once"],
                         capture_output=True, text=True, timeout=30)
    assert "Ditto ->" in out.stdout


def test_ordered_by_pokedex_number_keeps_variants_with_their_number(index):
    by_number = app.ordered(index, "number")
    assert by_number[:4] == ["bulbasaur", "ivysaur", "venusaur", "venusaur_female"]
    assert by_number[-1] == "mew"
    assert app.ordered(index, "name") == sorted(index)
    assert set(by_number) == set(index)


def test_menu_in_pokedex_order_prefixes_the_number(index, tmp_path):
    m = app.Menu(index, APP_DIR / "sprites", "pikachu", tmp_path / "state.json", order="number")
    m.update(stamped([("button", "ok")], 0.0), 0.0)
    assert m.label()["text"] == "#025 Pikachu"
    assert m.update(stamped([("wheel", 1)], 1.0), 1.0).slug == "pikachu_female"
    assert m.label()["text"] == "#025 Pikachu (Female)"
    assert m.update(stamped([("wheel", 1)], 2.0), 2.0).slug == "raichu"
    m.update(stamped([("button", "ok")], 3.0), 3.0)
    assert app.load_state(tmp_path / "state.json") == {"pokemon": "raichu", "order": "number"}


def test_menu_double_click_opens_the_order_screen_and_the_wheel_flips_it(index, tmp_path):
    m = menu(index, tmp_path)
    m.update(stamped([("button", "ok")], 0.0), 0.0)
    assert m.update(stamped([("button", "ok")], app.DOUBLE_CLICK), app.DOUBLE_CLICK) is None     # same sprite stays parked
    assert m.open and m.screen == "order"
    assert m.label()["text"] == "Order: A to Z"
    assert m.update(stamped([("wheel", 1)], 1.0), 1.0) is None
    assert m.label()["text"] == "Order: by number"
    assert m.update(stamped([("wheel", -1)], 1.5), 1.5) is None
    assert m.label()["text"] == "Order: A to Z"
    m.update(stamped([("wheel", 1)], 2.0), 2.0)
    assert m.update(stamped([("button", "start")], 3.0), 3.0) is m.sprite              # keeps it, closes
    assert not m.open
    assert m.order == "number" and m.slugs == app.ordered(index, "number")
    assert m.sprite.slug == "pikachu"
    assert app.load_state(tmp_path / "state.json") == {"pokemon": "pikachu", "order": "number"}


def test_menu_order_screen_back_and_silence_cancel(index, tmp_path):
    m = menu(index, tmp_path)
    m.update(stamped([("button", "ok"), ("button", "ok"), ("wheel", 1)], 0.0), 0.0)
    assert m.update(stamped([("button", "back")], 1.0), 1.0) is m.sprite
    assert not m.open and m.order == "name"
    m.update(stamped([("button", "ok"), ("button", "ok"), ("wheel", 1)], 5.0), 5.0)
    assert m.update(stamped([], 5.0 + app.MENU_TIMEOUT), 5.0 + app.MENU_TIMEOUT) is m.sprite
    assert not m.open and m.order == "name"
    assert app.load_state(tmp_path / "state.json") == {}


def test_menu_double_click_is_timed_by_arrival_not_by_the_loop(index, tmp_path):
    # The loop may be busy pushing frames when the clicks arrive. What
    # counts is how far apart the bar reported them.
    m = menu(index, tmp_path)
    m.update(stamped([("button", "ok")], 0.0), 0.0)
    m.update(stamped([("button", "ok")], 0.2), 1.5)
    assert m.screen == "order"
    m = menu(index, tmp_path)
    assert m.update([("button", "ok", 0.0), ("button", "ok", 0.2)], 1.5) is m.sprite   # parks it
    assert m.screen == "order"
    m = menu(index, tmp_path)
    shown = m.update([("button", "ok", 0.0), ("wheel", 1, 0.1), ("button", "ok", 0.2)], 1.5)
    assert shown is m.sprite and shown.slug == "pikachu_female" and m.screen == "order"


def test_menu_a_click_on_the_order_screen_always_keeps(index, tmp_path):
    m = menu(index, tmp_path)
    m.update([("button", "ok", 0.0), ("button", "ok", 0.3), ("wheel", 1, 0.5)], 0.5)
    assert m.update(stamped([("button", "ok")], 0.6), 0.6) is m.sprite   # a third quick click
    assert not m.open and m.order == "number"


def test_menu_order_screen_from_a_browsed_chooser_drops_the_browsing(index, tmp_path):
    m = menu(index, tmp_path)
    m.update([("button", "ok", 0.0), ("wheel", 1, 0.2), ("button", "ok", 0.3)], 0.3)
    assert m.screen == "order" and m.sprite.slug == "pikachu_female"
    assert m.update(stamped([("button", "back")], 1.0), 1.0).slug == "pikachu"
    assert m.order == "name" and app.load_state(tmp_path / "state.json") == {}
    m.update([("button", "ok", 5.0), ("wheel", 1, 5.2), ("button", "ok", 5.3), ("wheel", 1, 5.5)], 5.5)
    assert m.update(stamped([("button", "ok")], 6.0), 6.0).slug == "pikachu"
    assert app.load_state(tmp_path / "state.json") == {"pokemon": "pikachu", "order": "number"}


def test_menu_two_slow_clicks_open_then_keep(index, tmp_path):
    m = menu(index, tmp_path)
    m.update(stamped([("button", "ok")], 0.0), 0.0)
    m.update(stamped([("button", "ok")], app.DOUBLE_CLICK + 0.1), app.DOUBLE_CLICK + 0.1)
    assert not m.open and m.screen is None
    assert app.load_state(tmp_path / "state.json") == {"pokemon": "pikachu", "order": "name"}


def test_menu_order_screen_opens_from_the_chooser_too(index, tmp_path):
    m = menu(index, tmp_path)
    m.update(stamped([("button", "ok")], 0.0), 0.0)
    m.update(stamped([("wheel", 1)], 1.0), 1.0)
    m.update(stamped([("button", "ok")], 2.0), 2.0)                 # keeps pikachu_female, closes
    assert m.update(stamped([("button", "ok")], 2.2), 2.2) is m.sprite      # ...a quick second click parks it again
    assert m.screen == "order" and m.sprite.slug == "pikachu_female"


def test_menu_keeps_the_colour_while_browsing(index, tmp_path):
    m = app.Menu(index, APP_DIR / "sprites", "pikachu", tmp_path / "state.json", color="shiny")
    assert m.sprite.color == "shiny"
    m.update(stamped([("button", "ok"), ("wheel", 1)], 0.0), 0.0)
    assert m.sprite.color == "shiny"


def test_shiny_flag_picks_the_shiny_frames(tmp_path):
    out = subprocess.run(STRICT_PYTHON + [str(APP_DIR / "app.py"), "--host", "127.0.0.1:1",
                                         "--state", str(tmp_path / "s.json"), "--pokemon", "ditto",
                                         "--shiny", "--once"],
                         capture_output=True, text=True, timeout=30)
    assert "Ditto (shiny) ->" in out.stdout


def test_saved_order_is_the_default_and_the_flag_wins(tmp_path):
    state = tmp_path / "state.json"
    app.save_state(state, {"pokemon": "mew", "order": "number"})
    out = subprocess.run(STRICT_PYTHON + [str(APP_DIR / "app.py"), "--host", "127.0.0.1:1",
                                         "--state", str(state), "--once"],
                         capture_output=True, text=True, timeout=30)
    assert "#151 Mew ->" in out.stdout
    out = subprocess.run(STRICT_PYTHON + [str(APP_DIR / "app.py"), "--host", "127.0.0.1:1",
                                         "--state", str(state), "--order", "name", "--once"],
                         capture_output=True, text=True, timeout=30)
    assert ": Mew ->" in out.stdout


# --- parking the companion for the menu --------------------------------------


def test_parked_companion_idles_at_the_left_edge_forever(sprite):
    c = make(sprite)
    c.park(0.0)
    for now in (0.0, 5.0, 60.0, 600.0):
        element = c.step(now)
        assert element["x"] == 0
        assert element["path"].startswith("idle_right_")
    assert c.next_deadline(600.0) <= 0.3     # the idle frames still animate


# --- swapping the sprite on the bar ------------------------------------------


def test_pusher_use_reinstalls_the_new_sprite(sprite, index):
    bar = FakeBar(draws=[200, 200])
    p = app.Pusher(bar, sprite, 30)
    p.step([el(0)], 0.0)
    other = app.sprite_for(index, APP_DIR / "sprites", "ditto")
    p.use(other)
    assert p.step([el(0)], 1.0)
    assert p.sprite is other
    uploads = [c for c in bar.calls if c.startswith("upload:")]
    assert len(uploads) == sum(len(s.frames) for s in sprite.states.values()) + \
        sum(len(s.frames) for s in other.states.values())
    assert bar.calls.count("clear") == 2


# --- the listener's websocket client -----------------------------------------


class WsServer(threading.Thread):
    """A one-client RFC 6455 server on a loopback port. The tests use it as
    the bar's input stream. It can stay silent, answer pings or not, and push
    frames."""

    def __init__(self, pong=True):
        super().__init__(daemon=True)
        self.pong = pong
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(5)
        self.host = "127.0.0.1:%d" % self.sock.getsockname()[1]
        self.accepted = 0
        self.pings = 0
        self.conn = None
        self.lock = threading.Lock()

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self._serve(conn)

    def _serve(self, conn):
        try:
            stream = conn.makefile("rb")
            while stream.readline() not in (b"\r\n", b"\n", b""):
                pass
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: x\r\n\r\n")
            # Accepted only once the handshake answer is on the wire, so
            # anything a test sends next follows it.
            with self.lock:
                self.accepted += 1
                self.conn = conn
            while True:
                fin, opcode, body = app.InputListener._read_frame(stream)
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    with self.lock:
                        self.pings += 1
                    if self.pong:
                        self.send(0xA, body)
        except OSError:
            pass
        finally:
            conn.close()

    def send(self, opcode, payload, fin=True):
        with self.lock:
            self.conn.sendall(bytes([(0x80 if fin else 0) | opcode, len(payload)]) + payload)

    def close(self):
        self.sock.close()


def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


@pytest.fixture
def quick_timeout(monkeypatch):
    monkeypatch.setattr(app, "READ_TIMEOUT", 0.2)


def listener(server):
    lis = app.InputListener(server.host)
    lis.start()
    return lis


def printed(capsys):
    """Everything printed so far, kept across calls."""
    seen = []

    def read(text):
        seen.append(capsys.readouterr().out)
        return "".join(seen).count(text)
    return read


def test_listener_delivers_input_frames(quick_timeout, capsys):
    server = WsServer()
    server.start()
    lis = listener(server)
    try:
        _until(lambda: server.accepted == 1)
        server.send(0x2, wheel_frame(1))
        server.send(0x2, button_frame(0))
        events = lis.wait(2.0)
        while len(events) < 2:
            events += lis.wait(2.0)
        assert [e[:2] for e in events] == [("wheel", 1), ("button", "ok")]
        assert all(0 < time.monotonic() - e[2] < 5 for e in events)
        assert "dial connected" in capsys.readouterr().out
    finally:
        lis.stop()
        server.close()


def test_listener_keeps_a_quiet_stream_alive_with_pings(quick_timeout, capsys):
    server = WsServer(pong=True)
    server.start()
    lis = listener(server)
    try:
        _until(lambda: server.pings >= 3)
        assert server.accepted == 1
        assert lis.wait(0) == []
        assert "no input stream" not in capsys.readouterr().out
    finally:
        lis.stop()
        server.close()


def test_listener_reconnects_when_pings_go_unanswered(quick_timeout, capsys):
    server = WsServer(pong=False)
    server.start()
    lis = listener(server)
    try:
        count = printed(capsys)
        _until(lambda: count("dial connected") == 2)
        assert count("no input stream") == 1
    finally:
        lis.stop()
        server.close()


def test_listener_takes_any_frame_as_an_answer_to_its_ping(quick_timeout, capsys):
    server = WsServer(pong=False)
    server.start()
    lis = listener(server)
    try:
        _until(lambda: server.pings == 1)
        server.send(0x2, wheel_frame(1))
        _until(lambda: server.pings == 2)
        assert server.accepted == 1
        assert [e[:2] for e in lis.wait(1.0)] == [("wheel", 1)]
        assert "no input stream" not in capsys.readouterr().out
    finally:
        lis.stop()
        server.close()


def test_listener_reassembles_a_fragmented_message(quick_timeout):
    server = WsServer()
    server.start()
    lis = listener(server)
    try:
        _until(lambda: server.accepted == 1)
        frame = wheel_frame(-1)
        server.send(0x2, frame[:3], fin=False)
        server.send(0x0, frame[3:])
        assert [e[:2] for e in lis.wait(2.0)] == [("wheel", -1)]
    finally:
        lis.stop()
        server.close()


def test_read_frame_unmasks_and_reads_extended_lengths():
    payload = bytes(range(256)) * 2
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    frame = b"\x82\xfe" + struct.pack(">H", len(payload)) + mask + masked
    assert app.InputListener._read_frame(io.BytesIO(frame)) == (0x80, 0x2, payload)


def test_read_frame_rejects_a_truncated_extended_header():
    with pytest.raises(OSError):
        app.InputListener._read_frame(io.BytesIO(b"\x82\x7e\x00"))
    with pytest.raises(OSError):
        app.InputListener._read_frame(io.BytesIO(b"\x82\x7f\x00\x00"))


def test_menu_keeps_the_choice_when_it_cannot_be_saved(index, tmp_path, capsys):
    m = app.Menu(index, APP_DIR / "sprites", "pikachu", tmp_path / "missing" / "state.json")
    m.update(stamped([("button", "ok"), ("wheel", 1)], 0.0), 0.0)
    shown = m.sprite
    assert m.update(stamped([("button", "ok")], 1.0), 1.0) is shown
    assert not m.open
    m.update(stamped([("button", "ok"), ("button", "ok")], 2.0), 2.0)
    assert capsys.readouterr().out.count("cannot save") == 1
