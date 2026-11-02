# Pokémon Companion for the BUSY Bar

An animated Pokémon walks and idles across the 72×16 LED panel of a
[BUSY Bar](https://busy.app), in the spirit of
[vscode-pokemon](https://github.com/jakobhoeg/vscode-pokemon). The app ships
the first generation: all 151 Pokémon plus two female variants.

![Pikachu walking across the BUSY Bar panel](pokemon-companion/preview.gif)

The app folder `pokemon-companion/` follows the
[busybar-apps](https://github.com/maxswinkels/busybar-apps) gallery layout.
It holds one stdlib-only `app.py`, a `manifest.yaml`, a `preview.gif`, and
the pre-converted sprite frames in `sprites/`.

## Run it

```bash
cd pokemon-companion
python3 app.py                         # BUSY Bar over USB (10.0.4.20)
python3 app.py --host 192.168.1.50     # a bar on Wi-Fi
python3 app.py --host 127.0.0.1:8080   # the emulator
python3 app.py --pokemon charizard     # pick one. --list prints them all
python3 app.py --order number          # the chooser counts #001, #002, ...
```

On the bar itself, click the dial (OK, or START) to open the chooser. The
Pokémon parks at the left with its name beside it. The wheel steps through
the list. A second click keeps the Pokémon shown. BACK, or eight seconds
without input, brings the old one back.

A double click on OK opens the sorting screen. There the wheel flips between sorting by
A to Z and Pokédex number. Click again to keep the shown order. BACK to cancel. The `--order number` flag sets the same order from the command line.

The app saves the Pokémon and the order in `companion-state.json` next to
`app.py`. On the next start it uses them, unless `--pokemon` or `--order` say
otherwise.

## How it works

`tools/convert_sprites.py` converts the GIFs to PNG frames once, at build
time, for compatibility with the bar. The frames are at most 16 px tall. The
converter crops each sprite to the pixels it uses across idle and walk, and
scales it to fit the panel height. It writes the GIF's frame timing into
`sprites/index.json`, with the Pokédex number and name from the extension's
`pokemon-data.ts`.

At run time the app uploads only the chosen Pokémon's frames. Then it redraws
one `image` element whenever the frame or the position changes, about eight
times a second while the Pokémon walks. Button and wheel events arrive as
protobuf over the bar's `/api/status/ws` websocket. A small stdlib client in a
background thread reads them, the same way the gallery's counter app does.

## Development

Development uses three sibling checkouts next to this repository:

```bash
cd ..
git clone https://github.com/maxswinkels/busybar-emulator    # the bar, locally
git clone https://github.com/maxswinkels/busybar-apps        # gallery tooling
git clone https://github.com/jakobhoeg/vscode-pokemon         # the source GIFs
cd busybar-emulator/web && npm install && npm run build && cd ..
node server.js                                                 # http://127.0.0.1:8080
```

Tests use [uv](https://docs.astral.sh/uv/) for the dev-only dependencies
(Pillow, pytest). The end-to-end suite starts the real emulator on a free
port. It skips when the emulator is not built.

```bash
uv sync
uv run pytest -W error                       # unit + end-to-end
uv run python tools/convert_sprites.py --source ../vscode-pokemon/media \
    --out pokemon-companion/sprites --gen 1 --sheet tools/review/gen1_sheet.png
```

The `--sheet` contact sheets put each original frame next to its 16 px
result, so you can judge the conversion by eye. Git ignores them. Gallery
checks run from the busybar-apps checkout, with `apps/pokemon-companion`
symlinked to this repo's `pokemon-companion/`:

```bash
cd ../busybar-apps
npm ci
npm run check -- pokemon-companion --run
npm run preview -- pokemon-companion --seconds 8 -- --pokemon pikachu
```

## Attribution

The frames come from the animated GIFs in the
[vscode-pokemon](https://github.com/jakobhoeg/vscode-pokemon) project. Those
GIFs are the Pokémon Mystery Dungeon style sprites that the
[PMD Sprite Repository](https://sprites.pmdcollab.org) community draws.
SpriteCollab shares them under
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/), with credit
to the individual artists listed per Pokémon on that site.

Pokémon and the Pokémon character designs are © The Pokémon Company, Nintendo
and Game Freak. This project uses the sprites only as a non-commercial fan
project. We claim no ownership of them. Their licence does not allow commercial
use.

`tools/convert_sprites.py` does the conversion (crop, scale to 16 px, binary
alpha).
