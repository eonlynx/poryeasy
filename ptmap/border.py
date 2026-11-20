"""Shared border metatiles for maps split into north/south halves.

The GBA draws blocks from a connected map with the *current* map's secondary
tileset.  A split map therefore has to define every border-block metatile in
both halves at the same ID, with the same visual result.  This module adds those
shared metatiles without moving or rewriting any non-border metatile, tile or
palette.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .errors import MapEditorError
from .project import Project, Target, write_bytes_atomic, write_text_atomic

METATILE_ENTRY = 16
ATTR_ENTRY = 2
DEFAULT_BAND = 9
DEFAULT_TILE_LIMIT = 508


@dataclass(frozen=True)
class VariantSpec:
    key: str
    layout_id: str
    map_id: str
    width: int
    height: int
    side: str  # north | south
    horizontal: str  # west | east
    tileset_name: str
    base_name: str
    directory: Path
    base_dir: Path
    map_json: Path
    layout_dir: Path
    reserved_tiles: set[int] = field(default_factory=set)


class _BorderCapacityError(MapEditorError):
    pass


@dataclass
class BorderResult:
    variants: list[str] = field(default_factory=list)
    band_used: int = DEFAULT_BAND
    canonical_metatiles: int = 0
    reused_ids: int = 0
    new_ids: int = 0
    tiles_added: dict[str, int] = field(default_factory=dict)
    palettes_added: dict[str, int] = field(default_factory=dict)
    band_blocks: int = 0
    skipped_animated: int = 0
    warnings: list[str] = field(default_factory=list)
    backup_dir: Path | None = None
    changed: bool = False


@dataclass
class TilesetData:
    name: str
    directory: Path
    meta: bytes
    attrs: bytes
    tiles: list[bytes]
    palettes: list[tuple[str, ...]]

    @classmethod
    def load(cls, name: str, directory: Path) -> "TilesetData":
        try:
            meta = (directory / "metatiles.bin").read_bytes()
            attrs = (directory / "metatile_attributes.bin").read_bytes()
            image = np.array(Image.open(directory / "tiles.png").convert("P"))
        except OSError as exc:
            raise MapEditorError(f"Could not read tileset {name} at {directory}") from exc
        tw = image.shape[1] // 8
        tiles = [
            bytes(image[ty * 8 : ty * 8 + 8, tx * 8 : tx * 8 + 8].tobytes())
            for ty in range(image.shape[0] // 8)
            for tx in range(tw)
        ]
        if len(tiles) < 512:
            tiles.extend([bytes(64)] * (512 - len(tiles)))
        palettes = []
        for i in range(16):
            path = directory / "palettes" / f"{i:02d}.pal"
            lines = path.read_text().splitlines()
            palettes.append(tuple(lines[3:19]))
        return cls(name, directory, meta, attrs, tiles, palettes)


def metatile_entry(meta: bytes, mid: int) -> tuple[int, ...]:
    off = (mid - 512) * METATILE_ENTRY
    return struct.unpack("<8H", meta[off : off + METATILE_ENTRY])


def metatile_attr(attrs: bytes, mid: int) -> int:
    off = (mid - 512) * ATTR_ENTRY
    return struct.unpack("<H", attrs[off : off + ATTR_ENTRY])[0]


def _render(
    words: tuple[int, ...],
    tiles: list[bytes],
    palettes: list[tuple[str, ...]],
    primary: TilesetData,
) -> bytes:
    """Render both metatile layers (bottom 4 tiles, top 4 tiles)."""
    img = np.zeros((16, 16, 4), dtype=np.uint8)
    for layer in range(2):
        for n, word in enumerate(words[layer * 4 : layer * 4 + 4]):
            tile_id = word & 0x3FF
            if not tile_id:
                continue
            palette_id = (word >> 12) & 0xF
            pattern = tiles[tile_id - 512] if tile_id >= 512 else primary.tiles[tile_id]
            # Palette banks 0-5 are loaded from the primary tileset and
            # banks 6-12 from the secondary tileset, independently of where
            # the 8x8 tile pattern itself comes from.
            palette = (
                primary.palettes[palette_id]
                if palette_id < 6
                else palettes[palette_id]
            )
            colors = [tuple(map(int, line.split())) for line in palette]
            tile = np.zeros((8, 8, 4), dtype=np.uint8)
            for y in range(8):
                for x in range(8):
                    idx = pattern[y * 8 + x]
                    r, g, b = colors[idx]
                    tile[y, x] = (r, g, b, 0 if idx == 0 else 255)
            if (word >> 10) & 1:
                tile = tile[:, ::-1]
            if (word >> 11) & 1:
                tile = tile[::-1, :]
            y0 = (n // 2) * 8
            x0 = (n % 2) * 8
            if layer == 0:
                img[y0 : y0 + 8, x0 : x0 + 8] = tile
            else:
                target = img[y0 : y0 + 8, x0 : x0 + 8]
                mask = tile[:, :, 3] > 0
                target[mask] = tile[mask]
    return img.tobytes()


def _base_index(base: TilesetData) -> dict[tuple[bytes, int], int]:
    index = {}
    for mid in range(512, 1024):
        words = metatile_entry(base.meta, mid)
        if not any(words):
            continue
        key = (struct.pack("<8H", *words), metatile_attr(base.attrs, mid))
        index.setdefault(key, mid)
    return index


def _tileset_animation_tiles(project: Project, tileset_name: str) -> set[int]:
    """Secondary tile slots written by this tileset's animation callback."""
    fallback = set(range(1, 9)) | set(range(218, 224))
    headers_path = project.root / "src" / "data" / "tilesets" / "headers.h"
    anims_path = project.root / "src" / "tileset_anims.c"
    if not headers_path.is_file() or not anims_path.is_file():
        return fallback
    headers = headers_path.read_text(encoding="utf-8", errors="replace")
    anims = anims_path.read_text(encoding="utf-8", errors="replace")
    start = headers.find(f"const struct Tileset {tileset_name}")
    if start < 0:
        return fallback
    block = headers[start : headers.find("};", start) + 2]
    match = re.search(r"\.callback\s*=\s*(\w+)", block)
    if not match:
        return set()
    callback = match.group(1)
    init = re.search(
        rf"void {re.escape(callback)}\(void\)\s*\{{(.*?)\n\}}", anims, re.S
    )
    body = init.group(1) if init else ""
    actual = callback
    setter = re.search(r"sSecondaryTilesetAnimCallback\s*=\s*(\w+)", body)
    if setter:
        actual = setter.group(1)
    timer = re.search(
        rf"static void {re.escape(actual)}\(u16 timer\)\s*\{{(.*?)\n\}}", anims, re.S
    )
    timer_body = timer.group(1) if timer else ""
    queues = set(re.findall(r"(QueueAnimTiles_\w+)\(", timer_body))
    reserved: set[int] = set()
    dest_pattern = re.compile(
        r"BG_VRAM\s*\+\s*TILE_OFFSET_4BPP\(\s*"
        r"(?:(NUM_TILES_IN_PRIMARY)\s*\+\s*)?(\d+)\s*\)\s*\)\s*,\s*"
        r"(\d+)\s*\*\s*TILE_SIZE_4BPP"
    )
    for queue in queues:
        qfunc = re.search(
            rf"static void {re.escape(queue)}\(u16 timer\)\s*\{{(.*?)\n\}}",
            anims,
            re.S,
        )
        if not qfunc:
            continue
        for dest in dest_pattern.finditer(qfunc.group(1)):
            secondary = dest.group(1) is not None
            number = int(dest.group(2))
            count = int(dest.group(3))
            local = number if secondary else number - 512
            if 0 <= local < 512:
                reserved.update(range(local, min(512, local + count)))
    return reserved


