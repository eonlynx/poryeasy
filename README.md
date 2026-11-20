# Poryeasy

Poryeasy is a companion tool for Porytiles. It exports a **complete map
instance** (for example `LittlerootTown`) into editable `bottom.png`,
`middle.png`, and `top.png` layers and imports those layers back into Porymap's
compiled assets.

This is different from `porytiles decompile-tileset`, which exports a *tileset*
(primary/secondary). Poryeasy renders the map's actual layout with its
primary + secondary tilesets, one 16x16 block per map cell.

## Repository layout

```text
poryeasy/               this project
  porytiles_map.py      command-line entry point
  ptmap/                Python package (export/import implementation)
  tests/                integration tests
```

Poryeasy works standalone. Porytiles is optional: it is only needed if you
want to refresh `porytiles_src/` assets or rebuild Porytiles-managed tilesets
after an import.

## Clone

```sh
git clone <poryeasy-repository>
cd poryeasy
python -m pip install pillow
python porytiles_map.py --help
```

If you need `--sync-porytiles`, install/build Porytiles separately and put a
`porytiles` executable on `PATH`.

The import side is designed for GIMP/Aseprite-style editing:

* every 16x16 block is compared against the existing map;
* blocks that already match an existing metatile are remapped automatically;
* genuinely new blocks ask what to do (modify globally, add a new metatile,
  etc.);
* metatiles and tile graphics that become unused can be cleared from the
  tileset;
* the tool writes `map.bin`, `tiles.png`, `metatiles.bin`,
  `metatile_attributes.bin`, and the generated `-num_tiles` limit in
  `src/data/tilesets/graphics.h` when needed — no manual tileset compilation.

## Requirements

