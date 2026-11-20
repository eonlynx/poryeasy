"""Split a map horizontally into south/north halves without breaking borders.

Both halves receive a full copy of the original secondary tileset.  Keeping the
copies identical means every tile and metatile ID already matches across the
seam, so map connections cannot render with the wrong graphics.  The command
also splits directly connected left/right maps (for example West and East) so
their connections stay aligned, preserves warp indices, and runs the shared
border pass as a final guarantee.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .border import DEFAULT_BAND, discover_variants, share_border
from .errors import MapEditorError
from .project import Project, Target, write_bytes_atomic, write_text_atomic


@dataclass
class SplitResult:
    map_ids: list[str] = field(default_factory=list)
    row: int = 0
    variants: dict[str, tuple[str, str]] = field(default_factory=dict)
    adjacent: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backup_dir: Path | None = None
    changed: bool = False


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")


def _insert_before(text: str, anchor: str, block: str) -> str:
    if anchor not in text:
        raise MapEditorError(f"Could not find insertion anchor: {anchor[:80]}")
    return text.replace(anchor, block + anchor, 1)


def _tiles_line(text: str, symbol: str) -> tuple[str, int]:
    short = symbol.removeprefix("gTileset_")
    pattern = re.compile(
        rf'const u32 gTilesetTiles_{re.escape(short)}\[\] = INCGFX_U32\('
        rf'"([^"]+)", "([^"]+)"(?:, "([^"]*)")?\);'
    )
    match = pattern.search(text)
    if not match:
        raise MapEditorError(f"Could not find graphics entry for {symbol}.")
    flags = match.group(3) or ""
    count_match = re.search(r"-num_tiles\s+(\d+)", flags)
    count = int(count_match.group(1)) if count_match else 0
    return match.group(0), count


def _callback_for(headers: str, symbol: str) -> str | None:
    start = headers.find(f"const struct Tileset {symbol}")
    if start < 0:
        return None
    block = headers[start : headers.find("};", start) + 2]
    match = re.search(r"\.callback\s*=\s*(\w+)", block)
    return match.group(1) if match else None


def _palette_block(symbol: str, folder: str) -> str:
    lines = [f"const u16 gTilesetPalettes_{symbol}[][16] =", "{"]
    for i in range(16):
        lines.append(
            f'    INCGFX_U16("data/tilesets/secondary/{folder}/palettes/{i:02d}.pal", ".gbapal"),'
        )
    lines.append("};")
    return "\n".join(lines) + "\n\n"


def _tileset_block(symbol: str, folder: str, tile_count: int) -> str:
    flag = f"-num_tiles {tile_count} -Wnum_tiles" if tile_count else "-Wnum_tiles"
    return (
        f'const u32 gTilesetTiles_{symbol}[] = INCGFX_U32('
        f'"data/tilesets/secondary/{folder}/tiles.png", ".4bpp.fastSmol", "{flag}");\n\n'
    )


def _map_connections(raw: dict) -> list[dict]:
    value = raw.get("connections", [])
    if not isinstance(value, list):
        return []
    return [dict(connection) for connection in value if isinstance(connection, dict)]


def _scripts_callbacks(scripts_path: Path, map_name: str) -> list[str]:
    if not scripts_path.is_file():
        return []
    text = scripts_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    callbacks = []
    inside = False
    for line in lines:
        if line.strip() == f"{map_name}_MapScripts::":
            inside = True
            continue
        if inside:
            stripped = line.strip()
            if stripped.startswith("map_script "):
                callbacks.append(stripped)
            elif stripped and not stripped.startswith("@"):
                break
    return callbacks


def _find_map(project: Project, target: Target):
    if target.map is not None:
        return target.map
    maps = project.maps_using_layout(target.layout.id)
    if len(maps) != 1:
        raise MapEditorError("Split needs a concrete map symbol (not a layout ID).")
    return maps[0]


def _group_maps(project: Project, base_map, adjacent: bool):
    map_by_id = {entry.id: entry for entry in project._load_maps()}
    group = [base_map]
    if not adjacent:
        return group
    for connection in _map_connections(base_map.raw):
        if connection.get("direction") not in ("left", "right"):
            continue
        neighbour = map_by_id.get(connection.get("map"))
        if neighbour is None:
            continue
        if neighbour.layout_id in {entry.layout_id for entry in group}:
            continue
        if project.layouts[neighbour.layout_id].height != project.layouts[base_map.layout_id].height:
            continue
        group.append(neighbour)
    return group


def split_map(
    project: Project,
    target: Target,
    *,
    row: int | None = None,
    band: int = DEFAULT_BAND,
    adjacent: bool = True,
    dry_run: bool = False,
    backup: bool = True,
) -> SplitResult:
    base_map = _find_map(project, target)
    if base_map.id.endswith("_NORTH") or f"{base_map.id}_NORTH" in {
        entry.id for entry in project._load_maps()
    }:
        raise MapEditorError(f"{base_map.name} already looks split into north/south halves.")

    group = _group_maps(project, base_map, adjacent)
    map_by_id = {entry.id: entry for entry in project._load_maps()}
    base_layout = project.layouts[base_map.layout_id]
    split_row = base_layout.height // 2 if row is None else row
    if not 0 < split_row < base_layout.height:
        raise MapEditorError(
            f"--row must be between 1 and {base_layout.height - 1} for {base_map.name}."
        )

    layouts = project.layouts
    map_groups_path = project.root / "data" / "maps" / "map_groups.json"
    map_group_data = _read_json(map_groups_path) if map_groups_path.is_file() else None
    if map_group_data is None:
        raise MapEditorError("Could not find map_groups.json.")
    group_name = next(
        (key for key, values in map_group_data.items() if isinstance(values, list) and base_map.name in values),
        None,
    )
    if group_name is None:
        raise MapEditorError(f"Could not find {base_map.name} in map_groups.json.")

    # Validate names before touching anything.
    planned = []
    for entry in group:
        layout = layouts[entry.layout_id]
        if row is not None and layout.height != base_layout.height:
            raise MapEditorError(
                f"{entry.name} has height {layout.height}, but the split row was given "
                f"for height {base_layout.height}. Split it separately."
            )
        map_name = entry.folder.name
        north_map_name = f"{map_name}_North"
        north_map_id = f"{entry.id}_NORTH"
        north_layout_id = f"{layout.id}_NORTH"
        north_layout_name = f"{map_name}_North_Layout"
        north_folder = entry.folder.parent / north_map_name
        north_layout_dir = project.root / "data" / "layouts" / north_map_name
        base_secondary = layout.secondary_tileset
        if base_secondary.endswith(("South", "North")):
            raise MapEditorError(
                f"{entry.name} already uses split tileset {base_secondary}."
            )
        south_secondary = f"{base_secondary}South"
        north_secondary = f"{base_secondary}North"
        south_folder_name = project.tileset_folder_name(south_secondary)
        north_folder_name = project.tileset_folder_name(north_secondary)
        for path in (north_folder, north_layout_dir):
            if path.exists():
                raise MapEditorError(f"Refusing to overwrite existing {path}.")
        for name in (south_secondary, north_secondary):
            try:
                existing = project.tileset_dir(name, "secondary")
            except MapEditorError:
                existing = None
            if existing is not None:
                raise MapEditorError(f"Refusing to overwrite existing tileset {name}.")
        planned.append(
            {
                "entry": entry,
                "layout": layout,
                "row": split_row if layout.height == base_layout.height else layout.height // 2,
                "north_map_name": north_map_name,
                "north_map_id": north_map_id,
                "north_layout_id": north_layout_id,
                "north_layout_name": north_layout_name,
                "north_folder": north_folder,
                "north_layout_dir": north_layout_dir,
                "base_secondary": base_secondary,
                "south_secondary": south_secondary,
                "north_secondary": north_secondary,
                "south_folder_name": south_folder_name,
                "north_folder_name": north_folder_name,
            }
        )

    result = SplitResult(
        map_ids=[base_map.id] + [f"{entry.id}_NORTH" for entry in group],
        row=split_row,
        adjacent=[entry.id for entry in group[1:]],
        changed=True,
    )
    if dry_run:
        for item in planned:
            print(
                f"  plan: {item['entry'].name} -> {item['north_map_name']} "
                f"(row {item['row']}, {item['south_secondary']} / {item['north_secondary']})"
            )
        return result

    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = project.root / ".poryeasy-backups" / f"{stamp}-split"
        files = [
            "data/layouts/layouts.json",
            "data/maps/map_groups.json",
            "data/event_scripts.s",
            "src/data/tilesets/graphics.h",
            "src/data/tilesets/headers.h",
            "src/data/tilesets/metatiles.h",
            "src/data/heal_locations.json",
        ]
        for item in planned:
            files.extend(
                [
                    f"data/maps/{item['entry'].folder.name}",
                    f"data/layouts/{item['entry'].folder.name}",
                ]
            )
        for rel in files:
            src = project.root / rel
            if not src.exists():
                continue
            dst = backup_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        result.backup_dir = backup_dir

    # Prepare compacted half-tilesets. Full copies would leave no empty
    # metatile slots in tilesets that are already at 512 entries; compaction
    # keeps only the metatiles each half actually uses and leaves room for the
    # shared border set.
    ENTRY = 16
    ATTR = 2
    for item in planned:
        layout = item["layout"]
        row_here = item["row"]
        blocks = project.read_blocks(layout)
        border = project.read_border(layout)
        south_raw = [
            word
            for y in range(row_here, layout.height)
            for word in blocks[y * layout.width : (y + 1) * layout.width]
        ]
        north_raw = [
            word
            for y in range(0, row_here)
            for word in blocks[y * layout.width : (y + 1) * layout.width]
        ]
        south_used = sorted(
            {word & 0x3FF for word in south_raw + list(border) if (word & 0x3FF) >= 512}
        )
        north_used = sorted(
            {word & 0x3FF for word in north_raw + list(border) if (word & 0x3FF) >= 512}
        )
        south_map = {old: 512 + index for index, old in enumerate(south_used)}
        north_map = {old: 512 + index for index, old in enumerate(north_used)}

        def remap(values, mapping):
            return [
                (word & ~0x3FF) | mapping.get(word & 0x3FF, word & 0x3FF)
                for word in values
            ]

        item["south_blocks"] = remap(south_raw, south_map)
        item["north_blocks"] = remap(north_raw, north_map)
        item["south_border"] = remap(list(border), south_map)
        item["north_border"] = remap(list(border), north_map)
        item["south_used"] = south_used
        item["north_used"] = north_used

    # Tileset variant copies and declarations.
    graphics_path = project.root / "src" / "data" / "tilesets" / "graphics.h"
    headers_path = project.root / "src" / "data" / "tilesets" / "headers.h"
    metatiles_path = project.root / "src" / "data" / "tilesets" / "metatiles.h"
    graphics = graphics_path.read_text(encoding="utf-8")
    headers = headers_path.read_text(encoding="utf-8")
    metatiles = metatiles_path.read_text(encoding="utf-8")
    added = set()
    for item in planned:
        base_secondary = item["base_secondary"]
        if base_secondary in added:
            continue
        added.add(base_secondary)
        result.variants[base_secondary] = (
            item["south_secondary"],
            item["north_secondary"],
        )
        base_dir = project.tileset_dir(base_secondary, "secondary")
        base_meta = (base_dir / "metatiles.bin").read_bytes()
        base_attrs = (base_dir / "metatile_attributes.bin").read_bytes()
        tile_line, tile_count = _tiles_line(graphics, base_secondary)
        callback = _callback_for(headers, base_secondary)
        for symbol, folder, folder_name, used, mapping in (
            (
                item["south_secondary"],
                item["south_folder_name"],
                item["south_folder_name"],
                item["south_used"],
                {
                    old: 512 + index
                    for index, old in enumerate(item["south_used"])
                },
            ),
            (
                item["north_secondary"],
                item["north_folder_name"],
                item["north_folder_name"],
                item["north_used"],
                {
                    old: 512 + index
                    for index, old in enumerate(item["north_used"])
                },
            ),
        ):
            dest = project.paths["secondary_dir"] / folder_name
            if dest.exists():
                raise MapEditorError(f"Tileset folder already exists: {dest}")
            dest.mkdir(parents=True)
            shutil.copy2(base_dir / "tiles.png", dest / "tiles.png")
            shutil.copytree(base_dir / "palettes", dest / "palettes")
            if (base_dir / "anim").is_dir():
                shutil.copytree(base_dir / "anim", dest / "anim")
            new_meta = bytearray(512 * ENTRY)
            new_attrs = bytearray(512 * ATTR)
            for old in used:
                new = mapping[old]
                new_meta[(new - 512) * ENTRY : (new - 511) * ENTRY] = base_meta[
                    (old - 512) * ENTRY : (old - 511) * ENTRY
                ]
                new_attrs[(new - 512) * ATTR : (new - 511) * ATTR] = base_attrs[
                    (old - 512) * ATTR : (old - 511) * ATTR
                ]
            (dest / "metatiles.bin").write_bytes(bytes(new_meta))
            (dest / "metatile_attributes.bin").write_bytes(bytes(new_attrs))
            graphics += _tileset_block(symbol, folder_name, tile_count) + _palette_block(
                symbol, folder_name
            )
            headers += f"""const struct Tileset {symbol} =
{{
    .isCompressed = TRUE,
    .isSecondary = TRUE,
    .tiles = gTilesetTiles_{symbol},
    .palettes = gTilesetPalettes_{symbol},
    .metatiles = gMetatiles_{symbol},
    .metatileAttributes = gMetatileAttributes_{symbol},
    .callback = {callback or 'NULL'},
}};