def discover_variants(project: Project, target: Target) -> list[VariantSpec]:
    if target.map is None:
        maps = project.maps_using_layout(target.layout.id)
        if len(maps) != 1:
            raise MapEditorError(
                "share-border needs a concrete map (pass the map symbol, not a layout)."
            )
        base_map = maps[0]
    else:
        base_map = target.map

    map_by_id = {entry.id: entry for entry in project._load_maps()}
    root_id = base_map.id
    if root_id.endswith("_NORTH"):
        root_id = root_id[: -len("_NORTH")]
    if root_id not in map_by_id or f"{root_id}_NORTH" not in map_by_id:
        raise MapEditorError(
            f"{base_map.name} is not part of a north/south split pair "
            f"(missing {root_id} or {root_id}_NORTH)."
        )

    # Include the horizontal neighbours of both halves, so the four corners
    # around a central split are treated as one group.
    wanted = {root_id, f"{root_id}_NORTH"}
    for map_id in list(wanted):
        raw = map_by_id[map_id].raw
        connections = raw.get("connections", [])
        if not isinstance(connections, list):
            connections = []
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            if connection.get("direction") in ("left", "right"):
                neighbour = connection.get("map")
                if neighbour in map_by_id:
                    wanted.add(neighbour)
                    if neighbour.endswith("_NORTH"):
                        wanted.add(neighbour[: -len("_NORTH")])
                    else:
                        wanted.add(f"{neighbour}_NORTH")

    variants: list[VariantSpec] = []
    seen_layouts: set[str] = set()
    for map_id in sorted(wanted):
        entry = map_by_id.get(map_id)
        if entry is None or entry.layout_id in seen_layouts:
            continue
        layout = project.layouts[entry.layout_id]
        secondary = layout.secondary_tileset
        base_name = secondary
        if base_name.endswith("South"):
            base_name = base_name[: -len("South")]
        elif base_name.endswith("North"):
            base_name = base_name[: -len("North")]
        else:
            raise MapEditorError(
                f"{entry.name} uses {secondary}, which does not look like a split variant."
            )
        raw_connections = entry.raw.get("connections", [])
        if not isinstance(raw_connections, list):
            raw_connections = []
        directions = {
            connection.get("direction")
            for connection in raw_connections
            if isinstance(connection, dict)
        }
        if "right" in directions:
            horizontal = "west"
        elif "left" in directions:
            horizontal = "east"
        else:
            raise MapEditorError(
                f"{entry.name} has no left/right connection; cannot locate the vertical seam."
            )
        side = "north" if map_id.endswith("_NORTH") else "south"
        variants.append(
            VariantSpec(
                key=entry.name,
                layout_id=layout.id,
                map_id=map_id,
                width=layout.width,
                height=layout.height,
                side=side,
                horizontal=horizontal,
                tileset_name=secondary,
                base_name=base_name,
                directory=project.tileset_dir(secondary, "secondary"),
                base_dir=project.tileset_dir(base_name, "secondary"),
                map_json=entry.folder / "map.json",
                layout_dir=project.layout_path(layout.blockdata_filepath).parent,
                reserved_tiles=_tileset_animation_tiles(project, secondary),
            )
        )
        seen_layouts.add(entry.layout_id)

    if len({v.layout_id for v in variants}) < 2:
        raise MapEditorError("Could not find both halves of the split map.")
    return variants