* Python 3.10+
* [Pillow](https://python-pillow.org/) (`pip install pillow`)

Or use [uv](https://docs.astral.sh/uv/) and let the script's inline metadata
install Pillow automatically:

```sh
uv run porytiles_map.py --help
```

The tool expects to run inside a pokeemerald / pokeemerald-expansion project
(or receive `--project-root`).

## Quick start

From the decomp project root:

```sh
# 1. Export the map layers
python <poryeasy>/porytiles_map.py export LittlerootTown

#    Output (by default):
#    poryeasy/maps/LittlerootTown/
#      bottom.png
#      middle.png
#      top.png
#      preview.png     (composited preview; not used by import)
#      animated_metatiles.png (animated metatiles; rows=metatiles, columns=states)
#      animated_metatiles.json (sheet manifest; not edited by hand)
#      palettes.png    (editable palette swatches with usage counts)
#      palettes.json   (palette manifest; not edited by hand)
#      map.json        (export metadata)

# 2. Edit the layer PNGs in GIMP/Aseprite

# 3. Import the edits
python <poryeasy>/porytiles_map.py import LittlerootTown
```

A map can be selected by map folder (`LittlerootTown`), map symbol
(`MAP_LITTLEROOT_TOWN`), map name, layout ID (`LAYOUT_LITTLEROOT_TOWN`), or
layout name. For ordinary same-size edits, `poryeasy import <map>` is enough;
the more advanced flags have safe defaults and only need to be used when you
want a different policy.

For importing layers between two different decomp projects, see
[`docs/IMPORTING_MAPS.md`](docs/IMPORTING_MAPS.md).

Use `info` to see what a name resolves to:

```sh
python <poryeasy>/porytiles_map.py info LittlerootTown
```

Use `usages` to inspect the same global map/border usage counts that the import
uses when deciding whether a metatile can be removed:

```sh
python <poryeasy>/porytiles_map.py usages LittlerootTown
python <poryeasy>/porytiles_map.py usages LittlerootTown --metatile 0x211
```

## Editing rules

* For ordinary edits, keep the image the same size. To expand or crop, see
  “Canvas size and expanding maps” below; Poryeasy accepts the new size and
  updates the layout for you.
* Keep PNGs as RGBA. Fully transparent pixels should have alpha 0. Pixels with
  any non-zero alpha are treated as opaque (GBA tiles have no partial
  transparency).
* Prefer colours already present in the map. By default `--palette-mode smart`
  rewrites the least-used palette slots when edited art contains outside
  colours. `--palette-mode replace` rewrites the affected palette,
  `--palette-mode nearest` maps to existing colours, and
  `--palette-mode error` refuses. A separate `--quantize` option controls
  nearest-colour substitution when palette rewriting is disabled.
* Each 16x16 block is one metatile. You can edit as many blocks as you like in
  one import.
* In dual-layer projects (the default for pokeemerald), `middle.png` is fully
  transparent and editing it is an error. `--layer-mode triple` can override
  header detection for projects with triple-layer metatiles.

GIMP workflow: open all three PNGs as layers in one image (bottom at the
bottom), edit with the layer you want active, then export each layer back to
its matching filename. The import reads the three files independently.

## What happens to an edited block?

1. **Unchanged**: ignored.
2. **Matches an existing metatile**: the map block is remapped to it
   automatically (no tileset changes).
3. **New content**: the tool asks, once per distinct edit:

   | Option | Effect | Scope |
   | --- | --- | --- |
   | `1` Modify the original metatile | Replaces that metatile's entries | Every map using that metatile changes |
   | `2` Modify the underlying 8x8 tiles | Edits tile graphics in place | Every metatile/tile user changes (useful for global tree/grass redesigns) |
   | `3` Add to the secondary tileset | New metatile + new tiles as needed | Only the edited block(s) |
   | `4` Add to the primary tileset | Same, in the primary tileset | Only the edited block(s); primary may be full |
   | `5` Replace existing slots | Full overhaul: new contents take over free/original metatile slots | Can affect every map using the replaced slots |
   | `s` Skip | Leaves the block untouched | — |

Option 2 is only offered when the new pixels fit the original palette exactly and
the target 8x8 tile is not animated. Option 4 is only offered when the original
metatile contains no secondary-tile references and the primary tileset has free
metatile/tile slots.

Option 5 is **replace mode**. It is intended for a complete art overhaul where
the old metatile identities no longer mean anything. Poryeasy:

* groups the new 16x16 contents by rendered pixels (plus behaviour attribute);
* counts how many cells in the **edited image** use each new content;
* finds metatile IDs that will have zero references in this map and its border
  after all edits;
* assigns the most-used new contents to the most-used freed slots first, then
  appends the rest when slots run out (for example 35 old slots, 40 new contents:
  the 35 most-used contents replace and the remaining 5 are added);
* never overwrites an 8x8 tile slot that is still used by a metatile that
  remains in the map or border; if the freed slot shared artwork with kept
  content, it allocates another tile or appends a new one instead;
* updates the map cells to the assigned IDs and copies the *new* content's
  attributes, not the old slot's attributes.

Replacing a slot that other maps still use changes those maps too. That is
accepted by replace mode, but you should review/redo those maps in the same
overhaul. Protected IDs from `metatile_labels.h` are not reused in replace mode;
they are skipped with a warning.

For automation, skip the prompts:

```sh
# Local additions to the secondary tileset
... import LittlerootTown --on-new add-secondary --on-remove keep

# Full overhaul: replace/free slots, append only the excess
... import LittlerootTown --on-new replace --on-remove keep

# Validate without writing
... import LittlerootTown --on-new skip --on-remove keep --dry-run
```

## Recommended workflow for a full tileset overhaul

When the art changes completely, Poryeasy works by **content**, not by old
metatile IDs:

1. Start with one map that contains the core shared art (for example
   LittlerootTown). Export it and apply the shared/base changes first. Use
   `modify-metatile` or `modify-tiles` for content that should change
   everywhere; use `add-secondary` for genuinely new blocks.
2. Import that map. Those new blocks now exist in the project's metatile pool.
3. Do the same for the next map, keeping the edits consistent. Blocks that match
   the new pool are remapped automatically; only content that did not exist yet
   is added.
4. Once the new shared pool is established, finish per-map layout/order changes
   and re-export freely.

This is why consistency matters. If LittlerootTown's ground is changed but
PetalburgCity is not, Petalburg may temporarily show the old ground or a
mismatched metatile. Importing Petalburg with the same new ground teaches
Poryeasy that the content is shared, so it does not duplicate it. For large
overhauls, commit after each phase so you can compare/roll back.

Current behavior when a new block does not exist anywhere depends on the mode:

* `add-secondary`/`add-primary` use a globally-unused or already-transparent
  slot when possible, otherwise append to the chosen tileset.
* `replace` (option 5) first reuses metatile slots that become unused by this
  map after the edit, then appends the excess. This is the mode for the
  "old map used 35 metatiles, new map uses 40" workflow.
* If the edited map has fewer new contents than freed slots, the remaining
  freed slots stay empty instead of being filled with duplicate content.
* Newly added metatiles are assigned in row-major order of their first
  appearance in the map, filling the lowest free slots first.

Replace mode does not need to know which old metatile was "the ground" or "the
house". It only needs the set of slots that are free after the edit and the new
contents. The most frequently painted new contents take the freed slots first;
this keeps the dominant materials (ground, trees, water) together even when the
map layout changes completely.

## Battle Frontier outside split workflow

The Battle Frontier outside area is split into four layouts because the GBA
secondary tileset limit (512 tiles/metatiles per half) cannot hold the whole
area at once:

```text
West South   gTileset_BattleFrontierOutsideWestSouth
East South   gTileset_BattleFrontierOutsideEastSouth
West North   gTileset_BattleFrontierOutsideWestNorth
East North   gTileset_BattleFrontierOutsideEastNorth
```

The original full tilesets remain the source of truth:

```text
data/tilesets/secondary/battle_frontier_outside_west
data/tilesets/secondary/battle_frontier_outside_east
```

### Why IDs alone are not enough

When the player crosses a map connection, the engine renders the neighbouring
map's metatile IDs with the **current** map's secondary tileset. Two metatiles
with the same ID can still look wrong if their 16-byte entries point at
different 8x8 tile slots or different palette banks. The border therefore
requires all of the following to match in both tilesets:

* the metatile ID,
* the 16-byte metatile entry (both layers),
* the secondary tile indices used by those words,
* the palette bank bits and the palette contents in that bank.

`exact-border` enforces all four. It checks the bands the engine actually
copies: 7 columns into a west connection, 8 columns into an east connection,
and 7 rows into a north/south connection.

### Commands

```sh
# Rebuild the four split tilesets from the original East/West bases.
# Original IDs/positions are preserved and new content is placed in free
# slots in map order. exact-border runs automatically at the end.
python <poryeasy>/porytiles_map.py preserve-rebuild --project-root <project>

# Run only the exact border pass. It is idempotent; on an already-correct
# project it reports 0 changed entries, 0 moved tiles and 0 mismatches.
python <poryeasy>/porytiles_map.py exact-border --project-root <project>

# Validate without writing
python <poryeasy>/porytiles_map.py exact-border --project-root <project> --dry-run
```

`import` also runs `exact-border` automatically when the imported map is one of
the four Battle Frontier outside layouts. Use `--no-exact-border` to skip it
(for example while debugging an import).

`preserve-rebuild` reads the original pre-split map block data from git history
when `HEAD` already contains the split layouts, so it remains reproducible
after the split is committed. It writes a timestamped backup under
`.poryeasy-backups/` before changing tilesets.

### Guarantees checked by the tool

* visible seam entries are byte-identical on both sides;
* tile patterns referenced by those entries exist at the same indices;
* secondary palette banks used by those entries contain the same colours;
* every map block keeps the same rendered appearance (the pass only reorders
  tile slots and palette banks, it does not change art);
* all four visible seams report zero mismatches.

If any of these checks fails, the command stops without writing files.

## Canvas size and expanding maps

Poryeasy accepts layer images whose size differs from the layout and updates the
layout automatically. You do **not** have to tell it the new dimensions, and it
does not search for the old map inside the new image.

The default anchor is `top-left`: the old map stays at `(0,0)` and new blocks are
added to the right/bottom. For other expansions, declare where the old map sits:

```sh
# Add columns/rows on the left and/or top
... import LittlerootTown --offset-x 2 --offset-y 1

# Or use a predefined anchor
... import LittlerootTown --anchor bottom-right

# Crop (dangerous; explicit)
... import LittlerootTown --allow-shrink --offset-x 2 --offset-y 1
```

On a size change Poryeasy:

* computes the new layout dimensions from the PNG size;
* rebuilds `map.bin` in row-major order, keeping old cells at the chosen offset;
* fills cells outside the old map with `--fill-metatile` (default `0`);
* updates `width`/`height` in `layouts.json` without reformatting the file;
* shifts `x`/`y` coordinates in every map using that layout when the offset is
  non-zero (disable with `--no-shift-events`);
* keeps `border.bin` unchanged;
* warns about shared layouts and map connections that may need review.

New cells are then imported like any other block: if their pixels match an
existing metatile they are remapped, otherwise the normal `--on-new` policy or
prompt applies. `--offset-x`/`--offset-y` or `--anchor` are the only ways to say
where the old map went; there is no automatic image registration.

## Animated tiles and doors

Poryeasy exports `animated_metatiles.png`: **one row per animated metatile used
by the selected map, and one column per animation state**. The same 16x16 cell
grid is used for every row. `animated_metatiles.json` maps each row to its
metatile ID, animation name, frame files, destination tile range, door part and
palette information.

For normal tileset animations, each cell is the full 16x16 metatile rendered
with that animation frame. For doors, each row is one 16x16 part of the door
(e.g. top/bottom for a 1x2 door, or four quadrants for a 2x2 door) and each
column is an open frame. 1x1, 1x2 and packed 2x2 door layouts are supported; the
packed `-mwidth`/`-mheight` tile reorder is handled automatically.

On import:

* If `animated_metatiles.png` is byte-identical to the exported sheet (hash in the
  manifest), animation processing is skipped entirely. Ordinary map edits do not
  pay the cost of animation parsing or state rendering.
* Changed cells are converted back to indexed 8x8 tiles using the metatile or
  door palette, then written to `data/tilesets/.../anim/*.png` or
  `graphics/door_anims/*.png`.
* Frame 0 changes also update the static animated tile slots in `tiles.png`, so
  the map's initial frame reflects the new art.
* Map blocks are matched against every animated state of every animated
  metatile, not just frame 0. Moving an animated metatile within the map keeps
  its animation; painting a different state into the map still resolves to the
  animated metatile.
* Animated tile slots are never allocated for new static tiles and are protected
  from unused-tile cleanup.
* `modify-tiles` refuses to overwrite animated slots by default
  (`--allow-animated-edits` overrides this).

Editing the art of an existing door rewrites only
`graphics/door_anims/<name>.png`. If the edited colours fit a different tileset
palette, Poryeasy updates the corresponding entry in the door's
`sDoorAnimPalettes_*` array in `field_door.c` automatically. Adding door states
is described below.

### Authoring new doors

Append `parts` consecutive rows to `animated_metatiles.png`, where `parts` is 4
for a 2x2 door, 2 for 1x2 and 1 for 1x1. In interactive mode choose `door`, then:

* the base door whose timing, sound and geometry you want to reuse;
* a name for the new door.

Poryeasy then creates:

* `graphics/door_anims/<name>.png`, including the packed 2x2 layout options when
  needed;
* a new metatile in the base door's tileset, cloned from the base door
  metatile, with a new entry in `include/constants/metatile_labels.h`;
* a dedicated `sDoorAnimPalettes_<Name>[]` array initialized from the base door;
* a `sDoorAnimTiles_<Name>[]` graphics declaration and a
  `sDoorAnimGraphicsTable` entry in `src/field_door.c`, reusing the base door's
  size/sound/timing.

The new door is also added to `animated_metatiles.json` immediately. Paint its
metatile in the map to use it. Its behaviour/attribute data is cloned from the
base door.

### Map edit vs animation sheet edit

The animation sheet is **authoritative** for animated graphics:

* Editing only `top.png`/`middle.png`/`bottom.png` does not invent new animation
  frames. If the edited cell still matches any exported animation state (for
  example because you moved the flower), the animated metatile is reused and the
  animation is preserved. If it matches no state, it becomes new static content
  and Poryeasy warns when an originally animated cell loses its animation.
* Editing only `animated_metatiles.png` updates the animation frames and static
  frame 0. The map itself does not need to be repainted.
* Animation frames are shared by every map using that tileset animation, so
  sheet edits intentionally apply globally. Poryeasy does not skip them just
  because one map stopped showing that metatile.

### Adding animation states

To add states to an existing animated metatile:

1. Widen `animated_metatiles.png` to the right and paint the new state cell(s).
2. Import. For a row with a single animation, Poryeasy creates new frame PNGs
   and appends the declarations/frame references to `src/tileset_anims.c`.
3. The refreshed sheet/manifest is written automatically; re-export is not
   required.

Extra columns on multi-animation rows are ignored with a warning, because it is
ambiguous which animation should receive the extra frame.

To add states to a door, widen the sheet and paint the new columns in **every
part row** of that door. Poryeasy extends the door PNG, creates dedicated
`sDoorAnimFrames_Open<Name>`/`Close<Name>` tables, adds a new `DOOR_SIZE_*`
enum/`sDoorSizeInfo` entry, repoints that door's `DoorGraphics` entry and adds
the required switch cases to `field_door.c`. The custom size keeps other doors
of the same geometry on their original timing. Unpainted parts inherit the
previous state's tiles.

### Adding completely new rows

If you append rows to `animated_metatiles.png`, they have no manifest entry.
Poryeasy detects them and, in interactive mode, asks whether each row is:

* an **animated metatile** using one of the existing animations for this
  tileset pair;
* a **new tileset animation** (creates frame PNGs, destination tiles,
  `QueueAnimTiles_*`, frame arrays, callback/init code and header registration);
* a **door** (see “Authoring new doors” above); or
* skip.

for an animated metatile, select the animation, target tileset (secondary by
default) and optionally a behaviour name/number. Poryeasy creates the metatile
in the tileset (art in the bottom layer), reusing the selected animation's
existing art and all of its states, and adds its state signatures to the
matching index, so map cells painted with any of those states resolve to it.
Choose “new tileset animation” if the painted row should define new frames.

For a new tileset animation, choose a name, target tileset, frame count and
timer divisor. One 16x16 metatile (four 8x8 tiles) is allocated as the
animation's destination; each frame is written to
`data/tilesets/<kind>/<tileset>/anim/<name>/N.png`. New rows animate the whole
16x16 metatile; single-tile animations still need source work. Poryeasy patches
`src/tileset_anims.c` (declarations, frame array, queue function and callback),
plus `include/tileset_anims.h` and `src/data/tilesets/headers.h` when the tileset
had no animation callback yet.

After creating any new row, Poryeasy also rewrites `animated_metatiles.png` and
`animated_metatiles.json` so the new rows have permanent manifest entries and
the next import does not ask again. Missing frames are filled from the generated
art; extra columns beyond the animation length are cleared with a warning.

Use `--on-extra-animation-row skip` for automation. Extra rows are never guessed
silently.

### Adding animated metatiles

Paint the new metatile in the map image using existing animated tile graphics and
import the map normally. Poryeasy will create the metatile referencing the
animated slots. Re-export the sheet afterwards and the new metatile gets its own
row. Adding a completely new animation range (new destination slots/frames) is a
source-code change, not something the sheet can infer from pixels.

Palette-only animations are listed in `palette_animation_tilesets` in `map.json`.
The runtime animation overwrites tile graphics; editing only the static frame is
pointless unless you also edit the animation sheet or source frames.

## Palette editing

`palettes.png` contains one row per hardware palette visible with the map's
tileset pair, and 16 swatches per row. `palettes.json` records the palette file,
bank, tileset, current colours, and usage counts:

* `used_metatiles`: how many metatiles across readable maps/borders reference
  that palette bank;
* `used_pixels[slot]`: how many 8x8 tile pixels use each colour slot;
* `complete`: whether the project-wide usage scan could read every layout.

Edit a swatch to change that colour globally for the palette bank. On import:

* unchanged palettes are skipped via the sheet hash;
* changed colours that are in use produce a warning with the usage count;
* if usage could not be fully scanned, palette edits are refused unless
  `--force-palette-edits` is passed;
* index 0 is transparent and is not editable;
* palette files are backed up like every other modified file.

Changing a palette colour affects every map that shares that palette. For a
local-only recolour, duplicate/edit the tileset palette and assign the new bank
to the relevant tiles/metatiles instead.

### Colours outside the palette

Map and animated-sheet edits can use colours that no active palette contains.
This is controlled by `--palette-mode`:

| Mode | Behaviour |
| --- | --- |
| `smart` (default) | Rewrites only the least-used palette slots needed to fit the new colours. Protects at least one representative per hue family (for example, one green even if blue has ten shades), and protects high-use slots. Existing tiles keep their indices, so their colours adapt automatically. |
| `replace` | Rewrites the affected palette with the colours present in the edited art, so existing tiles adapt to the new palette. |
| `nearest` | Does not rewrite palettes; maps each outside colour to the nearest existing colour. |
| `error` | Refuses the edit. |

Palette influence is measured from the resulting map: unchanged cells carry the
old content's weight and edited cells carry the new content's weight. Only
palette *colours* change; tile/metatile indices stay stable. Palette rewrites
are backed up, affect every map sharing that palette bank, and are also
available when editing animated metatiles and doors.

Palette colours are weighted by how many pixels of the **resulting map** use
them. Unchanged cells count as the old map; edited cells count as the new
content. A colour that appears in more of the final map therefore has more
influence than one used by a single new block. In addition, a new colour is only
considered when it reaches `--palette-min-share` (default `0.5` percent of the
map's opaque pixels). Colours below that threshold are not written to any
palette; they are mapped to the closest existing colour instead.

This is what prevents an accidental pixel from rewriting a palette: a single
pixel, or a handful of pixels, is far below the default share. A deliberate
full-map recolour still has enough weight to change the palette. Use
`--palette-min-share 0` to disable the filter.

### Tiny accidental edits

A stray pixel can still create or modify a metatile even when it cannot change
the palette. Poryeasy reports changes of at most `--tiny-edit-pixels` pixels
(default `4`) with the block coordinates and pixel count:

```text
warning: 1 tiny edit(s) would create or modify metatile content: (12,7)(1px).
If they were accidental, fix the layer PNG(s) and re-import.
```

To make this a hard stop before the map/tileset changes are written:

```sh
... import LittlerootTown --tiny-edit-policy error
```

Policies are `warn` (default), `error` (abort the import), and `allow` (do not
warn). Tiny edits never influence palettes. If you correct the accidental pixel
in the layer PNG and import again, the block matches its original metatile and
is auto-remapped instead of creating a new one. For a non-interactive check,
combine `--dry-run` with `--tiny-edit-policy error`.

### Reusing near-identical 8x8 tiles

Edited 8x8 tiles are matched against the existing tile pool with a small pixel
tolerance. If a changed tile is close enough to an existing tile, Poryeasy
reuses that tile instead of allocating a new compiled tile. The original tile
of the edited subtile is checked first; horizontal/vertical flips are also
considered.

- `--tile-match-tolerance N` (default `1`) applies to normal edits. Use `0` to
  disable fuzzy matching completely.
- Tiny edits up to `--tiny-edit-pixels` (default `4`) automatically use the
  larger tiny-edit threshold, so a one-to-four-pixel accidental change reuses
  the previous tile.
- The reused tile is reported in the summary (`reused near tiles`), and the
  validation accepts the small difference as a harmonization instead of
  failing.

A tiny edit can still create or replace a metatile, but that metatile is built
from existing tiles: no new 8x8 tile is compiled. If the harmonized metatile
matches an existing one, the map cell is remapped to it directly.

## Tile limits and per-map tileset variants

GBA tilesets have a hard tile limit (normally 512 primary and 512 secondary
tiles). When an import would exceed a limit, Poryeasy automatically creates a
variant for the affected map instead of failing:

* the variant is named `<base>_<MapName>` (for example
  `gTileset_General_LittlerootTown`);
* it gets its own folder under `data/tilesets/primary` or
  `data/tilesets/secondary`, plus entries in `tilesets.h`, `graphics.c`,
  `metatiles.h` and `tilesets.h`;
* it keeps the map's used tiles, the new tiles, and then fills the remaining
  slots with the most-used tiles from the original tileset across the rest of
  the project's maps/borders;
* tiles that do not fit are replaced by the nearest kept tile, so the map
  remains as close as possible to the intended art;
* the affected layout in `layouts.json` is switched to the variant, so only
  maps using that layout change;
* the original tileset is truncated/remapped as far as possible, so shared
  changes still apply to it up to the limit.

Use `--force-variant` to create a per-map variant even when the tileset is under
the tile limit. Pass `primary`, `secondary`, or omit the value for both:

```sh
# Keep the palette and tile edits local to this map
... import LittlerootTown --force-variant secondary --on-new add-secondary
```

This is useful when you want to adapt palettes or add tiles for one map without
changing the base tileset used by every other map.

Variant metadata is stored in
`data/tilesets/<kind>/<base_folder>_variants.json`. It records the base
tileset, `base_maps`, variant names/folders, affected maps/layouts, variant→base
tile mapping, and baseline pattern/palette hashes. For example:

```json
{
  "base": "gTileset_General",
  "base_maps": ["PetalburgCity", "Route101"],
  "variants": [
    {
      "name": "gTileset_General_LittlerootTown",
      "maps": ["LittlerootTown"],
      "layouts": ["LAYOUT_LITTLEROOT_TOWN"]
    }
  ]
}
```

Later imports of the base tileset propagate changed tiles and palettes to every
variant whose copy has not been locally overridden.

Variants start with `.callback = NULL`; if the original tileset used runtime
animations, re-register the appropriate animation callback after reviewing the
variant. New variants and their source entries are included in the normal
backup set.

## Other considerations

* **Palettes:** use `palettes.png` for global palette-colour edits, or
  `--palette-mode smart/replace` for outside colours in edited art. Palette
  changes affect every map using that palette bank.
* **Primary vs secondary:** primary metatiles/tiles are shared by every map using
  that primary tileset; secondary slots are local to the layout's secondary
  tileset. Prefer `add-secondary` unless the content is truly global.
* **Metatile attributes:** new metatiles copy the original metatile's
  behaviour/attributes. Check special blocks (doors, ledges, water, triggers) in
  Porymap afterwards; per-block collision/elevation is preserved in `map.bin`.
* **Hard-coded IDs:** metatile labels, door animations and some source constants
  refer to fixed IDs. Poryeasy clears unused slots instead of renumbering later
  metatiles, and never clears labels listed in `metatile_labels.h`.
* **Usage completeness:** deletion is disabled for a tileset when any layout
  that could reference it is unreadable. Fix stale `layouts.json` entries rather
  than forcing deletion.
* **Limits:** GBA tilesets have finite tile/metatile slots. When the tile limit
  is exceeded, Poryeasy creates a per-map variant automatically; see “Tile
  limits and per-map tileset variants” above.
* **Attribute/behaviour editing:** a map editor that only changes pixels cannot
  infer that an existing block should become a ledge, warp or water tile. New
  animated metatiles/animation rows can set a behaviour in the interactive
  prompt; other attribute edits still belong in Porymap.
* **Performance:** simple imports do not scan the whole project. Project-wide
  metatile usage is only needed for explicit `usages` queries, replace-mode
  slot planning, palette usage counts during export, or clearing/reusing slots.
  Removal checks are limited to layouts that can actually reference the affected
  tileset. Resize, replace, animation parsing and palette processing are lazy:
  unchanged animation/palette sheets are skipped by hash during import.
* **Backups and Git:** every import backs up modified files, but a commit per
  phase is still the best way to review large overhauls.
* **Reloading:** close/reload the tileset and map in Porymap after an import;
  Porymap caches tileset data.
* **Tiles.png layout:** Poryeasy updates `tiles.png`, trailing transparent
  padding, and the generated `-num_tiles` limit in
  `src/data/tilesets/graphics.h` when present.



## Unused metatiles and tiles

Deletion is intentionally conservative. The tool only offers to clear a
metatile when **its usage count is exactly 0 after the edit** in every readable
map and border in the project. It shows the count before and after the edit:

```
Metatile 0x200 in gTileset_Petalburg: usages before edit = 1, usages after edit = 0.
  Clear this now-unused metatile slot from the tileset? [y/N]:
```

If a metatile is still used anywhere after the edit, it is never offered for
deletion. If any layout that could reference the tileset cannot be read (missing
`map.bin`/`border.bin`, malformed file, etc.), the tool refuses to clear
metatiles from that tileset and reports which layouts were unreadable. This
avoids deleting something that may still be used by a map the scan could not
inspect.

Clearing zeroes the metatile entries and attributes and makes the slot
available for future additions. It does not compact/shift metatile IDs, because
Porytiles and the game source contain hard-coded metatile labels (doors, etc.).
Metatiles named in `include/constants/metatile_labels.h` are never cleared and a
note is printed if they become unused.

### Reusing existing free slots (default)

Before every import, Poryeasy also clears slots in the map's primary and
secondary tilesets that have **zero usages in every readable map and border**.
This captures pre-existing holes (for example `gTileset_TrainerHill` from
`0x3D0` onward) and any slot that was emptied manually. New content fills those
slots from the lowest ID upward before appending new metatiles.

A metatile is treated as already free when all of its referenced 8x8 patterns
are fully transparent. Palettes use index 0 as the GBA transparency colour, so
this checks the rendered pattern pixels rather than only whether the entry
literally points at tile 0. Such slots are available without rewriting them.

The feature is enabled by default. There are two ways to restrict it:

```sh
# Keep zero-usage metatiles that still contain art; reuse only empty slots
... import LittlerootTown --reuse-empty-metatiles-only

# Disable the pre-import clearing/reuse policy entirely
... import LittlerootTown --no-reuse-unused-metatiles
```

With `--reuse-empty-metatiles-only`, Poryeasy still scans for zero-usage slots,
but only reuses those that are already fully transparent. Zero-usage metatiles
that still contain visible art are reserved and left untouched, so the
allocator cannot overwrite them. The summary reports them as `kept unused`.
This is separate from replace mode: metatiles freed by the edited map can still
be replaced.

In replace mode, freed slots that receive no new content are left empty when
this option is on, instead of being filled with duplicates of already-replaced
content. Slots still used by another map are kept intact so those maps do not
lose their art. With `--no-reuse-unused-metatiles`, the previous duplicate-fill
behavior is used, and no pre-import global scan/clearing is performed.

### Merging duplicate metatiles (default)

Before applying an import, Poryeasy checks the target map's primary and
secondary tilesets for **exact duplicate metatiles**. This runs for both replace
and add modes, so the freed slots are already available when new metatiles are
assigned. A duplicate means:

- the same 8x8 entries, including palette banks and flips;
- exactly the same metatile attributes/behaviour.

For each duplicate group, Poryeasy:

1. chooses a canonical ID: a `metatile_labels.h` protected ID first, then a
   door/animated ID, then the lowest **used** ID;
2. rewrites every `map.bin` and `border.bin` reference to the duplicate in every
   layout that uses that tileset — including other maps;
3. clears the duplicate metatile slot so it becomes empty and can be reused by
   the current import.

When the same metatile exists in both the target primary and secondary tilesets,
Poryeasy checks every map that references either copy:

- if all those maps use the target secondary tileset, the secondary copy is kept,
  the primary copy is cleared, and references are rewritten to the secondary ID;
- if any map uses a different secondary tileset, the primary copy is kept, the
  secondary copy is cleared, and references are rewritten to the primary ID.

If a canonical ID would not be valid in every map that references the group (for
example a primary ID used with a different primary tileset), the group is skipped
instead of risking broken map blocks.

This is enabled by default. Disable it with:

```sh
... import LittlerootTown --no-dedupe-metatiles
```

Groups with several protected/critical IDs, or with different attributes, are
skipped and reported. The import summary shows deduplicated metatiles, rewritten
cells, affected layouts, and a separate dedupe backup path when files are
written. Duplicate detection is conservative: it does not merge metatiles that
only look similar.

An optional second pass can clear 8x8 tile graphics that are no longer
referenced by any metatile:

```sh
... import LittlerootTown --on-new add-secondary --on-remove clear --on-remove-tiles ask
```

Tiles that appear in `anim/` frames are protected from this cleanup. Tile slots
that may be animated are reported when edited, since the game can overwrite the
static frame at runtime.

Usage counts are based on map/border references, like Porymap's usage display.
Hard-coded references outside map data are not fully knowable, which is why
named metatile labels are protected and slots are cleared rather than renumbered.

## Backups

By default every modified file is copied to:

```
.poryeasy-backups/<UTC timestamp>/<original relative path>
```

Use `--no-backup` to disable this. The write itself is atomic per file
(`.ptmap-tmp` then replace).

## Porytiles-managed tilesets

Porytiles is not bundled with Poryeasy. If a tileset contains `porytiles_src/`,
Porytiles will recompile the binary assets from those source PNGs later, which
would overwrite this tool's direct edits. The import prints a warning in that
case. Install Porytiles separately and refresh its source assets after
importing with:

```sh
porytiles decompile-tileset <TilesetName> --no-verify-checksums
```

or let the tool do it:

```sh
... import LittlerootTown --on-new add-secondary --sync-porytiles
```

For stock tilesets that were never imported by Porytiles (the common case),
there is no source folder and no extra step is needed.

## Notes and limitations

* After importing, reload the tileset/map in Porymap.
* A primary metatile may only reference primary tile slots; a secondary
  metatile may reference primary and its own secondary slots. Option 4 is
  disabled when this cannot be satisfied.
* If the primary tileset has no free tile slots (common for `gTileset_General`),
  use `add-secondary` or `replace`.
* Maps whose layout has no secondary tileset are not supported yet.
* Resizing a shared layout affects every map using it; event coordinates are
  shifted for those maps when the origin moves, but scripts, connections and
  hard-coded coordinates still need review.
* Removing a metatile clears its slot but deliberately does not renumber
  later metatiles.
* `preview.png`, `animated_metatiles.png` and `palettes.png` are only for
  humans; map import ignores `preview.png` and uses the animation/palette
  manifests explicitly.
* Newly created animated metatiles and doors are added to the sheet manifest,
  but Poryeasy does not paint them into the map automatically. Paint the new
  metatile where you want it; that is a map-design decision.

## Tests

The integration tests use a real pokeemerald-expansion checkout (sibling of the
Poryeasy repository by default, or set `PORYTILES_TEST_PROJECT`):

```sh
python -m unittest discover -s tests -v
```

They cover export, no-op round-trip, auto-remap, adding secondary metatiles and
tiles, global tile edits, clearing unused metatiles, interactive prompts, palette
usage/edit round-trips, smart/replace palette policies for map and animated art,
per-map tileset variants and variant propagation, and synthetic triple-layer,
1x2/2x2 door, new-door, new-door-state, new-tileset-animation and extra-row
projects.
