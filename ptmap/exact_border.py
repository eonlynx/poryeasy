"""Exact byte-level sharing for the split Battle Frontier outside borders.

The engine renders a connected map's edge with the *current* map's secondary
tileset.  Matching ids alone is not enough: every metatile word must point at
the same tile and palette slot in both tilesets.  This module rewrites the
four split tilesets so every id used in a visible seam band has one
byte-identical entry in both halves, copying/reordering tile patterns and
palette banks when the canonical slot is occupied by unrelated content.

It is intentionally specific to the Battle Frontier outside maps, like
``ptmap.preserve``, because those are the maps that currently use the
four-way split.
"""

from __future__ import annotations

import json
import shutil
import struct
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .border import TilesetData, _render as _render_metatile, metatile_attr, metatile_entry
from .errors import MapEditorError

SIDES = {
    "WS": (
        "battle_frontier_outside_west_south",
        "LAYOUT_BATTLE_FRONTIER_OUTSIDE_WEST",
        56,
    ),
    "ES": (
        "battle_frontier_outside_east_south",
        "LAYOUT_BATTLE_FRONTIER_OUTSIDE_EAST",
        72,
    ),
    "WN": (
        "battle_frontier_outside_west_north",
        "LAYOUT_BATTLE_FRONTIER_OUTSIDE_WEST_NORTH",
        56,
    ),
    "EN": (
        "battle_frontier_outside_east_north",
        "LAYOUT_BATTLE_FRONTIER_OUTSIDE_EAST_NORTH",
        72,
    ),
}
PAIR_SPECS = (
    ("WS", "ES", "ew"),
    ("WN", "EN", "ew"),
    ("WS", "WN", "ns"),
    ("ES", "EN", "ns"),
)
CANON_PRIORITY = {"WS": 0, "EN": 1, "ES": 2, "WN": 3}
# src/fieldmap.c: west connections copy MAP_OFFSET (7) columns, east
# connections copy MAP_OFFSET + 1 (8) columns, north/south copy 7 rows.
EW_WEST_COLS = 7
EW_EAST_COLS = 8
NS_ROWS = 8
# Animated secondary tile slots.  The fountain callback writes absolute tiles
# 512..519, i.e. secondary tile indices 0..7 of West South; the flag callback
# writes 218..223 in every split tileset.
ANIMATED = {
    "WS": set(range(0, 8)) | set(range(218, 224)),
    "ES": set(range(218, 224)),
    "WN": set(range(218, 224)),
    "EN": set(range(218, 224)),
}


@dataclass
class ExactBorderResult:
    shared_ids: int
    paired: dict[str, int]
    skipped: list[tuple[int, str, str, str]]
    entries_changed: int
    tiles_added: int
    palette_copies: int
    palette_rebanks: int
    seam_mismatches: dict[str, int]
    visible_mismatches: dict[str, int]
    map_changes: int
    backup_dir: Path | None


def _load_side(project, side: str) -> TilesetData:
    folder = SIDES[side][0]
    return TilesetData.load(
        folder, project.root / "data/tilesets/secondary" / folder
    )


def _render_side(data, primary, side: str, mid: int) -> bytes:
    return _render_metatile(
        metatile_entry(data[side].meta, mid),
        data[side].tiles,
        data[side].palettes,
        primary,
    )


def _band_ids(side: str, kind: str, blocks: dict[str, list[int]]) -> set[int]:
    width = SIDES[side][2]
    source = blocks[side]
    ids: set[int] = set()
    if kind == "ew":
        xs = (
            range(width - EW_WEST_COLS, width)
            if side in ("WS", "WN")
            else range(EW_EAST_COLS)
        )
        for y in range(36):
            for x in xs:
                ids.add(source[y * width + x] & 0x3FF)
    else:
        ys = range(NS_ROWS) if side in ("WS", "ES") else range(36 - NS_ROWS, 36)
        for y in ys:
            for x in range(width):
                ids.add(source[y * width + x] & 0x3FF)
    return {mid for mid in ids if mid >= 512}


