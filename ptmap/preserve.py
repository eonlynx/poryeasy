"""Preserve-ID rebuild for the Battle Frontier East/West split.

This rebuilds the four split secondary tilesets from the original full East
and West bases, preserving original metatile/tile IDs and positions, and
placing imported content in free slots in map order.  It is intentionally
specific to the Battle Frontier outside maps, which are the first project
that needed the four-way split.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .border import TilesetData, metatile_entry, metatile_attr, _render
from .errors import MapEditorError

ROOT: Path | None = None
SEC: Path | None = None
PRIMARY: TilesetData | None = None
BAND = 8
FLAG_SLOTS = set(range(218, 224))


@dataclass
class Side:
    key: str
    layout: str
    folder: str
    base: str
    width: int
    height: int
    orig_width: int
    y_offset: int
    side: str
    horizontal: str


SIDES = {
    "WS": Side("WS", "BattleFrontier_OutsideWest", "battle_frontier_outside_west_south", "battle_frontier_outside_west", 56, 36, 56, 36, "south", "west"),
    "WN": Side("WN", "BattleFrontier_OutsideWest_North", "battle_frontier_outside_west_north", "battle_frontier_outside_west", 56, 36, 56, 0, "north", "west"),
    "ES": Side("ES", "BattleFrontier_OutsideEast", "battle_frontier_outside_east_south", "battle_frontier_outside_east", 72, 36, 72, 36, "south", "east"),
    "EN": Side("EN", "BattleFrontier_OutsideEast_North", "battle_frontier_outside_east_north", "battle_frontier_outside_east", 72, 36, 72, 0, "north", "east"),
}


def load(path: Path) -> TilesetData:
    return TilesetData.load(path.name, path)


def key_of(d: TilesetData, mid: int):
    return (_render(metatile_entry(d.meta, mid), d.tiles, d.palettes, PRIMARY), metatile_attr(d.attrs, mid))


def _git_blob(commit: str, path: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def orig_blocks(name: str, expected_words: int):
    """Return the pre-split map block data.

    Before the split is committed, HEAD still contains the full map.  After
    the split commit, reconstruct the source from the parent of the commit
    that added the ``_North`` layout (or, failing that, the most recent
    historical blob with the expected size).
    """
    path = f"data/layouts/{name}/map.bin"
    candidates: list[str] = ["HEAD"]
    north_path = f"data/layouts/{name}_North/map.bin"
    added = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "log",
            "--diff-filter=A",
            "--format=%H",
            "-1",
            "--",
            north_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if added:
        parent = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", f"{added}^"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if parent:
            candidates.append(parent)
    history = subprocess.run(
        ["git", "-C", str(ROOT), "log", "--format=%H", "--", path],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    candidates.extend(history)

    seen: set[str] = set()
    for commit in candidates:
        if commit in seen:
            continue
        seen.add(commit)
        data = _git_blob(commit, path)
        if data is not None and len(data) == expected_words * 2:
            return list(struct.unpack(f"<{expected_words}H", data))
    raise MapEditorError(
        f"Could not find the original {name}/map.bin in git history "
        f"({expected_words} blocks expected). Commit the pre-split map or "
        "run preserve-rebuild before splitting."
    )


def rebuild_preserve(project) -> None:
    global ROOT, SEC, PRIMARY
    ROOT = project.root
    SEC = ROOT / "data/tilesets/secondary"
    PRIMARY = TilesetData.load("gTileset_General", ROOT / "data/tilesets/primary/general")
    base = {k: load(SEC / s.base) for k, s in SIDES.items()}
    current = {k: load(SEC / s.folder) for k, s in SIDES.items()}
    layout_ids = {
        k: json.loads((ROOT / "data/maps" / s.layout / "map.json").read_text(encoding="utf-8"))["layout"]
        for k, s in SIDES.items()
    }
    blocks = {
        k: project.read_blocks(project.layouts[layout_ids[k]])
        for k, s in SIDES.items()
    }
    borders = {
        k: list(project.read_border(project.layouts[layout_ids[k]]))
        for k, s in SIDES.items()
    }
    orig_maps = {
        "west": orig_blocks(
            "BattleFrontier_OutsideWest",
            SIDES["WS"].orig_width * SIDES["WS"].height * 2,
        ),
        "east": orig_blocks(
            "BattleFrontier_OutsideEast",
            SIDES["ES"].orig_width * SIDES["ES"].height * 2,
        ),
    }

    # Original block IDs and used IDs per half.
    orig_id = {}
    orig_used_ids = {}
    for k, s in SIDES.items():
        full = orig_maps["west" if s.base.endswith("west") else "east"]
        ids = set()
        mapping = {}
        for y in range(s.height):
            for x in range(s.width):
                oid = full[(s.y_offset + y) * s.orig_width + x] & 0x3FF
                mapping[(x, y)] = oid
                if oid >= 512:
                    ids.add(oid)
        for word in borders[k]:
            oid = word & 0x3FF
            if oid >= 512:
                ids.add(oid)
        orig_id[k] = mapping
        orig_used_ids[k] = ids

    # Classify current blocks: original appearance or new content.
    new_info: dict[tuple[bytes, int], dict] = {}
    new_order: list[tuple[bytes, int]] = []
    remap_key = {}  # (side, x, y) -> (kind, target)
    for k, s in SIDES.items():
        cur = current[k]
        for y in range(s.height):
            for x in range(s.width):
                word = blocks[k][y * s.width + x]
                mid = word & 0x3FF
                if mid < 512:
                    continue
                ckey = key_of(cur, mid)
                oid = orig_id[k][(x, y)]
                if k != "WS":
                    # Only West South received imported content. Every other
                    # half keeps its original block IDs.
                    remap_key[(k, x, y)] = ("orig", oid)
                    continue
                if oid >= 512 and ckey == key_of(base[k], oid):
                    remap_key[(k, x, y)] = ("orig", oid)
                    continue
                remap_key[(k, x, y)] = ("new", ckey)
                info = new_info.setdefault(ckey, {"source": (k, mid), "coords": [], "sides": set()})
                info["coords"].append((k, x, y))
                info["sides"].add(k)
                if ckey not in new_order:
                    new_order.append(ckey)

    # Border categories for the new content (WS only in practice).
    border_ew = set()
    border_ns = set()
    fountain_keys = set()
    for k, s in SIDES.items():
        if k != "WS":
            continue
        for ckey, info in new_info.items():
            for side, x, y in info["coords"]:
                if side != "WS":
                    continue
                if x >= s.width - BAND:
                    border_ew.add(ckey)
                if y < BAND:
                    border_ns.add(ckey)
    for x, y in ((26, 8), (26, 9)):
        mid = blocks["WS"][y * 56 + x] & 0x3FF
        fountain_keys.add(key_of(current["WS"], mid))
    border_ns -= fountain_keys
    border_ew -= fountain_keys

    # Free metatile IDs per destination.
    free_ids = {
        k: set(range(512, 1024)) - orig_used_ids[k] for k in SIDES
    }
    # Original IDs used near the North/South seam must stay intact in the
    # other half's copy of the same base tileset.
    ns_ws = {orig_id["WS"][(x, y)] for y in range(BAND) for x in range(56)} - set(range(512))
    ns_wn = {orig_id["WN"][(x, y)] for y in range(36 - BAND, 36) for x in range(56)} - set(range(512))
    ns_es = {orig_id["ES"][(x, y)] for y in range(BAND) for x in range(72)} - set(range(512))
    ns_en = {orig_id["EN"][(x, y)] for y in range(36 - BAND, 36) for x in range(72)} - set(range(512))
    free_ids["WN"] -= ns_ws
    free_ids["WS"] -= ns_wn
    free_ids["EN"] -= ns_es
    free_ids["ES"] -= ns_en
    new_ids: dict[tuple[bytes, int], int] = {}

    def allocate(ckey, affected, preferred_run=None):
        pools = [free_ids[k] for k in affected]
        common = set.intersection(*pools)
        if not common:
            # Rule violation: allocate in source, not in neighbour.
            base_pool = free_ids["WS"]
            if not base_pool:
                raise SystemExit("No free metatile slots")
            chosen = min(base_pool)
            free_ids["WS"].discard(chosen)
            return chosen
        if preferred_run:
            for lo, hi in preferred_run:
                for candidate in range(lo, hi + 1):
                    if candidate in common:
                        for k in affected:
                            free_ids[k].discard(candidate)
                        return candidate
        chosen = min(common)
        for k in affected:
            free_ids[k].discard(chosen)
        return chosen

    # Preferred contiguous pools to keep new content grouped.
    def runs(pool):
        out = []
        start = None
        prev = None
        for value in sorted(pool):
            if start is None:
                start = prev = value
            elif value == prev + 1:
                prev = value
            else:
                out.append((start, prev))
                start = prev = value
        if start is not None:
            out.append((start, prev))
        return sorted(out, key=lambda r: -(r[1] - r[0]))

    ws_es_runs = runs(free_ids["WS"] & free_ids["ES"])
    ws_wn_runs = runs(free_ids["WS"] & free_ids["WN"])
    ws_runs = runs(free_ids["WS"])
    for ckey in new_order:
        installed = {k for k in SIDES if ckey in new_info and k in new_info[ckey]["sides"]}
        if ckey in fountain_keys:
            affected = ["WS"]
            pool = ws_runs
        elif ckey in border_ew:
            affected = ["WS", "ES"]
            pool = ws_es_runs
        elif ckey in border_ns:
            affected = ["WS", "WN"]
            pool = ws_wn_runs
        else:
            affected = ["WS"]
            pool = ws_runs
        new_ids[ckey] = allocate(ckey, affected, pool)

    print("new appearances", len(new_ids), "EW border", len(border_ew), "NS border", len(border_ns))
    print("new ID ranges", (hex(min(new_ids.values())), hex(max(new_ids.values()))))

    # New content destinations.
    dest_meta = {k: bytearray(base[k].meta) for k in SIDES}
    dest_attrs = {k: bytearray(base[k].attrs) for k in SIDES}
    dest_tiles = {k: list(base[k].tiles) for k in SIDES}
    dest_palettes = {k: list(base[k].palettes) for k in SIDES}

    # Original used tiles/banks per half, to avoid overwriting.
    used_tiles = {}
    used_banks = {}
    for k, s in SIDES.items():
        tiles = set()
        banks = set()
        for oid in orig_used_ids[k]:
            for word in metatile_entry(base[k].meta, oid):
                tile = word & 0x3FF
                p = (word >> 12) & 0xF
                if tile >= 512:
                    tiles.add(tile - 512)
                if p >= 6:
                    banks.add(p)
        used_tiles[k] = tiles
        used_banks[k] = banks
    # Preserve tile patterns/palettes of original IDs that the other half
    # needs at the North/South seam.
    for dest, ids, src in (("WS", ns_wn, "WN"), ("WN", ns_ws, "WS"), ("ES", ns_en, "EN"), ("EN", ns_es, "ES")):
        base_src = base[src]
        for oid in ids:
            for word in metatile_entry(base_src.meta, oid):
                tile = word & 0x3FF
                pbit = (word >> 12) & 0xF
                if tile >= 512:
                    used_tiles[dest].add(tile - 512)
                if pbit >= 6:
                    used_banks[dest].add(pbit)
    reserved_tiles = {k: set(FLAG_SLOTS) for k in SIDES}

    # Fountain needs a free contiguous 8-tile range in WS.
    free_ws_tiles = [i for i in range(512) if i not in used_tiles["WS"] and i not in reserved_tiles["WS"]]
    fountain_start = None
    for i in range(len(free_ws_tiles) - 7):
        if free_ws_tiles[i + 7] == free_ws_tiles[i] + 7:
            fountain_start = free_ws_tiles[i]
            break
    if fountain_start is None:
        raise SystemExit("No contiguous 8-tile range for fountain")
    fountain_slots = list(range(fountain_start, fountain_start + 8))
    print("fountain tile slots", fountain_slots)

    # Per-destination tile allocation.
    pattern_maps = {k: {} for k in SIDES}
    next_tile = {k: None for k in SIDES}
    free_tile_sets = {
        k: set(range(512)) - used_tiles[k] - reserved_tiles[k]
        for k in SIDES
    }
    free_tile_sets["WS"] -= set(fountain_slots)

    def alloc_pattern(k, pattern, force_idx=None):
        if pattern in pattern_maps[k]:
            return pattern_maps[k][pattern]
        if force_idx is not None:
            idx = force_idx
        else:
            pool = sorted(free_tile_sets[k])
            if not pool:
                raise SystemExit(f"{k}: no free tiles")
            idx = pool[0]
        pattern_maps[k][pattern] = idx
        free_tile_sets[k].discard(idx)
        dest_tiles[k][idx] = pattern
        return idx

    # Fountain patterns in order.  The animation callbacks write the top
    # frames to slots 0..3 and the bottom frames to slots 4..7, so the top
    # metatile (lower y) must be assigned first.  Iterating a set here used
    # to swap the fountain halves while keeping the static render identical.
    fountain_order = sorted(
        fountain_keys,
        key=lambda ck: min((y, x) for _side, x, y in new_info[ck]["coords"]),
    )
    fountain_pattern_order = []
    for ckey in fountain_order:
        src_side, src_mid = new_info[ckey]["source"]
        for word in metatile_entry(current[src_side].meta, src_mid):
            tile = word & 0x3FF
            if tile >= 512:
                pattern = current[src_side].tiles[tile - 512]
                if pattern not in fountain_pattern_order:
                    fountain_pattern_order.append(pattern)
    for pattern, idx in zip(fountain_pattern_order, fountain_slots):
        pattern_maps["WS"][pattern] = idx
        dest_tiles["WS"][idx] = pattern
    print("fountain patterns", len(fountain_pattern_order))

    # Palette allocation per destination.
    palette_maps = {k: {} for k in SIDES}
    next_bank = {k: None for k in SIDES}
    free_banks = {
        k: [b for b in range(6, 13) if b not in used_banks[k]]
        for k in SIDES
    }

    def alloc_palette(k, content, prevent_bank=None):
        if content in palette_maps[k]:
            return palette_maps[k][content]
        # Reuse an existing bank with the same content.
        for b in range(6, 13):
            if dest_palettes[k][b] == content and (b not in used_banks[k] or True):
                palette_maps[k][content] = b
                return b
        for b in list(free_banks[k]):
            if b == prevent_bank:
                continue
            if all(v != content for v in palette_maps[k].values()):
                palette_maps[k][content] = b
                free_banks[k].remove(b)
                dest_palettes[k][b] = content
                return b
        raise SystemExit(f"{k}: no palette bank for new content")

    # Build new metatiles for every destination that needs them.
    for ckey, sid in new_ids.items():
        src_side, src_mid = new_info[ckey]["source"]
        src = current[src_side]
        affected = ["WS"]
        if ckey in border_ew:
            affected.append("ES")
        if ckey in border_ns:
            affected.append("WN")
        for k in affected:
            words = []
            for word in metatile_entry(src.meta, src_mid):
                tile = word & 0x3FF
                if not tile:
                    words.append(0)
                    continue
                p = (word >> 12) & 0xF
                if tile >= 512:
                    pattern = src.tiles[tile - 512]
                    force = pattern_maps[k].get(pattern)
                    new_tile = 512 + alloc_pattern(k, pattern, force)
                else:
                    new_tile = tile
                if p >= 6:
                    content = src.palettes[p]
                    p = alloc_palette(k, content)
                words.append((new_tile & 0x3FF) | (((word >> 10) & 3) << 10) | (p << 12))
            off = (sid - 512) * 16
            dest_meta[k][off : off + 16] = struct.pack("<8H", *words)
            aoff = (sid - 512) * 2
            dest_attrs[k][aoff : aoff + 2] = struct.pack("<H", ckey[1])

    def render_dest(k, mid):
        tmp = TilesetData.__new__(TilesetData)
        tmp.name = k
        tmp.directory = SEC / SIDES[k].folder
        tmp.meta = bytes(dest_meta[k])
        tmp.attrs = bytes(dest_attrs[k])
        tmp.tiles = dest_tiles[k]
        tmp.palettes = dest_palettes[k]
        return _render(metatile_entry(tmp.meta, mid), tmp.tiles, tmp.palettes, PRIMARY)

    # Some original West border IDs collide with different original East
    # metatiles. Move only those border appearances to a common free ID in
    # both tilesets.
    override_cells = {}
    override_ids = {}
    for west, east in (("WS", "ES"), ("WN", "EN")):
        for side in (west, east):
            s = SIDES[side]
            for y in range(s.height):
                for x in range(s.width):
                    if side == west:
                        if x < s.width - BAND:
                            continue
                    else:
                        if x >= BAND:
                            continue
                    mid = blocks[side][y * s.width + x] & 0x3FF
                    if mid < 512:
                        continue
                    kind, target = remap_key[(side, x, y)]
                    if kind != "orig":
                        continue
                    if render_dest(west, target) == render_dest(east, target):
                        continue
                    key = key_of(current[side], mid)
                    dedupe = (side, key)
                    if dedupe in override_ids:
                        override_cells[(side, x, y)] = override_ids[dedupe]
                        continue
                    affected = [west, east]
                    if side in ("WS", "ES") and y < BAND:
                        affected.append("WN" if side == "WS" else "EN")
                    elif side in ("WN", "EN") and y >= SIDES[side].height - BAND:
                        affected.append("WS" if side == "WN" else "ES")
                    common = set.intersection(*(free_ids[k] for k in affected))
                    if not common:
                        raise SystemExit("No common ID for original border override")
                    oid = min(common)
                    for k in affected:
                        free_ids[k].discard(oid)
                    override_ids[dedupe] = oid
                    override_cells[(side, x, y)] = oid
                    print("original border override", side, (x,y), hex(target), "->", hex(oid))
                    src = base[side]
                    for dest_side in affected:
                        words = []
                        for word in metatile_entry(src.meta, target):
                            tile = word & 0x3FF
                            if not tile:
                                words.append(0)
                                continue
                            pbit = (word >> 12) & 0xF
                            if tile >= 512:
                                pattern = src.tiles[tile - 512]
                                new_tile = 512 + alloc_pattern(dest_side, pattern)
                            else:
                                new_tile = tile
                            if pbit >= 6:
                                pbit = alloc_palette(dest_side, src.palettes[pbit])
                            words.append((new_tile & 0x3FF) | (((word >> 10) & 3) << 10) | (pbit << 12))
                        off = (oid - 512) * 16
                        dest_meta[dest_side][off:off+16] = struct.pack("<8H", *words)
                        aoff = (oid - 512) * 2
                        dest_attrs[dest_side][aoff:aoff+2] = struct.pack("<H", key[1])

    def final_target(side, x, y, mid):
        kind, target = remap_key[(side, x, y)]
        if kind == "orig":
            if (side, x, y) in override_cells:
                return override_cells[(side, x, y)]
            return target
        return new_ids[target]

    # Verify every new shared metatile in every destination that defines it.
    for ckey, sid in new_ids.items():
        affected = ["WS"]
        if ckey in border_ew:
            affected.append("ES")
        if ckey in border_ns:
            affected.append("WN")
        for k in affected:
            got = render_dest(k, sid)
            if got != ckey[0]:
                print("new verify failed", k, hex(sid), "border_ew", ckey in border_ew, "border_ns", ckey in border_ns)
                raise SystemExit("new shared verification failed")

    # Verify every used appearance.
    for k, s in SIDES.items():
        for y in range(s.height):
            for x in range(s.width):
                word = blocks[k][y * s.width + x]
                mid = word & 0x3FF
                if mid < 512:
                    continue
                target = final_target(k, x, y, mid)
                expected = key_of(current[k], mid)[0]
                got = render_dest(k, target)
                if got != expected:
                    raise SystemExit(f"verify failed {k} {(x,y)} {hex(mid)} -> {hex(target)}")

    # Border verification between adjacent destinations.
    def check(a, b, axis):
        bad = 0
        for side in (a, b):
            s = SIDES[side]
            for y in range(s.height):
                for x in range(s.width):
                    if axis == "ew":
                        ok = x >= s.width - BAND if s.horizontal == "west" else x < BAND
                    else:
                        ok = y < BAND if s.side == "south" else y >= s.height - BAND
                    if not ok:
                        continue
                    mid = blocks[side][y * s.width + x] & 0x3FF
                    if mid < 512:
                        continue
                    target = final_target(side, x, y, mid)
                    ra = render_dest(a, target)
                    rb = render_dest(b, target)
                    if ra != rb:
                        bad += 1
                        if bad <= 10:
                            print(" bad", side, (x,y), hex(mid), "->", hex(target))
        print("border", a, b, axis, "bad", bad)
        if bad:
            raise SystemExit("border verification failed")

    check("WS", "ES", "ew")
    check("WN", "EN", "ew")
    check("WS", "WN", "ns")
    check("ES", "EN", "ns")

    # Write destinations.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ROOT / ".poryeasy-backups" / f"{stamp}-preserve-rebuild"
    for k, s in SIDES.items():
        for path in (
            SEC / s.folder,
            ROOT / "data/layouts" / s.layout / "map.bin",
        ):
            rel = path.relative_to(ROOT)
            dst = backup / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, dst)
            else:
                shutil.copy2(path, dst)
    print("backup", backup)

    for k, s in SIDES.items():
        dest_dir = SEC / s.folder
        # Start from the original full base tileset.
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(SEC / s.base, dest_dir)
        # Preserve the fountain animation assets in West South.
        if k == "WS":
            for name in ("fountain", "fountain_bottom"):
                src = backup / "data/tilesets/secondary/battle_frontier_outside_west_south/anim" / name
                if src.is_dir():
                    shutil.copytree(src, dest_dir / "anim" / name)
        (dest_dir / "metatiles.bin").write_bytes(bytes(dest_meta[k]))
        (dest_dir / "metatile_attributes.bin").write_bytes(bytes(dest_attrs[k]))
        image = Image.open(SEC / s.base / "tiles.png").convert("P")
        palette = image.getpalette()
        arr = np.array(image)
        tw = arr.shape[1] // 8
        out = arr.copy()
        for idx in range(512):
            ty, tx = divmod(idx, tw)
            out[ty * 8 : ty * 8 + 8, tx * 8 : tx * 8 + 8] = np.frombuffer(
                dest_tiles[k][idx], dtype=np.uint8
            ).reshape(8, 8)
        new_image = Image.fromarray(out, mode="P")
        if palette:
            new_image.putpalette(palette)
        new_image.save(dest_dir / "tiles.png")
        for b in range(6, 13):
            (dest_dir / "palettes" / f"{b:02d}.pal").write_text(
                "\n".join(["JASC-PAL", "0100", "16", *dest_palettes[k][b]]) + "\n"
            )

        # Remap map blocks.
        new_blocks = []
        for y in range(SIDES[k].height):
            for x in range(SIDES[k].width):
                word = blocks[k][y * SIDES[k].width + x]
                mid = word & 0x3FF
                if mid < 512:
                    new_blocks.append(word)
                else:
                    target = final_target(k, x, y, mid)
                    new_blocks.append((word & ~0x3FF) | target)
        map_path = ROOT / "data/layouts" / s.layout / "map.bin"
        map_path.write_bytes(struct.pack(f"<{len(new_blocks)}H", *new_blocks))

    print("fountain slots", fountain_slots[0], fountain_slots[4])
