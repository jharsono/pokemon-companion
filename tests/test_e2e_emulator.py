"""End-to-end: the real app against the real busybar-emulator, no mocks.

Needs a built emulator checkout: BUSYBAR_EMULATOR_DIR, or the sibling
../busybar-emulator (with web/dist built). Without one, these tests skip.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import APP_DIR, ROOT, STRICT_PYTHON

sys.path.insert(0, str(APP_DIR))
import app  # noqa: E402

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _emulator_dir():
    env = os.environ.get("BUSYBAR_EMULATOR_DIR")
    for c in ([env] if env else []) + [ROOT.parent / "busybar-emulator"]:
        if c and (Path(c) / "server.js").is_file() and (Path(c) / "web" / "dist" / "index.html").is_file():
            return Path(c)
    pytest.skip("no built busybar-emulator: set BUSYBAR_EMULATOR_DIR")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lan_ip():
    """An address of this machine that is not loopback. The emulator treats
    it as a remote caller, and so asks for the API token."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))    # connect() sends no packet
            ip = s.getsockname()[0]
        except OSError:
            ip = "127.0.0.1"
    if ip.startswith("127."):
        pytest.skip("no non-loopback address for the auth test")
    return ip


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _post_json(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.getcode()


def _wait_until(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    pytest.fail("timed out waiting for " + what)


class Emulator:
    def __init__(self, port, proc, log_path):
        self.port = port
        self.proc = proc
        self.log_path = log_path
        self.host = "127.0.0.1:%d" % port
        self.base = "http://" + self.host

    def snapshot(self):
        """The first SSE event on /events is the full device state."""
        with urllib.request.urlopen(self.base + "/events", timeout=5) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    return json.loads(line[len("data:"):])
        pytest.fail("no state event from the emulator")

    def steal(self, priority, duration_ms):
        return _post_json(self.base + "/api/_scenario/steal",
                          {"priority": priority, "duration_ms": duration_ms})

    def offline(self, duration_ms):
        return _post_json(self.base + "/api/_scenario/offline", {"duration_ms": duration_ms})

    def log(self):
        return self.log_path.read_text()


def _start_emulator(emu_dir, log_dir, extra_env=None):
    port = _free_port()
    log_path = log_dir / ("emulator-%d.log" % port)
    log = open(log_path, "wb")
    proc = subprocess.Popen(["node", "server.js"], cwd=emu_dir, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "PORT": str(port), **(extra_env or {})})
    emu = Emulator(port, proc, log_path)

    def up():
        try:
            return b"api_semver" in _get(emu.base + "/api/version", timeout=1)
        except (urllib.error.URLError, OSError):
            return False
    try:
        _wait_until(up, 15, "the emulator to start")
    except BaseException:
        proc.terminate()
        log.close()
        raise
    return emu, log


@pytest.fixture(scope="module")
def emulator(tmp_path_factory):
    emu, log = _start_emulator(_emulator_dir(), tmp_path_factory.mktemp("emu"))
    try:
        yield emu
    finally:
        emu.proc.terminate()
        emu.proc.wait(timeout=10)
        log.close()


@pytest.fixture(scope="module")
def protected_emulator(tmp_path_factory):
    emu, log = _start_emulator(_emulator_dir(), tmp_path_factory.mktemp("emu"), {"BUSY_API_TOKEN": "hunter2"})
    try:
        yield emu
    finally:
        emu.proc.terminate()
        emu.proc.wait(timeout=10)
        log.close()


class AppProcess:
    """The app as a subprocess."""

    def __init__(self, host, *extra, env=None):
        self.proc = subprocess.Popen(
            STRICT_PYTHON + ["-u", str(APP_DIR / "app.py"), "--host", host, *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def stop(self, sig=signal.SIGINT, timeout=10):
        """Signal it and report (exit code, stdout, stderr)."""
        self.proc.send_signal(sig)
        return self.finish(timeout)

    def finish(self, timeout=10):
        """Wait for it to exit on its own and report (exit code, stdout, stderr)."""
        out, err = self.proc.communicate(timeout=timeout)
        return self.proc.returncode, out, err


def _our_elements(snapshot):
    frame = snapshot["frame"]
    if frame.get("application_name") != app.APP:
        return []
    return frame.get("elements", [])


def _sprite(slug):
    return app.sprite_for(app.load_index(APP_DIR / "sprites"), APP_DIR / "sprites", slug)


def _drawn(emulator):
    return _our_elements(emulator.snapshot())


def test_companion_is_drawn_from_uploaded_frames_and_cleared_on_exit(emulator):
    sprite = _sprite("pikachu")
    companion = AppProcess(emulator.host, "--pokemon", "pikachu")
    try:
        _wait_until(lambda: _drawn(emulator), 10, "the companion to be drawn")
        elements = _drawn(emulator)
        assert len(elements) == 1
        el = elements[0]
        assert el["id"] == app.ELEMENT_ID and el["type"] == "image"
        frames = {f for s in sprite.states.values() for f in s.frames}
        assert Path(el["path"]).name in frames
        assert 0 <= el["x"] <= app.W - sprite.w
        assert el["y"] == app.H - sprite.h
        png = _get(emulator.base + "/assets/" + el["path"])
        assert png.startswith(PNG_SIGNATURE)
        # It keeps animating: the frame or the position changes within a second.
        _wait_until(lambda: _drawn(emulator)[0] != el, 5, "the companion to move")
    finally:
        code, out, err = companion.stop()
    assert code == 0, (out, err)
    assert err == ""
    assert "stopped." in out
    assert _drawn(emulator) == []


def test_sigterm_also_clears_the_display(emulator):
    companion = AppProcess(emulator.host, "--pokemon", "squirtle")
    try:
        _wait_until(lambda: _drawn(emulator), 10, "the companion to be drawn")
    finally:
        code, out, err = companion.stop(signal.SIGTERM)
    assert (code, err) == (0, "")
    assert "stopped." in out
    assert _drawn(emulator) == []


def test_once_draws_one_frame_and_exits(emulator):
    code, out, err = AppProcess(emulator.host, "--pokemon", "bulbasaur", "--once").finish()
    assert (code, err) == (0, "")
    assert len(_drawn(emulator)) == 1


def test_waits_while_another_app_owns_the_display(emulator):
    assert emulator.steal(99, 2500) == 200
    companion = AppProcess(emulator.host, "--pokemon", "charmander")
    try:
        time.sleep(1.5)
        assert _drawn(emulator) == []
        _wait_until(lambda: _drawn(emulator), 10, "the companion after the steal ends")
    finally:
        code, out, err = companion.stop()
    assert (code, err) == (0, "")
    assert "display busy" in out
    assert "display back." in out


def test_survives_the_bar_going_away_and_coming_back(emulator):
    companion = AppProcess(emulator.host, "--pokemon", "eevee")
    try:
        _wait_until(lambda: _drawn(emulator), 10, "the companion to be drawn")
        assert emulator.offline(2500) == 200
        time.sleep(3.0)
        before = _drawn(emulator)
        _wait_until(lambda: _drawn(emulator) != before, 20, "the companion to move again after the outage")
    finally:
        code, out, err = companion.stop()
    assert (code, err) == (0, "")
    assert out.count("device unreachable") == 1
    assert "device back." in out


def test_once_against_an_unreachable_bar_fails_loudly():
    code, out, err = AppProcess("127.0.0.1:1", "--once").finish(timeout=30)
    assert code == 1
    assert "unreachable" in err
    assert out.count("device unreachable") == 1


def test_wrong_token_fails_loudly_and_right_token_works(protected_emulator):
    host = "%s:%d" % (_lan_ip(), protected_emulator.port)
    env = {k: v for k, v in os.environ.items() if k != "BUSY_HTTP_PASSWORD"}
    code, out, err = AppProcess(host, "--once", env=env).finish()
    assert code == 1
    assert app.AUTH_ERROR in err

    code, out, err = AppProcess(host, "--once", "--token", "hunter2", env=env).finish()
    assert (code, err) == (0, "")
    assert len(_drawn(protected_emulator)) == 1


# --- choosing a Pokémon on the bar -------------------------------------------


def _press(emulator, key):
    req = urllib.request.Request(emulator.base + "/api/input?key=" + key, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.getcode() == 200


def _label(emulator):
    return [e for e in _drawn(emulator) if e["type"] == "text"]


def _names(emulator):
    return [e["text"] for e in _label(emulator)]


def _closed(emulator):
    """The companion is back on its own. Closing the menu clears the panel
    and redraws it. So an empty panel is not yet a closed menu."""
    kinds = sorted(e["type"] for e in _drawn(emulator))
    return kinds == ["image"]


def _image_bytes(emulator):
    images = [e for e in _drawn(emulator) if e["type"] == "image"]
    assert len(images) == 1
    return _get(emulator.base + "/assets/" + images[0]["path"])


def _frame_on_disk(slug, name):
    return (_sprite(slug).dir / name).read_bytes()


def _calls_since(emulator, t):
    """Method and path of every request the emulator logged after `t`."""
    return [(c["method"], c["path"]) for c in emulator.snapshot()["log"] if c["t"] > t]


def _showing(emulator, slug):
    """Whether the image on the panel is one of `slug`'s frames."""
    sprite = _sprite(slug)
    on_bar = _image_bytes(emulator)
    return any(on_bar == (sprite.dir / f).read_bytes()
               for s in sprite.states.values() for f in s.frames)


def test_the_dial_opens_a_menu_and_the_wheel_picks_another_pokemon(emulator, tmp_path):
    state = tmp_path / "state.json"
    companion = AppProcess(emulator.host, "--pokemon", "pikachu", "--state", str(state))
    try:
        _wait_until(lambda: _drawn(emulator), 10, "the companion to be drawn")
        assert _label(emulator) == []
        before = emulator.snapshot()["log"][0]["t"]
        _press(emulator, "ok")
        _wait_until(lambda: _names(emulator) == ["Pikachu"], 5, "the menu to open")
        # Opening only parks the Pokémon. The app does not send its frames again.
        assert set(_calls_since(emulator, before)) == {("POST", "/api/input"), ("POST", "/api/display/draw")}
        assert [e["x"] for e in _drawn(emulator) if e["type"] == "image"] == [0]
        _press(emulator, "up")
        _wait_until(lambda: _names(emulator) == ["Pikachu (Female)"], 5,
                    "the wheel to move to the next Pokémon")
        assert _showing(emulator, "pikachu_female")
        time.sleep(app.DOUBLE_CLICK)        # a single click, not a double one
        _press(emulator, "ok")
        _wait_until(lambda: _closed(emulator), 5, "the menu to close")
        assert _showing(emulator, "pikachu_female")
    finally:
        code, out, err = companion.stop()
    assert (code, err) == (0, "")
    assert "dial connected" in out
    assert app.load_state(state)["pokemon"] == "pikachu_female"


def test_a_double_click_sets_the_order_on_the_bar(emulator, tmp_path):
    state = tmp_path / "state.json"
    companion = AppProcess(emulator.host, "--pokemon", "pikachu", "--state", str(state))
    try:
        _wait_until(lambda: _drawn(emulator), 10, "the companion to be drawn")
        _press(emulator, "ok")
        _press(emulator, "ok")
        _wait_until(lambda: _names(emulator) == ["Order: A to Z"], 5, "the order screen")
        _press(emulator, "up")
        _wait_until(lambda: _names(emulator) == ["Order: by number"], 5, "the wheel to flip the order")
        time.sleep(app.DOUBLE_CLICK)
        _press(emulator, "ok")
        _wait_until(lambda: _closed(emulator), 5, "the order screen to close")
        assert app.load_state(state) == {"pokemon": "pikachu", "order": "number"}
        time.sleep(app.DOUBLE_CLICK)
        _press(emulator, "ok")
        _wait_until(lambda: _names(emulator) == ["#025 Pikachu"], 5, "the chooser to count")
        _press(emulator, "back")
        _wait_until(lambda: _closed(emulator), 5, "the chooser to close")
    finally:
        code, out, err = companion.stop()
    assert (code, err) == (0, "")


def test_back_restores_the_pokemon_the_menu_started_from(emulator, tmp_path):
    state = tmp_path / "state.json"
    companion = AppProcess(emulator.host, "--pokemon", "pikachu", "--state", str(state))
    try:
        _wait_until(lambda: _drawn(emulator), 10, "the companion to be drawn")
        _press(emulator, "ok")
        _press(emulator, "down")
        _wait_until(lambda: _names(emulator) == ["Pidgey"], 5,
                    "the wheel to move to the previous Pokémon")
        _press(emulator, "back")
        _wait_until(lambda: _closed(emulator), 5, "the menu to close")
        assert _showing(emulator, "pikachu")
    finally:
        code, out, err = companion.stop()
    assert (code, err) == (0, "")
    assert not state.exists()


def test_emulator_log_is_clean(emulator):
    """Last on purpose: it reads the log every other test wrote."""
    log = emulator.log()
    assert "error" not in log.lower(), log
