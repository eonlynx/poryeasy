# Importing map layers from another decomp project

This guide describes the generic workflow for taking an exported map from one
decomp project and importing it into the matching map of another project.

## Placeholders

- `<poryeasy>`: path to this repository
- `<source-project>`: project that provides the edited layers
- `<target-project>`: project that receives the layers
- `<map>`: map name or symbol, for example a map folder name
- `<input-dir>`: directory that combines the source PNGs with the target JSONs

## 1. Export the source project

Run from `<source-project>`:

```sh
python <poryeasy>/porytiles_map.py export <map>
```

Export replaces existing layer images by default. Use `--no-overwrite` to
refuse.

## 2. Export the target project

Run from `<target-project>`:

```sh
python <poryeasy>/porytiles_map.py export <map>
```

This creates the target `map.json`, `palettes.json`, `palettes.png` and the
animation manifest that the import needs.

## 3. Build the input directory

Create `<input-dir>` and combine:

- the source `bottom.png`, `middle.png`, `top.png`;
- the source `animated_metatiles.png`, if the map has animations;
- the target `map.json`, `palettes.json`, `palettes.png`;
- an animation manifest compatible with the target project.

If the source and target animation sheets have different row layouts, translate
the manifest before importing. Copying animation PNGs against an unrelated
manifest will map rows to the wrong metatiles, doors or palettes.

## 4. Import into the target project

Run from `<target-project>`:

```sh
python <poryeasy>/porytiles_map.py import <map> --input <input-dir>
```

Current defaults:

- `--on-new replace`
- `--on-remove keep`
- `--on-extra-animation-row skip`
- `--palette-mode smart`
- `--palette-min-share 0.5` (new colours below this percentage of the map are ignored)
- `--tiny-edit-policy warn` (`warn`, `error`, or `allow`)
- `--tiny-edit-pixels 4`
- `--tile-match-tolerance 1` (tiny edits use the larger tiny-edit threshold)
- `--reuse-unused-metatiles` (on; disable with `--no-reuse-unused-metatiles`)
- `--reuse-empty-metatiles-only` (reuse only already-empty slots; keep zero-usage art)
- `--dedupe-metatiles` (on; disable with `--no-dedupe-metatiles`)

Use `--dry-run` to validate without writing files.

## 5. Re-export and review

```sh
python <poryeasy>/porytiles_map.py export <map>
```

The export directory contains `preview.png` for visual review. Also open the
map and tilesets in Porymap after importing.

## Notes

- Replace mode only frees a metatile when it has zero references in the edited
  map. Doors and critical animated metatiles are protected.
- Before importing, slots with zero usages across every readable map and border
  are cleared and reused first. Slots that are already fully transparent are
  free without being rewritten. Disable this with
  `--no-reuse-unused-metatiles`.
- `--reuse-empty-metatiles-only` preserves zero-usage metatiles that still
  contain visible art and only reuses already-empty slots; kept slots are
  reported as `kept unused`.
- Before an import, exact duplicate metatiles in the target primary and secondary
  tilesets are merged: references in every affected map/border are rewritten to a
  canonical ID and the duplicate slots are emptied, so the current import can
  reuse them. If the same metatile exists in both tilesets, all usages are
  checked: a shared target secondary keeps the secondary copy; differing
  secondaries keep the primary copy. Disable with `--no-dedupe-metatiles`.
- If replace mode has fewer new contents than freed slots, the remaining slots
  stay empty instead of being filled with duplicates. Slots still used by other
  maps are kept intact.
- New metatiles are allocated in row-major order of first appearance in the
  map, filling the lowest free slot first.
- `smart` palette mode counts real tileset pixels, keeps at least one colour
  per hue family, and distributes the remaining slots by usage.
- Palette influence now comes from the resulting map: unchanged cells carry the
  old colours' weight and edited cells carry the new colours' weight. New
  colours below `--palette-min-share` (default 0.5%) are ignored and mapped to
  the nearest existing colour, so stray pixels cannot rewrite palettes.
- Tiny edits of at most `--tiny-edit-pixels` pixels (default 4) are reported
  with their map coordinates. Use `--tiny-edit-policy error` to abort instead
  of importing them, or `allow` to suppress the warning. Tiny edits never
  influence palettes.
- Edited 8x8 tiles are matched against existing tiles with a small tolerance:
  `--tile-match-tolerance` (default 1) for normal edits, and
  `--tiny-edit-pixels` for tiny edits. A near-identical tile is reused instead
  of compiling a new one; validation accepts the harmonized difference. Use
  `--tile-match-tolerance 0` to disable fuzzy tile matching.
- Old tile indices are remapped to the closest colours in the new palettes.
- Metatiles that share an overwritten tile receive that tile's replacement
  palette bank, including metatiles outside the imported map.
- Always keep a backup or use version control before a large overhaul.