def _in_band(v: VariantSpec, x: int, y: int, band: int) -> bool:
    row_distance = y if v.side == "south" else (v.height - 1 - y)
    col_distance = (v.width - 1 - x) if v.horizontal == "west" else x
    return row_distance < band or col_distance < band


def _share_border_minimal(
    project: Project,
    variants: list[VariantSpec],
    *,
    band: int = DEFAULT_BAND,
    dry_run: bool = False,
    backup: bool = True,
) -> BorderResult:
    if band < 1:
        raise MapEditorError("--border-band must be at least 1.")
    project.use_layout_settings(project.layouts[variants[0].layout_id])

    primary = TilesetData.load("gTileset_General", project.tileset_dir("gTileset_General", "primary"))
    loaded: dict[str, TilesetData] = {}
    bases: dict[str, TilesetData] = {}
    blocks: dict[str, list[int]] = {}
    borders: dict[str, list[int]] = {}
    for spec in variants:
        loaded[spec.key] = TilesetData.load(spec.tileset_name, spec.directory)
        if spec.base_name not in bases:
            bases[spec.base_name] = TilesetData.load(spec.base_name, spec.base_dir)
        blocks[spec.key] = project.read_blocks(project.layouts[spec.layout_id])
        borders[spec.key] = list(project.read_border(project.layouts[spec.layout_id]))

    base_indexes = {name: _base_index(data) for name, data in bases.items()}
    pattern_sets = {
        key: set(data.tiles[: min(DEFAULT_TILE_LIMIT, len(data.tiles))])
        for key, data in loaded.items()
    }
    common_reserved = set.intersection(*(spec.reserved_tiles for spec in variants))
    skipped_animated = 0
    skipped_palettes: dict[str, set[int]] = defaultdict(set)
    canon: dict[tuple[bytes, int], tuple[str, int, TilesetData]] = {}
    band_assign: list[tuple[str, int, tuple[bytes, int]]] = []
    band_ids: dict[str, set[int]] = defaultdict(set)
    for spec in variants:
        data = loaded[spec.key]
        base = bases[spec.base_name]
        index = base_indexes[spec.base_name]
        variant_only_reserved = spec.reserved_tiles - common_reserved
        for y in range(spec.height):
            for x in range(spec.width):
                if not _in_band(spec, x, y, band):
                    continue
                idx = y * spec.width + x
                mid = blocks[spec.key][idx] & 0x3FF
                if mid < 512:
                    continue
                words = metatile_entry(data.meta, mid)
                animated_tiles = {
                    (word & 0x3FF) - 512
                    for word in words
                    if (word & 0x3FF) >= 512
                } & variant_only_reserved
                if animated_tiles:
                    patterns = {data.tiles[tile] for tile in animated_tiles}
                    if any(
                        not all(pattern in pattern_sets[other.key] for pattern in patterns)
                        for other in variants
                        if other.key != spec.key
                    ):
                        # e.g. the SilphCo fountain: only this half has the
                        # animation frames. Leave it untouched; it is not
                        # renderable in the other halves yet.
                        skipped_animated += 1
                        skipped_palettes[spec.key].update(
                            (word >> 12) & 0xF
                            for word in words
                            if (word & 0x3FF) >= 512
                            and ((word >> 12) & 0xF) >= 6
                        )
                        continue
                band_ids[spec.key].add(mid)
                key = (
                    struct.pack("<8H", *metatile_entry(data.meta, mid)),
                    metatile_attr(data.attrs, mid),
                )
                source: TilesetData
                smid: int
                if key in index:
                    source, smid = base, index[key]
                    base_sig = _render(
                        metatile_entry(source.meta, smid),
                        source.tiles,
                        source.palettes,
                        primary,
                    )
                    current_sig = _render(
                        metatile_entry(data.meta, mid),
                        data.tiles,
                        data.palettes,
                        primary,
                    )
                    if current_sig != base_sig:
                        source, smid = data, mid
                else:
                    source, smid = data, mid
                words = metatile_entry(source.meta, smid)
                at = metatile_attr(source.attrs, smid)
                sig = _render(words, source.tiles, source.palettes, primary)
                canonical_key = (sig, at)
                canon.setdefault(canonical_key, (spec.key, smid, source))
                band_assign.append((spec.key, idx, canonical_key))

    if not canon:
        return BorderResult(variants=[v.key for v in variants])

    # IDs that are already shared consistently can be reused (idempotence).
    key_current_ids: dict[tuple[bytes, int], dict[str, set[int]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for spec_key, idx, key in band_assign:
        spec = next(v for v in variants if v.key == spec_key)
        mid = blocks[spec_key][idx] & 0x3FF
        key_current_ids[key][spec_key].add(mid)

    outside_ids: dict[str, set[int]] = {}
    for spec in variants:
        used = set(borders[spec.key])
        for idx, word in enumerate(blocks[spec.key]):
            if not _in_band(spec, idx % spec.width, idx // spec.width, band):
                used.add(word)
        outside_ids[spec.key] = {w & 0x3FF for w in used if (w & 0x3FF) >= 512}

    def empty_mids(spec: VariantSpec) -> set[int]:
        data = loaded[spec.key]
        return {
            mid
            for mid in range(512, 1024)
            if not any(metatile_entry(data.meta, mid))
            and metatile_attr(data.attrs, mid) == 0
        }

    common_empty = set.intersection(*(empty_mids(spec) for spec in variants))
    shared_ids: dict[tuple[bytes, int], int] = {}
    reused_ids: set[int] = set()
    reused = 0
    for key in canon:
        ids_by_variant = key_current_ids[key]
        candidates = None
        for variant_key, ids in ids_by_variant.items():
            if len(ids) != 1:
                candidates = None
                break
            candidate = next(iter(ids))
            if candidates is None:
                candidates = {candidate}
            else:
                candidates &= {candidate}
        if candidates and len(candidates) == 1:
            candidate = next(iter(candidates))
            if (
                candidate not in reused_ids
                and all(candidate not in outside_ids[spec.key] for spec in variants)
            ):
                shared_ids[key] = candidate
                reused_ids.add(candidate)
                reused += 1
    available = sorted(common_empty - set(shared_ids.values()))
    new_ids = 0
    for key in canon:
        if key in shared_ids:
            continue
        if __import__("os").environ.get("PORYEASY_DEBUG_BORDER"):
            source_key, smid, source = canon[key]
            print(
                f"DEBUG new border id: source {source_key} mid 0x{smid:03X} "
                f"current ids {dict(key_current_ids[key])}"
            )
        if not available:
            raise MapEditorError(
                f"Need {len(canon)} shared border metatiles but the four tilesets "
                f"do not have enough common empty slots."
            )
        shared_ids[key] = available.pop(0)
        new_ids += 1

    canonical_patterns: set[bytes] = set()
    for _source_key, (_spec_key, smid, source) in canon.items():
        for word in metatile_entry(source.meta, smid):
            if (word & 0x3FF) >= 512:
                canonical_patterns.add(source.tiles[(word & 0x3FF) - 512])

    palette_contents: list[tuple[str, ...]] = []
    seen_contents: set[tuple[str, ...]] = set()
    for _source_key, (_spec_key, smid, source) in canon.items():
        for word in metatile_entry(source.meta, smid):
            if (word & 0x3FF) >= 512:
                palette = (word >> 12) & 0xF
                if palette >= 6:
                    content = source.palettes[palette]
                    if content not in seen_contents:
                        seen_contents.add(content)
                        palette_contents.append(content)

    outside_palette_usage: dict[str, set[int]] = {}
    for spec in variants:
        usage: set[int] = set()
        for mid in outside_ids[spec.key]:
            for word in metatile_entry(loaded[spec.key].meta, mid):
                if (word & 0x3FF) >= 512:
                    palette = (word >> 12) & 0xF
                    if palette >= 6:
                        usage.add(palette)
        outside_palette_usage[spec.key] = usage

    plans: dict[str, dict] = {}
    for spec in variants:
        data = loaded[spec.key]
        used_tiles = set(spec.reserved_tiles)
        all_used_mids = (
            {word & 0x3FF for word in blocks[spec.key]}
            | {word & 0x3FF for word in borders[spec.key]}
        ) - set(range(512))
        for mid in all_used_mids:
            for word in metatile_entry(data.meta, mid):
                if (word & 0x3FF) >= 512:
                    used_tiles.add((word & 0x3FF) - 512)
        free_tiles = [
            i for i in range(min(DEFAULT_TILE_LIMIT, len(data.tiles)))
            if i not in used_tiles
        ]

        # Do not overwrite palette banks used by a skipped, animated-only
        # border metatile (for example the SilphCo fountain).
        reserved_banks = set(skipped_palettes[spec.key])
        palette_map: dict[tuple[str, ...], int] = {}
        palette_writes: dict[int, tuple[str, ...]] = {}
        used_banks: set[int] = set()
        for content in palette_contents:
            existing = [
                b
                for b in range(6, 13)
                if b not in reserved_banks
                and b not in used_banks
                and data.palettes[b] == content
            ]
            if existing:
                bank = existing[0]
            else:
                candidates = [
                    b
                    for b in range(6, 13)
                    if b not in reserved_banks
                    and b not in used_banks
                    and b not in outside_palette_usage[spec.key]
                ]
                if not candidates:
                    raise MapEditorError(
                        f"{spec.tileset_name}: no free palette bank for shared border colours."
                    )
                bank = candidates[0]
                palette_writes[bank] = content
            palette_map[content] = bank
            used_banks.add(bank)

        existing_patterns: dict[bytes, int] = {}
        for i in range(min(DEFAULT_TILE_LIMIT, len(data.tiles))):
            existing_patterns.setdefault(data.tiles[i], i)
        pattern_map: dict[bytes, int] = {}
        missing: list[bytes] = []
        for pattern in canonical_patterns:
            if pattern in existing_patterns:
                pattern_map[pattern] = existing_patterns[pattern]
            else:
                missing.append(pattern)
        available_tiles = [i for i in free_tiles if i not in set(pattern_map.values())]
        if len(missing) > len(available_tiles):
            raise MapEditorError(
                f"{spec.tileset_name}: needs {len(missing)} new border tiles but only "
                f"{len(available_tiles)} free slots remain."
            )
        tile_writes: dict[int, bytes] = {}
        for pattern, idx in zip(sorted(missing), available_tiles):
            pattern_map[pattern] = idx
            tile_writes[idx] = pattern
        if __import__("os").environ.get("PORYEASY_DEBUG_BORDER") and palette_writes:
            print(
                f"DEBUG palette writes {spec.key}: "
                + ", ".join(f"bank {b} {content[0]!r}" for b, content in palette_writes.items())
            )
        plans[spec.key] = {
            "pattern_map": pattern_map,
            "tile_writes": tile_writes,
            "palette_map": palette_map,
            "palette_writes": palette_writes,
        }

    new_meta: dict[str, bytes] = {}
    new_attrs: dict[str, bytes] = {}
    new_tiles: dict[str, list[bytes]] = {}
    new_palettes: dict[str, list[tuple[str, ...]]] = {}
    for spec in variants:
        data = loaded[spec.key]
        plan = plans[spec.key]
        meta = bytearray(data.meta)
        attrs = bytearray(data.attrs)
        tiles = list(data.tiles)
        palettes = list(data.palettes)
        for idx, pattern in plan["tile_writes"].items():
            tiles[idx] = pattern
        for bank, content in plan["palette_writes"].items():
            palettes[bank] = content
        for canonical_key, (_spec_key, smid, source) in canon.items():
            sid = shared_ids[canonical_key]
            words = []
            for word in metatile_entry(source.meta, smid):
                tile = word & 0x3FF
                if not tile:
                    words.append(0)
                    continue
                palette = (word >> 12) & 0xF
                if tile >= 512:
                    new_tile = 512 + plan["pattern_map"][source.tiles[tile - 512]]
                    if palette >= 6:
                        palette = plan["palette_map"][source.palettes[palette]]
                else:
                    new_tile = tile
                    if palette >= 6:
                        palette = plan["palette_map"][source.palettes[palette]]
                words.append(
                    (new_tile & 0x3FF)
                    | (((word >> 10) & 0x3) << 10)
                    | ((palette & 0xF) << 12)
                )
            off = (sid - 512) * METATILE_ENTRY
            meta[off : off + METATILE_ENTRY] = struct.pack("<8H", *words)
            aoff = (sid - 512) * ATTR_ENTRY
            attrs[aoff : aoff + ATTR_ENTRY] = struct.pack("<H", canonical_key[1])
        new_meta[spec.key] = bytes(meta)
        new_attrs[spec.key] = bytes(attrs)
        new_tiles[spec.key] = tiles
        new_palettes[spec.key] = palettes

    new_blocks = {spec.key: list(blocks[spec.key]) for spec in variants}
    for spec_key, idx, canonical_key in band_assign:
        spec = next(v for v in variants if v.key == spec_key)
        word = new_blocks[spec_key][idx]
        new_blocks[spec_key][idx] = (word & ~0x3FF) | shared_ids[canonical_key]

    # Verification: every shared border metatile renders identically everywhere.
    for canonical_key in canon:
        expected = canonical_key[0]
        sid = shared_ids[canonical_key]
        for spec in variants:
            got = _render(
                metatile_entry(new_meta[spec.key], sid),
                new_tiles[spec.key],
                new_palettes[spec.key],
                primary,
            )
            if got != expected:
                raise MapEditorError(
                    f"Internal validation failed: shared metatile 0x{sid:03X} "
                    f"renders differently in {spec.tileset_name}."
                )
    # Verification: no non-border metatile changed appearance.
    for spec in variants:
        for mid in outside_ids[spec.key]:
            before = _render(
                metatile_entry(loaded[spec.key].meta, mid),
                loaded[spec.key].tiles,
                loaded[spec.key].palettes,
                primary,
            )
            after = _render(
                metatile_entry(new_meta[spec.key], mid),
                new_tiles[spec.key],
                new_palettes[spec.key],
                primary,
            )
            if before != after:
                raise MapEditorError(
                    f"Internal validation failed: metatile 0x{mid:03X} in "
                    f"{spec.tileset_name} changed outside the border band."
                )

    result = BorderResult(
        variants=[spec.key for spec in variants],
        canonical_metatiles=len(canon),
        reused_ids=reused,
        new_ids=new_ids,
        band_blocks=len(band_assign),
        skipped_animated=skipped_animated,
        changed=bool(new_ids) or any(
            plan["tile_writes"] or plan["palette_writes"]
            for plan in plans.values()
        ),
    )
    for spec in variants:
        result.tiles_added[spec.key] = len(plans[spec.key]["tile_writes"])
        result.palettes_added[spec.key] = len(plans[spec.key]["palette_writes"])

    if dry_run:
        return result

    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = project.root / ".poryeasy-backups" / f"{stamp}-share-border"
        for spec in variants:
            for src in (
                spec.directory,
                spec.layout_dir / "map.bin",
            ):
                if not src.exists():
                    continue
                rel = src.relative_to(project.root)
                dst = backup_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
        result.backup_dir = backup_dir

    for spec in variants:
        write_bytes_atomic(spec.directory / "metatiles.bin", new_meta[spec.key])
        write_bytes_atomic(spec.directory / "metatile_attributes.bin", new_attrs[spec.key])

        path = spec.directory / "tiles.png"
        image = Image.open(path).convert("P")
        palette = image.getpalette()
        arr = np.array(image)
        tw = arr.shape[1] // 8
        out = arr.copy()
        for idx, pattern in plans[spec.key]["tile_writes"].items():
            ty, tx = divmod(idx, tw)
            out[ty * 8 : ty * 8 + 8, tx * 8 : tx * 8 + 8] = np.frombuffer(
                pattern, dtype=np.uint8
            ).reshape(8, 8)
        new_image = Image.fromarray(out, mode="P")
        if palette:
            new_image.putpalette(palette)
        buffer = io.BytesIO()
        new_image.save(buffer, format="PNG")
        write_bytes_atomic(path, buffer.getvalue())

        for bank, content in plans[spec.key]["palette_writes"].items():
            lines = ["JASC-PAL", "0100", "16", *content]
            write_text_atomic(
                spec.directory / "palettes" / f"{bank:02d}.pal",
                "\n".join(lines) + "\n",
            )

        blocks_path = project.layout_path(project.layouts[spec.layout_id].blockdata_filepath)
        write_bytes_atomic(
            blocks_path,
            struct.pack(f"<{len(new_blocks[spec.key])}H", *new_blocks[spec.key]),
        )
    return result


def _appearance(data: TilesetData, mid: int, primary: TilesetData) -> tuple[bytes, int]:
    return (
        _render(metatile_entry(data.meta, mid), data.tiles, data.palettes, primary),
        metatile_attr(data.attrs, mid),
    )


def _share_border_rebuild(
    project: Project,
    variants: list[VariantSpec],
    *,
    band: int,
    dry_run: bool,
    backup: bool,
) -> BorderResult:
    project.use_layout_settings(project.layouts[variants[0].layout_id])
    primary = TilesetData.load(
        "gTileset_General", project.tileset_dir("gTileset_General", "primary")
    )
    loaded = {
        spec.key: TilesetData.load(spec.tileset_name, spec.directory)
        for spec in variants
    }
    bases: dict[str, TilesetData] = {}
    for spec in variants:
        if spec.base_name not in bases:
            bases[spec.base_name] = TilesetData.load(spec.base_name, spec.base_dir)
    blocks = {
        spec.key: project.read_blocks(project.layouts[spec.layout_id]) for spec in variants
    }
    borders = {
        spec.key: list(project.read_border(project.layouts[spec.layout_id]))
        for spec in variants
    }

    # Every unique appearance already used by a half must stay available.
    own_keys: dict[str, dict[tuple[bytes, int], tuple[TilesetData, int]]] = {}
    old_mid_key: dict[str, dict[int, tuple[bytes, int]]] = {}
    for spec in variants:
        data = loaded[spec.key]
        used_mids = {
            word & 0x3FF
            for word in blocks[spec.key] + borders[spec.key]
            if (word & 0x3FF) >= 512
        }
        keys: dict[tuple[bytes, int], tuple[TilesetData, int]] = {}
        mid_map: dict[int, tuple[bytes, int]] = {}
        for mid in sorted(used_mids):
            key = _appearance(data, mid, primary)
            keys.setdefault(key, (data, mid))
            mid_map[mid] = key
        own_keys[spec.key] = keys
        old_mid_key[spec.key] = mid_map

    # Shared border appearances (the only ones that need identical IDs).
    pattern_sets = {
        key: set(data.tiles[: min(DEFAULT_TILE_LIMIT, len(data.tiles))])
        for key, data in loaded.items()
    }
    common_reserved = set.intersection(*(spec.reserved_tiles for spec in variants))
    skipped_animated = 0
    skipped_palettes: dict[str, set[int]] = defaultdict(set)
    canon: dict[tuple[bytes, int], tuple[str, int, TilesetData]] = {}
    band_cells: list[tuple[str, int, tuple[bytes, int]]] = []
    base_indexes = {name: _base_index(data) for name, data in bases.items()}

    for spec in variants:
        data = loaded[spec.key]
        base = bases[spec.base_name]
        index = base_indexes[spec.base_name]
        variant_only_reserved = spec.reserved_tiles - common_reserved
        for y in range(spec.height):
            for x in range(spec.width):
                if not _in_band(spec, x, y, band):
                    continue
                idx = y * spec.width + x
                mid = blocks[spec.key][idx] & 0x3FF
                if mid < 512:
                    continue
                words = metatile_entry(data.meta, mid)
                animated_tiles = {
                    (word & 0x3FF) - 512
                    for word in words
                    if (word & 0x3FF) >= 512
                } & variant_only_reserved
                if animated_tiles:
                    patterns = {data.tiles[tile] for tile in animated_tiles}
                    if any(
                        not all(pattern in pattern_sets[other.key] for pattern in patterns)
                        for other in variants
                        if other.key != spec.key
                    ):
                        skipped_animated += 1
                        skipped_palettes[spec.key].update(
                            (word >> 12) & 0xF
                            for word in words
                            if (word & 0x3FF) >= 512
                            and ((word >> 12) & 0xF) >= 6
                        )
                        continue
                key = (
                    struct.pack("<8H", *words),
                    metatile_attr(data.attrs, mid),
                )
                source, smid = data, mid
                if key in index:
                    base_source = base
                    base_mid = index[key]
                    base_sig = _appearance(base_source, base_mid, primary)[0]
                    current_sig = _appearance(data, mid, primary)[0]
                    if base_sig == current_sig:
                        source, smid = base_source, base_mid
                canonical_key = _appearance(source, smid, primary)
                canon.setdefault(canonical_key, (spec.key, smid, source))
                band_cells.append((spec.key, idx, canonical_key))

    if not canon:
        return BorderResult(variants=[spec.key for spec in variants], band_used=band)

    canon_sources = {
        key: (source, smid) for key, (_owner, smid, source) in canon.items()
    }

    # Assign one common ID to each shared appearance, then fill each variant
    # with its own remaining appearances. The union must fit 512 per variant.
    canon_ids = {key: 512 + index for index, key in enumerate(sorted(canon))}
    variant_ids: dict[str, dict[tuple[bytes, int], int]] = {}
    for spec in variants:
        ids = dict(canon_ids)
        next_id = 512 + len(canon_ids)
        for key in sorted(set(own_keys[spec.key]) - set(canon)):
            if next_id >= 1024:
                raise _BorderCapacityError(
                    f"{spec.tileset_name} needs more than 512 metatiles with a "
                    f"{band}-tile border band."
                )
            ids[key] = next_id
            next_id += 1
        if len(ids) > 512:
            raise _BorderCapacityError(
                f"{spec.tileset_name} needs {len(ids)} metatiles with a "
                f"{band}-tile border band (limit 512)."
            )
        variant_ids[spec.key] = ids

    # Plan tiles and palettes for each variant.
    plans: dict[str, dict] = {}
    for spec in variants:
        data = loaded[spec.key]
        sources = dict(own_keys[spec.key])
        sources.update(canon_sources)

        forced_patterns: dict[bytes, int] = {}
        forced_palette_contents: dict[tuple[str, ...], int] = {}
        for _key, (source, smid) in own_keys[spec.key].items():
            for word in metatile_entry(source.meta, smid):
                tile = word & 0x3FF
                palette = (word >> 12) & 0xF
                if tile >= 512 and (tile - 512) in spec.reserved_tiles:
                    forced_patterns[source.tiles[tile - 512]] = tile - 512
                    if palette >= 6:
                        forced_palette_contents[source.palettes[palette]] = palette

        palette_contents: list[tuple[str, ...]] = []
        seen_contents: set[tuple[str, ...]] = set()
        for _key, (source, smid) in sources.items():
            for word in metatile_entry(source.meta, smid):
                palette = (word >> 12) & 0xF
                if palette >= 6:
                    content = source.palettes[palette]
                    if content not in seen_contents:
                        seen_contents.add(content)
                        palette_contents.append(content)

        palette_map = dict(forced_palette_contents)
        used_banks = set(palette_map.values())
        palette_writes: dict[int, tuple[str, ...]] = {}
        for content in palette_contents:
            if content in palette_map:
                continue
            existing = [
                bank
                for bank in range(6, 13)
                if bank not in used_banks and data.palettes[bank] == content
            ]
            if existing:
                bank = existing[0]
            else:
                free = [bank for bank in range(6, 13) if bank not in used_banks]
                if not free:
                    raise _BorderCapacityError(
                        f"{spec.tileset_name} needs more secondary palettes than "
                        f"the hardware allows."
                    )
                bank = free[0]
                palette_writes[bank] = content
            palette_map[content] = bank
            used_banks.add(bank)

        patterns: set[bytes] = set()
        for _key, (source, smid) in sources.items():
            for word in metatile_entry(source.meta, smid):
                if (word & 0x3FF) >= 512:
                    patterns.add(source.tiles[(word & 0x3FF) - 512])
        pattern_map = dict(forced_patterns)
        used_indices = set(forced_patterns.values()) | set(spec.reserved_tiles)
        available = [i for i in range(DEFAULT_TILE_LIMIT) if i not in used_indices]
        remaining = sorted(patterns - set(pattern_map))
        if len(remaining) > len(available):
            raise _BorderCapacityError(
                f"{spec.tileset_name} needs {len(remaining)} new border tiles but "
                f"only {len(available)} slots are free with a {band}-tile band."
            )
        for pattern, idx in zip(remaining, available):
            pattern_map[pattern] = idx
        plans[spec.key] = {
            "palette_map": palette_map,
            "palette_writes": palette_writes,
            "pattern_map": pattern_map,
        }

    def encode(source: TilesetData, smid: int, spec_key: str) -> list[int]:
        plan = plans[spec_key]
        result = []
        for word in metatile_entry(source.meta, smid):
            tile = word & 0x3FF
            if not tile:
                result.append(0)
                continue
            palette = (word >> 12) & 0xF
            if tile >= 512:
                new_tile = 512 + plan["pattern_map"][source.tiles[tile - 512]]
            else:
                new_tile = tile
            if palette >= 6:
                palette = plan["palette_map"][source.palettes[palette]]
            result.append(
                (new_tile & 0x3FF)
                | (((word >> 10) & 0x3) << 10)
                | ((palette & 0xF) << 12)
            )
        return result

    new_meta: dict[str, bytes] = {}
    new_attrs: dict[str, bytes] = {}
    new_tiles: dict[str, list[bytes]] = {}
    new_palettes: dict[str, list[tuple[str, ...]]] = {}
    new_blocks: dict[str, list[int]] = {}
    for spec in variants:
        data = loaded[spec.key]
        plan = plans[spec.key]
        meta = bytearray(512 * METATILE_ENTRY)
        attrs = bytearray(512 * ATTR_ENTRY)
        sources = dict(own_keys[spec.key])
        sources.update(canon_sources)
        for key, sid in variant_ids[spec.key].items():
            source, smid = sources[key]
            words = encode(source, smid, spec.key)
            off = (sid - 512) * METATILE_ENTRY
            meta[off : off + METATILE_ENTRY] = struct.pack("<8H", *words)
            aoff = (sid - 512) * ATTR_ENTRY
            attrs[aoff : aoff + ATTR_ENTRY] = struct.pack("<H", key[1])
        tiles = list(data.tiles)
        for pattern, idx in plan["pattern_map"].items():
            tiles[idx] = pattern
        palettes = list(data.palettes)
        for bank, content in plan["palette_writes"].items():
            palettes[bank] = content
        blocks_out = []
        for word in blocks[spec.key]:
            mid = word & 0x3FF
            if mid < 512:
                blocks_out.append(word)
            else:
                key = old_mid_key[spec.key][mid]
                blocks_out.append((word & ~0x3FF) | variant_ids[spec.key][key])
        border_out = []
        for word in borders[spec.key]:
            mid = word & 0x3FF
            if mid < 512:
                border_out.append(word)
            else:
                key = old_mid_key[spec.key][mid]
                border_out.append((word & ~0x3FF) | variant_ids[spec.key][key])
        new_meta[spec.key] = bytes(meta)
        new_attrs[spec.key] = bytes(attrs)
        new_tiles[spec.key] = tiles
        new_palettes[spec.key] = palettes
        new_blocks[spec.key] = blocks_out + border_out

    # Verification: every used metatile must keep its exact appearance.
    for spec in variants:
        border_len = len(borders[spec.key])
        for key, sid in variant_ids[spec.key].items():
            got = _render(
                metatile_entry(new_meta[spec.key], sid),
                new_tiles[spec.key],
                new_palettes[spec.key],
                primary,
            )
            if got != key[0]:
                raise MapEditorError(
                    f"Internal validation failed: metatile 0x{sid:03X} in "
                    f"{spec.tileset_name} changed appearance."
                )
    # Verification: shared IDs render identically in all four tilesets.
    for key in canon:
        sid = canon_ids[key]
        expected = key[0]
        for spec in variants:
            got = _render(
                metatile_entry(new_meta[spec.key], sid),
                new_tiles[spec.key],
                new_palettes[spec.key],
                primary,
            )
            if got != expected:
                raise MapEditorError(
                    f"Internal validation failed: shared metatile 0x{sid:03X} "
                    f"differs in {spec.tileset_name}."
                )

    result = BorderResult(
        variants=[spec.key for spec in variants],
        band_used=band,
        canonical_metatiles=len(canon),
        new_ids=len(canon_ids),
        reused_ids=0,
        band_blocks=len(band_cells),
        skipped_animated=skipped_animated,
        changed=True,
        warnings=(
            [f"border band reduced to {band} so both halves fit 512 metatiles."]
            if band != DEFAULT_BAND
            else []
        ),
    )
    for spec in variants:
        result.tiles_added[spec.key] = len(plans[spec.key]["pattern_map"])
        result.palettes_added[spec.key] = len(plans[spec.key]["palette_writes"])

    if dry_run:
        return result

    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = project.root / ".poryeasy-backups" / f"{stamp}-share-border"
        for spec in variants:
            for src in (spec.directory, spec.layout_dir / "map.bin"):
                if not src.exists():
                    continue
                rel = src.relative_to(project.root)
                dst = backup_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
        result.backup_dir = backup_dir

    for spec in variants:
        write_bytes_atomic(spec.directory / "metatiles.bin", new_meta[spec.key])
        write_bytes_atomic(
            spec.directory / "metatile_attributes.bin", new_attrs[spec.key]
        )
        path = spec.directory / "tiles.png"
        image = Image.open(path).convert("P")
        palette = image.getpalette()
        arr = np.array(image)
        tw = arr.shape[1] // 8
        out = arr.copy()
        for pattern, idx in plans[spec.key]["pattern_map"].items():
            ty, tx = divmod(idx, tw)
            if ty * 8 + 8 > out.shape[0]:
                continue
            out[ty * 8 : ty * 8 + 8, tx * 8 : tx * 8 + 8] = np.frombuffer(
                pattern, dtype=np.uint8
            ).reshape(8, 8)
        new_image = Image.fromarray(out, mode="P")
        if palette:
            new_image.putpalette(palette)
        buffer = io.BytesIO()
        new_image.save(buffer, format="PNG")
        write_bytes_atomic(path, buffer.getvalue())
        for bank, content in plans[spec.key]["palette_writes"].items():
            lines = ["JASC-PAL", "0100", "16", *content]
            write_text_atomic(
                spec.directory / "palettes" / f"{bank:02d}.pal",
                "\n".join(lines) + "\n",
            )
        block_count = len(blocks[spec.key])
        border_count = len(borders[spec.key])
        blocks_path = project.layout_path(
            project.layouts[spec.layout_id].blockdata_filepath
        )
        write_bytes_atomic(
            blocks_path,
            struct.pack(
                f"<{block_count}H", *new_blocks[spec.key][:block_count]
            ),
        )
        border_path = project.layout_path(
            project.layouts[spec.layout_id].border_filepath
        )
        if border_count:
            write_bytes_atomic(
                border_path,
                struct.pack(
                    f"<{border_count}H", *new_blocks[spec.key][block_count:]
                ),
            )
    return result


def share_border(
    project: Project,
    variants: list[VariantSpec],
    *,
    band: int = DEFAULT_BAND,
    dry_run: bool = False,
    backup: bool = True,
) -> BorderResult:
    """Share border metatiles, reducing the band automatically when needed."""
    candidates = [band]
    for fallback in (8, 6, 4, 2):
        if fallback < band and fallback not in candidates:
            candidates.append(fallback)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return _share_border_rebuild(
                project,
                variants,
                band=candidate,
                dry_run=dry_run,
                backup=backup,
            )
        except _BorderCapacityError as exc:
            last_error = exc
            continue
    raise MapEditorError(
        f"Could not share the border even with a 2-tile band: {last_error}"
    )
