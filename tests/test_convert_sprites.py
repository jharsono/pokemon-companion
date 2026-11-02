"""Tests for tools/convert_sprites.py against real vscode-pokemon GIFs.

The source GIFs come from a vscode-pokemon checkout: VSCODE_POKEMON_MEDIA, or
the sibling checkout ../vscode-pokemon/media. Without either, these tests skip.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from conftest import STRICT_PYTHON, TOOLS

sys.path.insert(0, str(TOOLS))
import convert_sprites as cs  # noqa: E402

PANEL_W, PANEL_H = 72, 16
ASSET_PATH_RE = re.compile(r"[a-zA-Z0-9._/-]{1,64}")     # the firmware's asset file rule
ALL_STATES = {"idle_right", "idle_left", "walk_right", "walk_left"}


def _media_dir():
    env = os.environ.get("VSCODE_POKEMON_MEDIA")
    candidates = [env] if env else []
    candidates.append(Path(__file__).resolve().parents[2] / "vscode-pokemon" / "media")
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    pytest.skip("no vscode-pokemon media dir: set VSCODE_POKEMON_MEDIA")


@pytest.fixture(scope="module")
def media():
    return _media_dir()


@pytest.fixture(scope="module")
def data(media):
    return cs.load_data(cs.data_path(media))


@pytest.fixture(scope="module")
def pikachu(media, data, tmp_path_factory):
    out = tmp_path_factory.mktemp("sprites")
    entry = cs.convert_pokemon(media / "gen1" / "pikachu", out, gen=1, data=data)
    return out, entry


@pytest.fixture(scope="module")
def gen1(media, tmp_path_factory):
    out = tmp_path_factory.mktemp("gen1")
    return out, cs.convert(media, out, gens=[1], data=cs.load_data(cs.data_path(media)))


def _png(path):
    with Image.open(path) as im:
        return im.convert("RGBA")


def _frames(out, entry, slug, color, state):
    files = entry["colors"][color]["states"][state]["frames"]
    return [_png(out / f"gen{entry['gen']}" / slug / color / f) for f in files]


def _lowest_row(im):
    return im.getbbox()[3] - 1


def test_load_frames_keeps_gif_timing(media):
    frames = cs.load_frames(media / "gen1" / "pikachu" / "default_walk_8fps.gif")
    assert [d for _, d in frames] == [300, 300]
    assert all(im.mode == "RGBA" and im.size == (32, 32) for im, _ in frames)


def test_frames_fit_the_panel_and_are_never_upscaled(pikachu):
    out, entry = pikachu
    for state in ALL_STATES:
        for im in _frames(out, entry, "pikachu", "default", state):
            assert im.mode == "RGBA"
            assert im.height <= PANEL_H
            assert im.width <= PANEL_W
            assert im.width <= 32 and im.height <= 32


def test_two_frame_gif_yields_two_pngs_with_durations(pikachu):
    out, entry = pikachu
    walk = entry["colors"]["default"]["states"]["walk_right"]
    assert len(walk["frames"]) == 2
    assert walk["durations_ms"] == [300, 300]
    assert all((out / "gen1" / "pikachu" / "default" / f).is_file() for f in walk["frames"])


def test_identical_consecutive_frames_collapse_into_one(media, tmp_path):
    # bulbasaur's default GIFs are six frames at 100 ms: three identical
    # frames in a row, twice. So the bar should see two frames of 300 ms.
    entry = cs.convert_pokemon(media / "gen1" / "bulbasaur", tmp_path, gen=1)
    idle = entry["colors"]["default"]["states"]["idle_right"]
    assert idle["durations_ms"] == [300, 300]
    frames = _frames(tmp_path, entry, "bulbasaur", "default", "idle_right")
    assert len(frames) == 2
    assert frames[0].tobytes() != frames[1].tobytes()


def test_idle_and_walk_share_one_scale(pikachu):
    out, entry = pikachu
    color = entry["colors"]["default"]
    sizes = {im.size for state in color["states"] for im in _frames(out, entry, "pikachu", "default", state)}
    assert sizes == {(color["w"], color["h"])}
    assert color["h"] == PANEL_H


def test_left_facing_is_a_mirror_when_no_dedicated_sprite(pikachu):
    out, entry = pikachu
    for state in ("idle", "walk"):
        rights = _frames(out, entry, "pikachu", "default", state + "_right")
        lefts = _frames(out, entry, "pikachu", "default", state + "_left")
        assert entry["colors"]["default"]["states"][state + "_left"]["mirrored"] is True
        for r, l in zip(rights, lefts):
            assert l.tobytes() == r.transpose(Image.FLIP_LEFT_RIGHT).tobytes()


def test_dedicated_walk_left_sprite_is_used_when_present(media, tmp_path):
    # gen2/pichu_spiky_eared ships default_walk_left_8fps.gif
    src = media / "gen2" / "pichu_spiky_eared"
    if not (src / "default_walk_left_8fps.gif").is_file():
        pytest.skip("source has no dedicated walk_left sprite")
    entry = cs.convert_pokemon(src, tmp_path, gen=2)
    walk_left = entry["colors"]["default"]["states"]["walk_left"]
    assert (walk_left["source"], walk_left["mirrored"]) == ("default_walk_left_8fps.gif", False)
    walk_right = entry["colors"]["default"]["states"]["walk_right"]
    assert (walk_right["source"], walk_right["mirrored"]) == ("default_walk_8fps.gif", False)


def test_feet_sit_on_the_bottom_row(pikachu):
    out, entry = pikachu
    lowest = {state: max(_lowest_row(im) for im in _frames(out, entry, "pikachu", "default", state))
              for state in ALL_STATES}
    # idle and walk share one crop box. So the lower of the two touches the
    # last row, and the other may sit a pixel or two above it.
    assert max(lowest.values()) == PANEL_H - 1
    assert min(lowest.values()) >= PANEL_H - 3


def test_alpha_is_binary_for_both_resample_filters(media, tmp_path):
    for resample in ("nearest", "box"):
        entry = cs.convert_pokemon(media / "gen1" / "pikachu", tmp_path / resample, gen=1, resample=resample)
        for im in _frames(tmp_path / resample, entry, "pikachu", "default", "walk_right"):
            assert set(im.getchannel("A").tobytes()) <= {0, 255}
            assert im.size == (entry["colors"]["default"]["w"], entry["colors"]["default"]["h"])


def test_box_filter_does_not_darken_edges_with_transparent_neighbours():
    # A yellow disc on transparent black. If the filter averaged the black
    # into the edge pixels, it would leave a dark fringe. So every surviving
    # pixel must stay yellow.
    yellow = (255, 220, 0, 255)
    src = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    ImageDraw.Draw(src).ellipse((3, 3, 28, 28), fill=yellow)
    out = cs.fit(src, src.getbbox(), (16, 16), Image.BOX)
    opaque = [px for px in out.get_flattened_data() if px[3] == 255]
    assert opaque
    assert all(max(abs(a - b) for a, b in zip(px[:3], yellow[:3])) <= 2 for px in opaque)


def test_index_entry_shape(pikachu):
    out, entry = pikachu
    assert entry["gen"] == 1
    assert entry["name"] == "Pikachu"
    assert entry["number"] == 25
    assert set(entry["colors"]) == {"default"}
    color = entry["colors"]["default"]
    assert set(color["states"]) == ALL_STATES
    assert color["source_size"] == [17, 19]     # idle and walk together
    for state in color["states"].values():
        assert len(state["frames"]) == len(state["durations_ms"])
        assert all(ASSET_PATH_RE.fullmatch(f) for f in state["frames"])


def test_display_name_from_slug():
    assert cs.display_name("venusaur_female") == "Venusaur (Female)"
    assert cs.display_name("mr_mime") == "Mr Mime"
    assert cs.display_name("nidoran_male") == "Nidoran (Male)"


def test_load_data_reads_pokedex_numbers_and_names(data):
    assert data["pikachu"] == {"number": 25, "name": "Pikachu"}
    assert data["venusaur_female"]["number"] == 3       # a variant keeps its number
    assert data["mew"]["number"] == 151


def test_names_come_from_the_extension_with_the_slug_as_fallback(media, data, tmp_path):
    entry = cs.convert_pokemon(media / "gen1" / "mrmime", tmp_path, gen=1, data=data)
    assert entry["name"] == data["mrmime"]["name"] == "Mr. Mime"
    assert entry["number"] == 122
    entry = cs.convert_pokemon(media / "gen1" / "mrmime", tmp_path, gen=1, data={})
    assert entry["name"] == "Mrmime"
    assert "number" not in entry


def test_non_ascii_names_fall_back_to_the_slug(media, data, tmp_path):
    # The bar's bitmap fonts are ASCII only.
    assert data["nidoran_female"]["name"] == "Nidoran\u2640"
    entry = cs.convert_pokemon(media / "gen1" / "nidoran_female", tmp_path, gen=1, data=data)
    assert entry["name"] == "Nidoran (Female)"
    assert entry["number"] == 29


def test_gen1_numbers_run_from_1_to_151(gen1):
    out, index = gen1
    numbers = {e["number"] for e in index.values()}
    assert numbers == set(range(1, 152))


def test_convert_pokemon_without_gifs_returns_none(tmp_path):
    (tmp_path / "empty").mkdir()
    assert cs.convert_pokemon(tmp_path / "empty", tmp_path / "out", gen=1) is None


def test_convert_gen_filter_writes_index_and_only_that_gen(media, tmp_path):
    index = cs.convert(media, tmp_path, gens=[1], only=["bulbasaur", "charmander"])
    assert set(index) == {"bulbasaur", "charmander"}
    written = json.loads((tmp_path / "index.json").read_text())
    assert written == index
    assert (tmp_path / "gen1" / "bulbasaur" / "default").is_dir()
    assert not (tmp_path / "gen2").exists()


def test_every_gen1_pokemon_converts_within_the_panel(gen1):
    out, index = gen1
    assert len(index) == 153
    for slug, entry in index.items():
        for color, info in entry["colors"].items():
            assert set(info["states"]) == ALL_STATES, slug
            assert 1 <= info["h"] <= PANEL_H and 1 <= info["w"] <= PANEL_W, slug
            for state in info["states"]:
                for im in _frames(out, entry, slug, color, state):
                    assert im.size == (info["w"], info["h"]), (slug, color, state)


def test_command_line_without_the_data_file_fails_cleanly(media, tmp_path):
    out = subprocess.run(STRICT_PYTHON + [str(TOOLS / "convert_sprites.py"),
                          "--source", str(media), "--out", str(tmp_path), "--gen", "1",
                          "--only", "pikachu", "--data", str(tmp_path / "nope.ts")],
                         capture_output=True, text=True)
    assert out.returncode == 1
    assert "nope.ts" in out.stderr and "Traceback" not in out.stderr


def test_contact_sheet_is_written(media, tmp_path):
    cs.convert(media, tmp_path, gens=[1], only=["bulbasaur"], sheet=tmp_path / "sheet.png")
    with Image.open(tmp_path / "sheet.png") as sheet:
        assert sheet.width > 0 and sheet.height > 0


def test_contact_sheet_pages(media, tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "SHEET_ROWS_PER_PAGE", 1)
    # two pokemon = two rows: a full page and one more
    cs.convert(media, tmp_path, gens=[1], only=["bulbasaur", "eevee"], sheet=tmp_path / "sheet.png")
    assert (tmp_path / "sheet.png").is_file()
    assert (tmp_path / "sheet-p2.png").is_file()
    assert not (tmp_path / "sheet-p3.png").exists()


def test_command_line(media, tmp_path):
    out = subprocess.run(STRICT_PYTHON + [str(TOOLS / "convert_sprites.py"),
                          "--source", str(media), "--out", str(tmp_path), "--gen", "1",
                          "--only", "pikachu", "--only", "eevee", "--sheet", str(tmp_path / "sheet.png")],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == f"converted 2 pokemon, 16 frames -> {tmp_path}"
    assert out.stderr == ""
    assert (tmp_path / "sheet.png").is_file()