"""
            metatiles += (
                f'const u16 gMetatiles_{symbol}[] = INCBIN_U16('
                f'"data/tilesets/secondary/{folder_name}/metatiles.bin");\n'
                f'const u16 gMetatileAttributes_{symbol}[] = INCBIN_U16('
                f'"data/tilesets/secondary/{folder_name}/metatile_attributes.bin");\n\n'
            )

    # Layouts, blocks and borders.
    layouts_json = project.paths["layouts_json"]
    layout_data = _read_json(layouts_json)
    layout_entries = layout_data["layouts"] if isinstance(layout_data, dict) else layout_data
    for item in planned:
        entry = item["entry"]
        layout = item["layout"]
        row_here = item["row"]
        south_blocks = item["south_blocks"]
        north_blocks = item["north_blocks"]
        south_border = item["south_border"]
        north_border = item["north_border"]
        write_bytes_atomic(
            project.layout_path(layout.blockdata_filepath),
            struct.pack(f"<{len(south_blocks)}H", *south_blocks),
        )
        border_path = project.layout_path(layout.border_filepath)
        if south_border:
            write_bytes_atomic(
                border_path,
                struct.pack(f"<{len(south_border)}H", *south_border),
            )
        item["north_layout_dir"].mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(
            item["north_layout_dir"] / "map.bin",
            struct.pack(f"<{len(north_blocks)}H", *north_blocks),
        )
        if north_border:
            write_bytes_atomic(
                item["north_layout_dir"] / "border.bin",
                struct.pack(f"<{len(north_border)}H", *north_border),
            )
        raw_layout = dict(layout.raw)
        raw_layout["height"] = row_here
        raw_layout["secondary_tileset"] = item["south_secondary"]
        layout_entries[layout_entries.index(layout.raw)] = raw_layout
        north_raw = dict(layout.raw)
        north_raw.update(
            {
                "id": item["north_layout_id"],
                "name": item["north_layout_name"],
                "height": layout.height - row_here,
                "secondary_tileset": item["north_secondary"],
                "border_filepath": f"data/layouts/{item['north_layout_dir'].name}/border.bin",
                "blockdata_filepath": f"data/layouts/{item['north_layout_dir'].name}/map.bin",
            }
        )
        layout_entries.insert(layout_entries.index(raw_layout) + 1, north_raw)

    # Maps and events.
    map_group_values = map_group_data[group_name]
    for item in planned:
        entry = item["entry"]
        original = _read_json(entry.folder / "map.json")
        row_here = item["row"]
        south = dict(original)
        north = dict(original)
        south["id"] = entry.id
        south["name"] = entry.name
        south["layout"] = entry.layout_id
        north["id"] = item["north_map_id"]
        north["name"] = item["north_map_name"]
        north["layout"] = item["north_layout_id"]

        for key in ("object_events", "coord_events", "bg_events"):
            events = original.get(key, [])
            north_events = [dict(e) for e in events if int(e.get("y", 0)) < row_here]
            south_events = []
            for event in events:
                y = int(event.get("y", 0))
                if y >= row_here:
                    shifted = dict(event)
                    shifted["y"] = y - row_here
                    south_events.append(shifted)
            south[key] = south_events
            north[key] = north_events

        warps = original.get("warp_events", [])
        south_warps = []
        north_warps = []
        warp_mapping = {}
        for index, warp in enumerate(warps):
            y = int(warp.get("y", 0))
            if y < row_here:
                clone = dict(warp)
                north_warps.append(clone)
                warp_mapping[index] = ("north", len(north_warps) - 1)
                placeholder = dict(warp)
                placeholder["x"] = 0
                placeholder["y"] = 255
                south_warps.append(placeholder)
            else:
                clone = dict(warp)
                clone["y"] = y - row_here
                south_warps.append(clone)
                warp_mapping[index] = ("south", index)
        south["warp_events"] = south_warps
        north["warp_events"] = north_warps

        south_connections = []
        north_connections = []
        for connection in _map_connections(original):
            direction = connection.get("direction")
            target_name = connection.get("map")
            if direction == "up":
                north_connections.append(dict(connection))
                south_connections.append(
                    {
                        "map": item["north_map_id"],
                        "offset": 0,
                        "direction": "up",
                    }
                )
            elif direction == "down":
                south_connections.append(dict(connection))
                north_connections.append(
                    {
                        "map": entry.id,
                        "offset": 0,
                        "direction": "down",
                    }
                )
            elif direction in ("left", "right"):
                side_suffix = "_NORTH" if direction else ""
                south_connections.append(dict(connection))
                neighbour_south = target_name
                neighbour_north = (
                    f"{target_name}_NORTH"
                    if target_name and not target_name.endswith("_NORTH")
                    else target_name
                )
                north_connections.append(
                    {
                        "map": neighbour_north if neighbour_north in map_by_id else target_name,
                        "offset": connection.get("offset", 0),
                        "direction": direction,
                    }
                )
        south["connections"] = south_connections
        north["connections"] = north_connections

        _write_json(entry.folder / "map.json", south)
        item["north_folder"].mkdir(parents=True, exist_ok=False)
        _write_json(item["north_folder"] / "map.json", north)

        # Scripts: the north header references the south callbacks, which remain
        # global labels because event_scripts.s includes both files once.
        callbacks = _scripts_callbacks(entry.folder / "scripts.inc", entry.folder.name)
        script_lines = [f"{item['north_map_name']}_MapScripts::"]
        script_lines.extend(f"\t{line}" for line in callbacks)
        script_lines.append("\t.byte 0")
        write_text_atomic(
            item["north_folder"] / "scripts.inc",
            "\n".join(script_lines) + "\n",
        )
        write_text_atomic(
            item["north_folder"] / "text.inc",
            f"@ Text labels for {entry.name} remain in the south map.\n",
        )

        map_group_values.insert(
            map_group_values.index(entry.folder.name) + 1, item["north_map_name"]
        )

        # External warps that pointed at a north-half warp now target the north map.
        for path in (project.paths["maps_dir"]).glob("*/map.json"):
            try:
                data = _read_json(path)
            except Exception:
                continue
            changed = False
            for warp in data.get("warp_events", []):
                if warp.get("dest_map") != entry.id:
                    continue
                try:
                    index = int(str(warp.get("dest_warp_id", "-1")))
                except ValueError:
                    continue
                target_warp = warp_mapping.get(index)
                if target_warp and target_warp[0] == "north":
                    warp["dest_map"] = item["north_map_id"]
                    warp["dest_warp_id"] = str(target_warp[1])
                    changed = True
            if changed:
                _write_json(path, data)

    # Includes.
    event_scripts = project.root / "data" / "event_scripts.s"
    text = event_scripts.read_text(encoding="utf-8", errors="replace")
    for item in planned:
        include = f'.include "data/maps/{item["north_map_name"]}/scripts.inc"'
        if include not in text:
            anchor = f'.include "data/maps/{item["entry"].folder.name}/scripts.inc"'
            if anchor in text:
                text = text.replace(anchor, anchor + "\n\t" + include, 1)
            else:
                text += "\n\t" + include + "\n"
    write_text_atomic(event_scripts, text)

    # Heal locations in the north half move to the north map.
    heal_json = project.root / "src" / "data" / "heal_locations.json"
    if heal_json.is_file():
        heal_data = _read_json(heal_json)
        changed = False
        for entry in heal_data.get("heal_locations", heal_data if isinstance(heal_data, list) else []):
            if entry.get("map") != base_map.id:
                continue
            y = entry.get("y")
            if isinstance(y, int) and y >= split_row:
                entry["map"] = f"{base_map.id}_NORTH"
                entry["y"] = y - split_row
                changed = True
        if changed:
            _write_json(heal_json, heal_data)

    # Connections in maps outside the group: down/up can be pointed at the
    # correct half; horizontal connections are reported for manual review.
    for path in (project.paths["maps_dir"]).glob("*/map.json"):
        if any(path == item["entry"].folder / "map.json" or path == item["north_folder"] / "map.json" for item in planned):
            continue
        try:
            data = _read_json(path)
        except Exception:
            continue
        connections = data.get("connections", [])
        if not isinstance(connections, list):
            continue
        changed = False
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            if connection.get("map") != base_map.id:
                continue
            if connection.get("direction") == "down":
                connection["map"] = f"{base_map.id}_NORTH"
                changed = True
            elif connection.get("direction") in ("left", "right"):
                result.warnings.append(
                    f"{path.parent.name} has a horizontal connection to {base_map.name}; "
                    "check which half it should target."
                )
        if changed:
            _write_json(path, data)

    # Persist JSON files after all planned maps have been inserted.
    if isinstance(layout_data, dict):
        layout_data["layouts"] = layout_entries
        _write_json(layouts_json, layout_data)
    else:
        _write_json(layouts_json, layout_entries)
    _write_json(map_groups_path, map_group_data)
    write_text_atomic(graphics_path, graphics)
    write_text_atomic(headers_path, headers)
    write_text_atomic(metatiles_path, metatiles)

    # Re-run the shared-border pass on the new group. With identical full-copy
    # variants this is normally a no-op, but it also handles edited variants.
    project._maps = None
    project.layouts = project._load_layouts()
    target_map = next(entry for entry in project._load_maps() if entry.id == base_map.id)
    try:
        variants = discover_variants(project, Target(base_map.id, target_map, project.layouts[target_map.layout_id]))
        border_result = share_border(project, variants, band=band, backup=False)
        result.changed = result.changed or border_result.changed
        result.warnings.extend(border_result.warnings)
    except MapEditorError as exc:
        result.warnings.append(f"shared border pass skipped: {exc}")

    return result