def sync_exact_borders(
    project,
    *,
    dry_run: bool = False,
    backup: bool = True,
) -> ExactBorderResult:
    """Synchronise every visible Battle Frontier outside border id.

    Raises MapEditorError if the seam cannot be made exactly identical or if
    the rewrite would change any map block's appearance.
    """

    primary = TilesetData.load(
        "gTileset_General", project.root / "data/tilesets/primary/general"
    )
    data = {side: _load_side(project, side) for side in SIDES}
    blocks = {
        side: project.read_blocks(project.layouts[SIDES[side][1]])
        for side in SIDES
    }
    used = {
        side: {word & 0x3FF for word in blocks[side] if (word & 0x3FF) >= 512}
        for side in SIDES
    }

    bands = {(a, kind): _band_ids(a, kind, blocks) for a, b, kind in PAIR_SPECS}
    bands.update(
        {(b, kind): _band_ids(b, kind, blocks) for a, b, kind in PAIR_SPECS}
    )

    # Edges per id.  An id is skipped only when one side uses it outside the
    # seam for unrelated map content, which cannot be shared without moving
    # that content.  In the current maps that is only the West-only x=47
    # column, which East never displays.
    hits: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
    skipped: list[tuple[int, str, str, str]] = []
    for a, b, kind in PAIR_SPECS:
        union = bands[(a, kind)] | bands[(b, kind)]
        for mid in sorted(union):
            in_a = mid in bands[(a, kind)]
            in_b = mid in bands[(b, kind)]
            key_side = a if in_a else b
            key_render = _render_side(data, primary, key_side, mid)
            blocked = None
            for side in (a, b):
                if side == key_side:
                    continue
                if mid in used[side] and mid not in bands[(side, kind)]:
                    if _render_side(data, primary, side, mid) != key_render:
                        blocked = side
                        break
            if blocked is not None:
                skipped.append((mid, a, b, blocked))
                continue
            hits[mid].append((a, b, kind))

    # Independent seam components.  The two north seams are separate unless
    # an east/west seam also uses the same id (in which case they merge).
    components: dict[int, list[frozenset[str]]] = {}
    for mid, edges in hits.items():
        parent = {side: side for side in SIDES}

        def find(side: str) -> str:
            while parent[side] != side:
                parent[side] = parent[parent[side]]
                side = parent[side]
            return side

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for a, b, _kind in edges:
            union(a, b)
        groups: dict[str, set[str]] = defaultdict(set)
        for a, b, _kind in edges:
            groups[find(a)].update((a, b))
        components[mid] = [frozenset(group) for group in groups.values()]

    changed_ids: dict[str, set[int]] = {side: set() for side in SIDES}
    for mid, comps in components.items():
        for comp in comps:
            for side in comp:
                changed_ids[side].add(mid)

    def pair_kinds(mid: int, side: str) -> list[str]:
        return [kind for a, b, kind in hits[mid] if side in (a, b)]

    def choose_canonical(mid: int, comp: frozenset[str]) -> str:
        in_band = {
            side
            for side in comp
            if any(mid in bands[(side, kind)] for kind in pair_kinds(mid, side))
        }
        candidates = in_band or comp
        return sorted(candidates, key=lambda side: CANON_PRIORITY[side])[0]

    units = []
    for mid, comps in components.items():
        for comp in comps:
            units.append((mid, comp, choose_canonical(mid, comp)))
    units.sort(key=lambda unit: (CANON_PRIORITY[unit[2]], unit[0], sorted(unit[1])))

    canonical_tiles: dict[str, set[int]] = {side: set() for side in SIDES}
    for mid, comp, canonical in units:
        for word in metatile_entry(data[canonical].meta, mid):
            tile = word & 0x3FF
            if tile >= 512:
                canonical_tiles[canonical].add(tile - 512)

    protected_tiles: dict[str, set[int]] = {}
    protected_palettes: dict[str, set[int]] = {}
    for side in SIDES:
        tiles: set[int] = set(ANIMATED[side]) | canonical_tiles[side]
        palettes: set[int] = set()
        for mid in used[side]:
            if mid in changed_ids[side]:
                continue
            for word in metatile_entry(data[side].meta, mid):
                tile = word & 0x3FF
                bank = (word >> 12) & 0xF
                if tile >= 512:
                    tiles.add(tile - 512)
                if bank >= 6:
                    palettes.add(bank)
        protected_tiles[side] = tiles
        protected_palettes[side] = palettes

    meta = {side: bytearray(data[side].meta) for side in SIDES}
    attrs = {side: bytearray(data[side].attrs) for side in SIDES}
    tiles = {side: list(data[side].tiles) for side in SIDES}
    palettes = {side: [tuple(p) for p in data[side].palettes] for side in SIDES}

    claimed_tiles = {side: set() for side in SIDES}
    claimed_palettes = {side: set() for side in SIDES}
    pattern_slot: dict[tuple[frozenset[str], bytes], int] = {}
    palette_slot: dict[tuple[frozenset[str], tuple[str, ...]], int] = {}

    entries_changed = 0
    tiles_added = 0
    palette_copies = 0
    palette_rebanks = 0
    failed: list[str] = []

    def free_tile(sides: frozenset[str], pattern: bytes) -> int | None:
        key = (sides, pattern)
        if key in pattern_slot:
            return pattern_slot[key]
        for idx in range(512):
            ok = True
            for side in sides:
                if idx in protected_tiles[side] or idx in claimed_tiles[side]:
                    if tiles[side][idx] != pattern:
                        ok = False
                        break
            if ok:
                return idx
        return None

    def claim_tile(sides: frozenset[str], idx: int, pattern: bytes) -> None:
        for side in sides:
            if tiles[side][idx] != pattern:
                tiles[side][idx] = pattern
            claimed_tiles[side].add(idx)

    def palette_free(
        sides: frozenset[str], bank: int, palette: tuple[str, ...]
    ) -> bool:
        for side in sides:
            if bank in protected_palettes[side] or bank in claimed_palettes[side]:
                if palettes[side][bank] != palette:
                    return False
        return True

    def choose_palette(
        sides: frozenset[str],
        original_bank: int,
        palette: tuple[str, ...],
    ) -> int | None:
        nonlocal palette_copies
        key = (sides, palette)
        if key in palette_slot and palette_free(sides, palette_slot[key], palette):
            return palette_slot[key]
        if original_bank >= 6 and palette_free(sides, original_bank, palette):
            for side in sides:
                if palettes[side][original_bank] != palette:
                    palettes[side][original_bank] = palette
                    palette_copies += 1
                claimed_palettes[side].add(original_bank)
            palette_slot[key] = original_bank
            return original_bank
        for bank in range(6, 13):
            if palette_free(sides, bank, palette):
                for side in sides:
                    palettes[side][bank] = palette
                    claimed_palettes[side].add(bank)
                    palette_copies += 1
                palette_slot[key] = bank
                return bank
        return None

    for mid, sides, canonical in units:
        words = list(metatile_entry(data[canonical].meta, mid))
        for n, word in enumerate(words):
            tile = word & 0x3FF
            if tile < 512:
                continue
            idx = tile - 512
            pattern = data[canonical].tiles[idx]
            if (sides, pattern) in pattern_slot:
                new_idx = pattern_slot[(sides, pattern)]
            elif all(
                tiles[side][idx] == pattern
                or (idx not in protected_tiles[side] and idx not in claimed_tiles[side])
                for side in sides
            ):
                new_idx = idx
            else:
                new_idx = free_tile(sides, pattern)
                if new_idx is None:
                    failed.append(f"0x{mid:03X}: no common free tile for pattern")
                    continue
                tiles_added += 1
            claim_tile(sides, new_idx, pattern)
            pattern_slot[(sides, pattern)] = new_idx
            if new_idx != idx:
                words[n] = (word & ~0x3FF) | (512 + new_idx)

            bank = (words[n] >> 12) & 0xF
            if bank >= 6:
                palette = data[canonical].palettes[bank]
                chosen = choose_palette(sides, bank, palette)
                if chosen is None:
                    failed.append(f"0x{mid:03X}: no common free palette bank")
                    continue
                if chosen != bank:
                    words[n] = (words[n] & ~0xF000) | (chosen << 12)
                    palette_rebanks += 1

        for side in sides:
            off = (mid - 512) * 16
            packed = struct.pack("<8H", *words)
            if bytes(meta[side][off : off + 16]) != packed:
                entries_changed += 1
            meta[side][off : off + 16] = packed
            aoff = (mid - 512) * 2
            attrs[side][aoff : aoff + 2] = struct.pack(
                "<H", metatile_attr(data[canonical].attrs, mid)
            )

    if failed:
        raise MapEditorError(
            "Exact border sync failed:\n  " + "\n  ".join(failed[:20])
        )

    def working_render(side: str, mid: int) -> bytes:
        return _render_metatile(
            metatile_entry(bytes(meta[side]), mid),
            tiles[side],
            palettes[side],
            primary,
        )

    map_changes = 0
    for side in SIDES:
        seen = set()
        for word in blocks[side]:
            mid = word & 0x3FF
            if mid < 512 or mid in seen:
                continue
            seen.add(mid)
            if working_render(side, mid) != _render_side(data, primary, side, mid):
                map_changes += 1
    if map_changes:
        raise MapEditorError(
            f"Exact border sync would change {map_changes} map metatile appearance(s)"
        )

    seam_mismatches: dict[str, int] = {}
    for a, b, kind in PAIR_SPECS:
        union = bands[(a, kind)] | bands[(b, kind)]
        diffs = 0
        for mid in sorted(union):
            comps = components.get(mid, [])
            if not any(a in comp and b in comp for comp in comps):
                continue
            ea = struct.unpack(
                "<8H", bytes(meta[a])[(mid - 512) * 16 : (mid - 512) * 16 + 16]
            )
            eb = struct.unpack(
                "<8H", bytes(meta[b])[(mid - 512) * 16 : (mid - 512) * 16 + 16]
            )
            if ea != eb:
                diffs += 1
        seam_mismatches[f"{a}/{b} {kind}"] = diffs

    visible_mismatches: dict[str, int] = {}
    for a, b, kind in PAIR_SPECS:
        width_a, width_b = SIDES[a][2], SIDES[b][2]
        if kind == "ew":
            ids_a = {
                blocks[a][y * width_a + x] & 0x3FF
                for y in range(36)
                for x in range(width_a - EW_WEST_COLS, width_a)
            }
            ids_b = {
                blocks[b][y * width_b + x] & 0x3FF
                for y in range(36)
                for x in range(EW_EAST_COLS)
            }
        else:
            ids_a = {
                blocks[a][y * width_a + x] & 0x3FF
                for y in range(NS_ROWS)
                for x in range(width_a)
            }
            ids_b = {
                blocks[b][y * width_b + x] & 0x3FF
                for y in range(36 - NS_ROWS, 36)
                for x in range(width_b)
            }
        union = {mid for mid in ids_a | ids_b if mid >= 512}
        diffs = 0
        for mid in sorted(union):
            ea = struct.unpack(
                "<8H", bytes(meta[a])[(mid - 512) * 16 : (mid - 512) * 16 + 16]
            )
            eb = struct.unpack(
                "<8H", bytes(meta[b])[(mid - 512) * 16 : (mid - 512) * 16 + 16]
            )
            if ea != eb:
                diffs += 1
        visible_mismatches[f"{a}/{b} {kind}"] = diffs

    bad = sum(seam_mismatches.values()) + sum(visible_mismatches.values())
    if bad:
        raise MapEditorError(
            "Exact border verification failed: "
            + ", ".join(
                f"{name}={value}"
                for name, value in {**seam_mismatches, **visible_mismatches}.items()
                if value
            )
        )

    paired: dict[str, int] = {}
    for a, b, kind in PAIR_SPECS:
        union = bands[(a, kind)] | bands[(b, kind)]
        paired[f"{a}/{b} {kind}"] = sum(
            1
            for mid, comps in components.items()
            if mid in union and any(a in comp and b in comp for comp in comps)
        )

    backup_dir = None
    if not dry_run:
        if backup:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_dir = (
                project.root / ".poryeasy-backups" / f"{stamp}-exact-border"
            )
            for side, (folder, _layout, _width) in SIDES.items():
                src = project.root / "data/tilesets/secondary" / folder
                dst = backup_dir / src.relative_to(project.root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    shutil.copytree(src, dst)

        for side, (folder, _layout, _width) in SIDES.items():
            folder_path = project.root / "data/tilesets/secondary" / folder
            if bytes(meta[side]) != data[side].meta:
                (folder_path / "metatiles.bin").write_bytes(bytes(meta[side]))
            if bytes(attrs[side]) != data[side].attrs:
                (folder_path / "metatile_attributes.bin").write_bytes(bytes(attrs[side]))

            if any(
                tiles[side][idx] != data[side].tiles[idx] for idx in range(512)
            ):
                image = Image.open(folder_path / "tiles.png").convert("P")
                image_palette = image.getpalette()
                arr = np.array(image)
                tw = arr.shape[1] // 8
                out = arr.copy()
                for idx in range(512):
                    ty, tx = divmod(idx, tw)
                    out[ty * 8 : ty * 8 + 8, tx * 8 : tx * 8 + 8] = np.frombuffer(
                        tiles[side][idx], dtype=np.uint8
                    ).reshape(8, 8)
                new_image = Image.fromarray(out, mode="P")
                if image_palette:
                    new_image.putpalette(image_palette)
                new_image.save(folder_path / "tiles.png")

            for bank in range(16):
                if tuple(palettes[side][bank]) != tuple(data[side].palettes[bank]):
                    (folder_path / "palettes" / f"{bank:02d}.pal").write_text(
                        "\n".join(
                            ["JASC-PAL", "0100", "16", *palettes[side][bank]]
                        )
                        + "\n"
                    )

    return ExactBorderResult(
        shared_ids=len(components),
        paired=paired,
        skipped=skipped,
        entries_changed=entries_changed,
        tiles_added=tiles_added,
        palette_copies=palette_copies,
        palette_rebanks=palette_rebanks,
        seam_mismatches=seam_mismatches,
        visible_mismatches=visible_mismatches,
        map_changes=map_changes,
        backup_dir=backup_dir,
    )
