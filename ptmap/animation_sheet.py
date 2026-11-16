"""Export and import the Poryeasy animated metatile sheet."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image

from .animations import (
    AnimationCatalog,
    DoorAnimationDef,
    TilesetAnimationDef,
    clear_tile_pattern_cache,
    load_indexed_tile_patterns,
    scan_animation_catalog,
)
from .errors import MapEditorError
from .palette_planner import apply_palette_plan, plan_bank_colors
from .project import Project, write_text_atomic
from .tileset import (
    METATILE_SIZE,
    TILE_SIZE,
    TileRef,
    TilesetPair,
    flip_pattern,
    patch_from_rgba,
)

CELL = METATILE_SIZE
ANIMATION_SHEET_NAME = "animated_metatiles.png"
ANIMATION_MANIFEST_NAME = "animated_metatiles.json"
SHEET_FORMAT = "poryeasy-animated-metatiles"
SHEET_VERSION = 1


@dataclass
class NewDoorPlan:
    base: DoorAnimationDef
    name: str
    part_rows: list[list[tuple[int, bytes]]]
    start_extra: int = 0


@dataclass
class AnimationApplyResult:
    changed: bool = False
    palette_changed: bool = False
    written: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    touched_tilesets: set[tuple[str, str]] = field(default_factory=set)
    extra_metatile_states: dict[tuple[bytes, bytes, bytes], list[int]] = field(
        default_factory=dict
    )


@dataclass
class AnimationRow:
    kind: str
    states: int
    data: dict
    cells: list[bytes] = field(default_factory=list)


def _defs_for_metatile(
    catalog: AnimationCatalog, pair: TilesetPair, metatile
) -> list[TilesetAnimationDef]:
    used: list[TilesetAnimationDef] = []
    for kind, tileset in (
        ("primary", pair.primary.name),
        ("secondary", pair.secondary.name),
    ):
        defs = catalog.defs_for(kind, tileset)
        if not defs:
            continue
        for entry in metatile:
            for definition in defs:
                if definition not in used and any(
                    start <= entry.tile_index < start + count
                    for start, count in definition.ranges
                ):
                    used.append(definition)
    return used


def _frame_patterns(
    project: Project, definition: TilesetAnimationDef, frame_index: int
) -> list[bytes]:
    paths = definition.frame_paths
    if not paths:
        return []
    path = project.root / paths[frame_index % len(paths)]
    return list(load_indexed_tile_patterns(str(path)))


def _state_overrides(
    project: Project, definition: TilesetAnimationDef, state: int
) -> dict[int, bytes]:
    patterns = _frame_patterns(project, definition, state)
    overrides: dict[int, bytes] = {}
    for start, count in definition.ranges:
        for index in range(count):
            if index < len(patterns):
                overrides[start + index] = patterns[index]
    return overrides


def _render_metatile_state(
    project: Project,
    pair: TilesetPair,
    metatile,
    definitions: list[TilesetAnimationDef],
    state: int,
) -> bytes:
    overrides: dict[int, bytes] = {}
    for definition in definitions:
        overrides.update(_state_overrides(project, definition, state))
    layers = [pair.render_layer_patch(metatile, layer, overrides) for layer in range(3)]
    out = bytearray(CELL * CELL * 4)
    for layer in layers:
        for i in range(0, len(layer), 4):
            if layer[i + 3]:
                out[i : i + 4] = layer[i : i + 4]
    return bytes(out)


def _metatile_state_signature(
    project: Project,
    pair: TilesetPair,
    metatile,
    definitions: list[TilesetAnimationDef],
    state: int,
) -> tuple[bytes, bytes, bytes]:
    overrides: dict[int, bytes] = {}
    for definition in definitions:
        overrides.update(_state_overrides(project, definition, state))
    return tuple(
        pair.render_layer_patch(metatile, layer, overrides) for layer in range(3)
    )  # type: ignore[return-value]


def animated_state_signature_index(
    project: Project, pair: TilesetPair, directory: Path
) -> dict[tuple[bytes, bytes, bytes], list[int]]:
    manifest_path = directory / ANIMATION_MANIFEST_NAME
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    catalog = scan_animation_catalog(project)
    index: dict[tuple[bytes, bytes, bytes], list[int]] = {}
    for row in manifest.get("rows", []):
        if row.get("kind") != "metatile":
            continue
        metatile_id = int(row["metatile_id"])
        try:
            metatile = pair.metatile(metatile_id)
        except MapEditorError:
            continue
        definitions = [
            definition
            for definition in (
                catalog.by_key(animation["key"])
                for animation in row.get("animations", [])
            )
            if definition is not None
        ]
        if not definitions:
            continue
        states = int(row.get("states", 0))
        for state in range(states):
            signature = _metatile_state_signature(
                project, pair, metatile, definitions, state
            )
            index.setdefault(signature, []).append(metatile_id)
    return index


def sheet_cell_index(directory: Path) -> dict[bytes, list[int]]:
    """Indexes the painted animation sheet by composited 16x16 cell.

    Door rows other than part 0 are skipped: they are animation-only overlays
    and do not correspond to a map cell that should be remapped.
    """
    manifest_path = directory / ANIMATION_MANIFEST_NAME
    sheet_path = directory / ANIMATION_SHEET_NAME
    if not manifest_path.is_file() or not sheet_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    try:
        sheet = Image.open(sheet_path).convert("RGBA")
    except OSError:
        return {}
    index: dict[bytes, list[int]] = {}
    for row_index, row in enumerate(manifest.get("rows", [])):
        kind = row.get("kind")
        if kind not in ("metatile", "door"):
            continue
        if kind == "door" and int(row.get("part", 0)) != 0:
            continue
        try:
            metatile_id = int(row["metatile_id"])
        except (KeyError, TypeError, ValueError):
            continue
        states = int(row.get("states", 0))
        for state in range(states):
            cell = sheet.crop(
                (
                    state * CELL,
                    row_index * CELL,
                    (state + 1) * CELL,
                    (row_index + 1) * CELL,
                )
            ).tobytes()
            if not any(cell[index] for index in range(3, len(cell), 4)):
                continue
            index.setdefault(cell, []).append(metatile_id)
    return index


def _door_frame_patterns(project: Project, path: str) -> list[bytes]:
    return list(load_indexed_tile_patterns(str(project.root / path)))


def _door_tile_layout(door, path: Path) -> tuple[int, int, int, int]:
    with Image.open(path) as opened:
        width, height = opened.size
    if width % TILE_SIZE or height % TILE_SIZE:
        raise MapEditorError(f"Door image {path} dimensions are not multiples of 8.")
    tiles_width = width // TILE_SIZE
    tiles_height = height // TILE_SIZE
    metatile_width = max(1, door.metatile_width)
    metatile_height = max(1, door.metatile_height)
    if tiles_width % metatile_width or tiles_height % metatile_height:
        raise MapEditorError(
            f"Door image {path} is not divisible by its packed metatile size "
            f"({metatile_width}x{metatile_height} tiles)."
        )
    return tiles_width, tiles_height, metatile_width, metatile_height


def _door_index_pairs(
    tiles_width: int, tiles_height: int, metatile_width: int, metatile_height: int
) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    metatiles_wide = tiles_width // metatile_width
    metatiles_high = tiles_height // metatile_height
    for metatile_y in range(metatiles_high):
        for metatile_x in range(metatiles_wide):
            for subtile_y in range(metatile_height):
                for subtile_x in range(metatile_width):
                    source_index = (
                        (metatile_y * metatile_height + subtile_y) * tiles_width
                        + metatile_x * metatile_width
                        + subtile_x
                    )
                    dest_index = (
                        (metatile_y * metatiles_wide + metatile_x)
                        * metatile_width
                        * metatile_height
                        + subtile_y * metatile_width
                        + subtile_x
                    )
                    pairs.append((source_index, dest_index))
    return pairs


def _door_dest_patterns(project: Project, door) -> list[bytes]:
    path = project.root / door.frame_path
    source = _door_frame_patterns(project, door.frame_path)
    tiles_width, tiles_height, metatile_width, metatile_height = _door_tile_layout(
        door, path
    )
    dest = [bytes(64)] * (tiles_width * tiles_height)
    for source_index, dest_index in _door_index_pairs(
        tiles_width, tiles_height, metatile_width, metatile_height
    ):
        if source_index < len(source) and dest_index < len(dest):
            dest[dest_index] = source[source_index]
    return dest


def _door_source_patterns_from_dest(
    project: Project, door, dest: list[bytes]
) -> list[bytes]:
    path = project.root / door.frame_path
    tiles_width, tiles_height, metatile_width, metatile_height = _door_tile_layout(
        door, path
    )
    needed_height = max(tiles_height, (len(dest) + tiles_width - 1) // tiles_width)
    if needed_height % metatile_height:
        needed_height += metatile_height - (needed_height % metatile_height)
    source = [bytes(64)] * (tiles_width * needed_height)
    for source_index, dest_index in _door_index_pairs(
        tiles_width, needed_height, metatile_width, metatile_height
    ):
        if source_index < len(source) and dest_index < len(dest):
            source[source_index] = dest[dest_index]
    return source


def _attribute_with_behavior(
    project: Project, attribute: bytes, behavior: str | None
) -> tuple[bytes, str | None]:
    if not behavior or not behavior.strip():
        return attribute, None
    raw = behavior.strip()
    value: int | None = None
    try:
        value = int(raw, 0)
    except ValueError:
        header = project.root / "include" / "constants" / "metatile_behaviors.h"
        if header.is_file():
            text = header.read_text(encoding="utf-8", errors="replace")
            enum_match = re.search(r"enum\s*\{(.*?)\}", text, re.DOTALL)
            if enum_match is not None:
                body = re.sub(r"/\*.*?\*/", "", enum_match.group(1), flags=re.DOTALL)
                body = re.sub(r"//[^\n]*", "", body)
                values: dict[str, int] = {}
                current = 0
                for item in body.split(","):
                    item = item.strip()
                    if not item:
                        continue
                    name, _, assigned = item.partition("=")
                    name = name.strip()
                    if not re.fullmatch(r"MB_\w+", name):
                        continue
                    if assigned.strip():
                        try:
                            current = int(assigned.strip(), 0)
                        except ValueError:
                            continue
                    values[name] = current
                    current += 1
                value = values.get(
                    raw.upper() if raw.upper().startswith("MB_") else raw
                )
    if value is None:
        return attribute, (
            f"Unknown metatile behavior '{behavior}'; the new animated metatile "
            "inherited the animation's existing behavior."
        )
    mask = 0xFF if len(attribute) <= 2 else 0x1FF
    merged = (int.from_bytes(attribute, "little") & ~mask) | (value & mask)
    return merged.to_bytes(len(attribute), "little"), None


def _metatile_for_animation(pair: TilesetPair, definition: TilesetAnimationDef):
    for tileset in (pair.primary, pair.secondary):
        for local, metatile in enumerate(tileset.metatiles):
            if any(
                any(
                    start <= entry.tile_index < start + count
                    for start, count in definition.ranges
                )
                for entry in metatile
            ):
                return tileset, local, metatile
    return None


def _apply_new_door_plan(
    project: Project,
    pair: TilesetPair,
    plan: NewDoorPlan,
    result: AnimationApplyResult,
    dry_run: bool,
) -> DoorAnimationDef:
    base = plan.base
    tileset = pair.primary if base.tileset == pair.primary.name else pair.secondary
    parts = base.parts[0] * base.parts[1]
    states = max(
        (
            max((column for column, _cell in cells), default=-1) + 1
            for cells in plan.part_rows
        ),
        default=0,
    )
    if states > len(base.frame_offsets):
        result.warnings.append(
            f"New door {plan.name}: only {len(base.frame_offsets)} states fit the "
            "selected base door timing; extra columns were ignored."
        )
    states = min(states, len(base.frame_offsets))
    dest = _door_dest_patterns(project, base)
    palette_values = list(base.palette_banks)
    bank_changes = 0
    for state in range(states):
        for part in range(parts):
            cell_by_state = dict(plan.part_rows[part])
            cell = cell_by_state.get(state)
            if cell is None:
                continue
            for subtile in range(4):
                bank_index = part * 4 + subtile
                palette_bank = (
                    palette_values[bank_index]
                    if bank_index < len(palette_values)
                    else 1
                )
                sx = (subtile % 2) * TILE_SIZE
                sy = (subtile // 2) * TILE_SIZE
                patch = patch_from_rgba(cell, CELL, sx, sy, TILE_SIZE)
                pattern, chosen_bank = _quantize_door_pattern(
                    pair,
                    patch,
                    palette_bank,
                    f"new door {plan.name} state {state} part {part}",
                )
                if bank_index < len(palette_values) and chosen_bank != palette_bank:
                    palette_values[bank_index] = chosen_bank
                    bank_changes += 1
                template_tile = base.frame_offsets[0] + part * 4 + subtile
                template_pattern = (
                    dest[template_tile] if template_tile < len(dest) else bytes(64)
                )
                offset = 16 if any(value >= 16 for value in template_pattern) else 0
                stored = bytes((offset + value) if value else 0 for value in pattern)
                dest_index = base.frame_offsets[state] + part * 4 + subtile
                if dest_index < len(dest):
                    dest[dest_index] = stored

    door_dir = project.root / "graphics" / "door_anims"
    labels_path = project.paths["metatile_labels"]
    labels_text = (
        labels_path.read_text(encoding="utf-8") if labels_path.is_file() else ""
    )
    field_door = project.root / "src" / "field_door.c"
    door_text = field_door.read_text(encoding="utf-8", errors="replace")
    prefix = base.tileset.removeprefix("gTileset_")
    raw_name = re.sub(r"[^A-Za-z0-9]+", "_", plan.name).strip("_")
    raw_name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw_name)
    raw_name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", raw_name)
    words = [part.lower() for part in raw_name.split("_") if part]
    if not words:
        words = ["custom", "door"]
    base_snake = "_".join(words)
    snake = base_snake
    suffix = 2
    while True:
        camel = "".join(part.capitalize() for part in snake.split("_"))
        label = f"METATILE_{prefix}_Door_{camel}"
        new_var = f"sDoorAnimTiles_{camel}"
        new_png = door_dir / f"{snake}.png"
        if (
            not new_png.exists()
            and not re.search(rf"#define\s+{re.escape(label)}\b", labels_text)
            and not re.search(rf"\b{re.escape(new_var)}\b", door_text)
        ):
            break
        snake = f"{base_snake}_{suffix}"
        suffix += 1

    source_patterns = _door_source_patterns_from_dest(project, base, dest)
    if not dry_run:
        _write_pattern_image_from_template(
            project.root / base.frame_path, new_png, source_patterns
        )
    result.written.append(new_png)

    base_local = base.metatile_id - tileset.global_metatile_base
    if not 0 <= base_local < len(tileset.metatiles):
        raise MapEditorError(f"Base door metatile 0x{base.metatile_id:03X} is invalid.")
    new_metatile = tileset.allocate_metatile(
        list(tileset.metatiles[base_local]),
        tileset.attributes[base_local],
    )

    declaration_pattern = re.compile(
        rf"(static\s+const\s+u8\s+{re.escape(base.tiles_var)}\[\]\s*=\s*[^;]*;)"
    )
    declaration_match = declaration_pattern.search(door_text)
    if declaration_match is None:
        raise MapEditorError(
            f"Could not find the declaration for {base.tiles_var} in field_door.c."
        )
    options = (
        ""
        if base.metatile_width == 1 and base.metatile_height == 1
        else f', "-mwidth {base.metatile_width} -mheight {base.metatile_height}"'
    )
    relative_png = new_png.relative_to(project.root).as_posix()
    declaration = (
        f"\nstatic const u8 {new_var}[] = "
        f'INCGFX_U8("{relative_png}", ".4bpp"{options});'
    )
    new_palettes_var = f"sDoorAnimPalettes_{camel}"
    palette_declaration = (
        f"\nstatic const u8 {new_palettes_var}[] = {{"
        + ", ".join(str(value) for value in palette_values)
        + "};"
    )
    anchor = door_text.find("static const struct DoorGraphics sDoorAnimGraphicsTable")
    if anchor < 0:
        anchor = declaration_match.end()
    door_text = (
        door_text[:anchor] + declaration + palette_declaration + door_text[anchor:]
    )
    table_match = re.search(
        r"(sDoorAnimGraphicsTable\[\]\s*=\s*\{)(.*?)(\n\s*\{\},\n\};)",
        door_text,
        re.DOTALL,
    )
    if table_match is None:
        raise MapEditorError("Could not find sDoorAnimGraphicsTable in field_door.c.")
    entry = f"""
    {{
        .metatileNum = {label},
        .tileset = &{base.tileset},
        .sound = {base.sound},
        .size = {base.size_name},
        .tiles = {new_var},
        .palettes = {new_palettes_var},
    }},"""
    door_text = (
        door_text[: table_match.start(3)] + entry + door_text[table_match.start(3) :]
    )
    if not dry_run:
        write_text_atomic(field_door, door_text)
        label_line = f"#define {label} 0x{new_metatile:03X}\n"
        marker = labels_text.rfind("#endif")
        if marker >= 0:
            labels_text = labels_text[:marker] + label_line + labels_text[marker:]
        else:
            labels_text = labels_text.rstrip() + "\n" + label_line
        write_text_atomic(labels_path, labels_text)
    result.written.extend([field_door, labels_path])
    result.changed = True
    result.touched_tilesets.add((tileset.name, tileset.kind))
    result.warnings.append(
        f"Created new door '{plan.name}' (label {label}, metatile "
        f"0x{new_metatile:03X}, PNG {relative_png}). It has its own palette array "
        f"initialized from {base.palettes_var}"
        + (f" with {bank_changes} automatic bank change(s)" if bank_changes else "")
        + " and reuses the base door's timing. Paint its metatile in the map to use it."
    )
    return DoorAnimationDef(
        key=f"{base.tileset}/{label}",
        tileset=base.tileset,
        kind=base.kind,
        metatile_id=new_metatile,
        metatile_label=label,
        parts=base.parts,
        frame_path=relative_png,
        frame_offsets=list(base.frame_offsets),
        frame_tile_count=base.frame_tile_count,
        palette_banks=palette_values,
        metatile_width=base.metatile_width,
        metatile_height=base.metatile_height,
        part_positions=list(base.part_positions),
        sound=base.sound,
        size_name=base.size_name,
        tiles_var=new_var,
        palettes_var=new_palettes_var,
    )


def _render_door_part(
    project: Project,
    pair: TilesetPair,
    door,
    state: int,
    part_index: int,
) -> bytes:
    patterns = _door_dest_patterns(project, door)
    frame_offset = door.frame_offsets[state]
    start = frame_offset + part_index * 4
    if start + 4 > len(patterns):
        raise MapEditorError(
            f"Door animation {door.key} frame {state} does not contain part {part_index}."
        )
    out = bytearray(CELL * CELL * 4)
    for tile_index in range(4):
        pattern = patterns[start + tile_index]
        sx = (tile_index % 2) * TILE_SIZE
        sy = (tile_index // 2) * TILE_SIZE
        palette_bank = (
            door.palette_banks[part_index * 4 + tile_index]
            if part_index * 4 + tile_index < len(door.palette_banks)
            else 1
        )
        palette = pair.palette_for(palette_bank)
        for row in range(TILE_SIZE):
            for col in range(TILE_SIZE):
                value = pattern[row * TILE_SIZE + col] & 0x0F
                if value == 0:
                    continue
                r, g, b = palette[value] if value < len(palette) else (0, 0, 0)
                pos = ((sy + row) * CELL + sx + col) * 4
                out[pos : pos + 4] = bytes((r, g, b, 255))
    return bytes(out)


def sheet_rows(
    project: Project,
    pair: TilesetPair,
    blocks: list[int],
) -> list[AnimationRow]:
    catalog = scan_animation_catalog(project)
    used_ids = sorted({word & project.settings.metatile_id_mask for word in blocks})
    rows: list[AnimationRow] = []

    for metatile_id in used_ids:
        try:
            metatile = pair.metatile(metatile_id)
        except MapEditorError:
            continue
        definitions = _defs_for_metatile(catalog, pair, metatile)
        if not definitions:
            continue
        states = max(len(definition.frame_paths) for definition in definitions)
        manifest = {
            "kind": "metatile",
            "metatile_id": metatile_id,
            "tileset": pair.metatile_owner(metatile_id).name,
            "states": states,
            "animations": [
                {
                    "key": definition.key,
                    "tileset": definition.tileset,
                    "ranges": definition.ranges,
                    "frame_paths": definition.frame_paths,
                    "frame_count": len(definition.frame_paths),
                }
                for definition in definitions
            ],
        }
        rows.append(AnimationRow("metatile", states, manifest))

    behavior_values = project.metatile_behavior_values()
    door_behaviors = {
        value
        for name, value in behavior_values.items()
        if name
        in {
            "MB_ANIMATED_DOOR",
            "MB_NON_ANIMATED_DOOR",
            "MB_WATER_DOOR",
            "MB_PETALBURG_GYM_DOOR",
            "MB_CLOSED_SOOTOPOLIS_DOOR",
            "MB_TRICK_HOUSE_PUZZLE_DOOR",
        }
    }
    for door in catalog.door_defs:
        if door.metatile_id not in used_ids:
            continue
        if door.tileset:
            if door.tileset not in (pair.primary.name, pair.secondary.name):
                continue
            active_door = door
        else:
            if door.metatile_id < project.settings.num_metatiles_in_primary:
                owner = pair.primary
            else:
                owner = pair.secondary
            local = door.metatile_id - owner.global_metatile_base
            if not 0 <= local < len(owner.metatiles):
                continue
            if door_behaviors:
                attribute = pair.attribute(door.metatile_id)
                behavior = attribute[0] if attribute else 0
                if behavior not in door_behaviors:
                    continue
            active_door = replace(
                door,
                key=f"{owner.name}/{door.metatile_label}",
                tileset=owner.name,
                kind=owner.kind,
            )
        part_count = active_door.parts[0] * active_door.parts[1]
        door_patterns = _door_dest_patterns(project, active_door)
        for part in range(part_count):
            subtile_offsets: list[int] = []
            for subtile in range(4):
                tile_index = active_door.frame_offsets[0] + part * 4 + subtile
                pattern = (
                    door_patterns[tile_index]
                    if tile_index < len(door_patterns)
                    else bytes(64)
                )
                subtile_offsets.append(
                    16 if any(value >= 16 for value in pattern) else 0
                )
            manifest = {
                "kind": "door",
                "key": active_door.key,
                "metatile_id": active_door.metatile_id,
                "metatile_label": active_door.metatile_label,
                "tileset": active_door.tileset,
                "part": part,
                "parts": list(active_door.parts),
                "states": len(active_door.frame_offsets),
                "frame_path": active_door.frame_path,
                "frame_offsets": active_door.frame_offsets,
                "frame_tile_count": active_door.frame_tile_count,
                "palette_banks": active_door.palette_banks,
                "subtile_offsets": subtile_offsets,
            }
            rows.append(AnimationRow("door", len(active_door.frame_offsets), manifest))
    return rows


def export_animation_sheet(
    project: Project,
    pair: TilesetPair,
    blocks: list[int],
    directory,
) -> tuple[dict, object]:
    rows = sheet_rows(project, pair, blocks)
    columns = max((row.states for row in rows), default=1)
    sheet = Image.new("RGBA", (columns * CELL, max(1, len(rows)) * CELL), (0, 0, 0, 0))

    catalog = scan_animation_catalog(project)
    for row_index, row in enumerate(rows):
        for state in range(row.states):
            if row.kind == "metatile":
                metatile_id = row.data["metatile_id"]
                metatile = pair.metatile(metatile_id)
                definitions = [
                    catalog.by_key(animation["key"])
                    for animation in row.data["animations"]
                ]
                definitions = [
                    definition for definition in definitions if definition is not None
                ]
                cell = _render_metatile_state(
                    project, pair, metatile, definitions, state
                )
            else:
                door = next(
                    (
                        entry
                        for entry in catalog.door_defs
                        if entry.key == row.data["key"]
                    ),
                    None,
                )
                if door is None:
                    door = next(
                        (
                            entry
                            for entry in catalog.door_defs
                            if entry.metatile_id == row.data.get("metatile_id")
                            and entry.metatile_label == row.data.get("metatile_label")
                        ),
                        None,
                    )
                if door is None:
                    continue
                cell = _render_door_part(project, pair, door, state, row.data["part"])
            row.cells.append(cell)
            sheet.paste(
                Image.frombytes("RGBA", (CELL, CELL), cell),
                (state * CELL, row_index * CELL),
            )

    sheet_path = directory / ANIMATION_SHEET_NAME
    manifest_path = directory / ANIMATION_MANIFEST_NAME
    sheet.save(sheet_path, format="PNG")
    manifest = {
        "format": SHEET_FORMAT,
        "version": SHEET_VERSION,
        "cell_size": CELL,
        "columns": columns,
        "sheet_sha256": hashlib.sha256(sheet_path.read_bytes()).hexdigest(),
        "rows": [row.data for row in rows],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest, sheet


def _save_pattern_image(path: Path, patterns: list[bytes]) -> None:
    with Image.open(path) as opened:
        opened.load()
        width, height = opened.size
        palette = opened.getpalette() or []
    data = bytearray(width * height)
    tiles_per_row = width // TILE_SIZE
    for local, pattern in enumerate(patterns):
        if local * TILE_SIZE >= width * height:
            break
        tx = local % tiles_per_row
        ty = local // tiles_per_row
        for row in range(TILE_SIZE):
            dest = (ty * TILE_SIZE + row) * width + tx * TILE_SIZE
            data[dest : dest + TILE_SIZE] = pattern[
                row * TILE_SIZE : (row + 1) * TILE_SIZE
            ]
    image = Image.frombytes("P", (width, height), bytes(data))
    if len(palette) < 768:
        palette = palette + [0] * (768 - len(palette))
    image.putpalette(palette[:768])
    image.save(path, format="PNG")


def _set_pattern(
    updates: dict[int, bytes], key: int, pattern: bytes, description: str
) -> None:
    previous = updates.get(key)
    if previous is not None and previous != pattern:
        raise MapEditorError(
            f"Conflicting edits for {description}. Multiple sheet cells disagree."
        )
    updates[key] = pattern


def _cell_has_pixels(cell: bytes) -> bool:
    return any(cell[index] for index in range(3, len(cell), 4))


def _write_pattern_image_from_template(
    template: Path, destination: Path, patterns: list[bytes]
) -> None:
    with Image.open(template) as opened:
        opened.load()
        width, height = opened.size
        palette = opened.getpalette() or []
    tiles_per_row = width // TILE_SIZE
    rows = max(1, (len(patterns) + tiles_per_row - 1) // tiles_per_row)
    height = max(height, rows * TILE_SIZE)
    data = bytearray(width * height)
    for local, pattern in enumerate(patterns):
        if local * TILE_SIZE >= width * height:
            break
        tx = local % tiles_per_row
        ty = local // tiles_per_row
        for row in range(TILE_SIZE):
            dest = (ty * TILE_SIZE + row) * width + tx * TILE_SIZE
            data[dest : dest + TILE_SIZE] = pattern[
                row * TILE_SIZE : (row + 1) * TILE_SIZE
            ]
    image = Image.frombytes("P", (width, height), bytes(data))
    if len(palette) < 768:
        palette = palette + [0] * (768 - len(palette))
    image.putpalette(palette[:768])
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")


def _append_animation_frames_to_source(
    project: Project, definition: TilesetAnimationDef, new_paths: list[str]
) -> list[str]:
    if not definition.frame_array_name:
        raise MapEditorError(
            f"Cannot add states to {definition.key}: its frame array name is unknown."
        )
    source = project.root / "src" / "tileset_anims.c"
    if not source.is_file():
        raise MapEditorError(
            f"Cannot add states to {definition.key}: src/tileset_anims.c is missing."
        )
    text = source.read_text(encoding="utf-8", errors="replace")
    array_pattern = re.compile(
        re.escape(definition.frame_array_name) + r"\s*\[\s*\]\s*=\s*\{(.*?)\};",
        re.DOTALL,
    )
    match = array_pattern.search(text)
    if match is None:
        raise MapEditorError(
            f"Cannot add states to {definition.key}: frame array "
            f"{definition.frame_array_name} was not found."
        )
    existing_numbers = [
        int(value)
        for value in re.findall(r"_Frame(\d+)", match.group(1))
        if value.isdigit()
    ]
    next_number = max(existing_numbers, default=-1) + 1
    declarations: list[str] = []
    names: list[str] = []
    for path in new_paths:
        name = f"{definition.frame_array_name}_Frame{next_number}"
        next_number += 1
        names.append(name)
        declarations.append(f'const u16 {name}[] = INCGFX_U16("{path}", ".4bpp");')
    insert_at = match.start()
    declaration_text = "\n".join(declarations) + "\n"
    text = text[:insert_at] + declaration_text + text[insert_at:]
    match = array_pattern.search(text)
    if match is None:
        raise MapEditorError(
            f"Cannot add states to {definition.key}: frame array disappeared after editing."
        )
    body = match.group(1).rstrip().rstrip(",")
    new_body = body + ",\n" + ",\n".join(f"    {name}" for name in names) + ",\n"
    text = text[: match.start(1)] + new_body + text[match.end(1) :]
    write_text_atomic(source, text)
    return names


@contextmanager
def _using_palettes(pair, primary, secondary):
    current_primary = pair.primary.palettes
    current_secondary = pair.secondary.palettes
    pair.primary.palettes = primary
    pair.secondary.palettes = secondary
    try:
        yield
    finally:
        pair.primary.palettes = current_primary
        pair.secondary.palettes = current_secondary


def _cell_rgba(sheet: Image.Image, column: int, row: int) -> bytes:
    cell = sheet.crop(
        (column * CELL, row * CELL, (column + 1) * CELL, (row + 1) * CELL)
    ).convert("RGBA")
    return cell.tobytes()


def _ensure_palette_for_patch(
    pair: TilesetPair, patch: bytes, bank: int, description: str
) -> bool:
    mode = getattr(pair, "palette_mode", "smart")
    if mode not in ("smart", "replace"):
        return False
    colors = Counter(
        (patch[index], patch[index + 1], patch[index + 2])
        for index in range(0, len(patch), 4)
        if patch[index + 3]
    )
    if not colors:
        return False
    plan = plan_bank_colors(pair, bank, colors, mode)
    if not plan.replaced_slots:
        return False
    apply_palette_plan(pair, plan)
    return True


def _quantize_pattern(
    pair: TilesetPair, patch: bytes, palette: int, description: str
) -> bytes:
    def run():
        return pair.secondary.quantize_patch(
            patch,
            allowed_palettes=[palette],
            preferred_palette=palette,
            mode="error",
            palette_bank=pair.palette_bank(),
        )

    try:
        pattern, _, exact, substitutions = run()
    except MapEditorError as exc:
        if not _ensure_palette_for_patch(pair, patch, palette, description):
            raise MapEditorError(f"{description}: {exc}") from exc
        pattern, _, exact, substitutions = run()
    if not exact or substitutions:
        raise MapEditorError(
            f"The animated sheet uses colours that do not fit palette {palette} "
            f"for {description}."
        )
    return pattern


def _patch_door_palette_arrays(
    project: Project, updates: dict[str, dict[int, int]]
) -> bool:
    if not updates:
        return False
    source = project.root / "src" / "field_door.c"
    text = source.read_text(encoding="utf-8", errors="replace")
    changed = False
    for name, replacements in updates.items():
        pattern = re.compile(
            rf"(static\s+const\s+u8\s+{re.escape(name)}\[\]\s*=\s*\{{)([^}}]*)(\}};)",
            re.DOTALL,
        )
        match = pattern.search(text)
        if match is None:
            raise MapEditorError(
                f"Could not find the door palette array {name} in field_door.c."
            )
        values = [
            int(value.strip(), 0)
            for value in match.group(2).split(",")
            if value.strip()
        ]
        array_changed = False
        for index, value in replacements.items():
            if index < len(values) and values[index] != value:
                values[index] = value
                array_changed = True
        if array_changed:
            body = ", ".join(str(value) for value in values)
            text = text[: match.start(2)] + body + text[match.end(2) :]
            changed = True
    if changed:
        write_text_atomic(source, text)
    return changed


def _add_door_size_to_source(
    project: Project, door: DoorAnimationDef, offsets: list[int]
) -> str:
    source = project.root / "src" / "field_door.c"
    text = source.read_text(encoding="utf-8", errors="replace")
    width, height = door.parts
    parts = width * height
    raw = door.metatile_label.removeprefix("METATILE_")
    suffix_base = (
        "".join(
            word[:1].upper() + word[1:]
            for word in re.split(r"[^A-Za-z0-9]+", raw)
            if word
        )
        or "CustomDoor"
    )
    if width == 2 and height == 2:
        if "LEFT" in door.size_name:
            dimension = "2x2_LEFT"
            base_case = "DOOR_SIZE_2x2_LEFT"
        elif "RIGHT" in door.size_name:
            dimension = "2x2_RIGHT"
            base_case = "DOOR_SIZE_2x2_RIGHT"
        else:
            dimension = "2x2"
            base_case = "DOOR_SIZE_2x2_LEFT"
    else:
        dimension = f"{width}x{height}"
        base_case = f"DOOR_SIZE_{dimension}"
    suffix = suffix_base
    counter = 2
    while True:
        new_size = f"DOOR_SIZE_{dimension}_{suffix}"
        open_name = f"sDoorAnimFrames_Open{suffix}"
        close_name = f"sDoorAnimFrames_Close{suffix}"
        if new_size not in text and open_name not in text and close_name not in text:
            break
        suffix = f"{suffix_base}{counter}"
        counter += 1

    open_lines = [f"    {{{parts}, -1}},"]
    open_lines.extend(
        f"    {{{parts}, {offset} * TILE_SIZE_4BPP}}," for offset in offsets
    )
    close_lines = [
        f"    {{{parts}, {offset} * TILE_SIZE_4BPP}}," for offset in reversed(offsets)
    ]
    close_lines.append(f"    {{{parts}, -1}},")
    arrays = (
        f"static const struct DoorAnimFrame {open_name}[] = {{\n"
        + "\n".join(open_lines)
        + "\n    {}\n};\n\n"
        f"static const struct DoorAnimFrame {close_name}[] = {{\n"
        + "\n".join(close_lines)
        + "\n    {}\n};\n\n"
    )
    anchor = text.find("static const struct DoorSizeInfo sDoorSizeInfo")
    if anchor < 0:
        raise MapEditorError("Could not find sDoorSizeInfo in field_door.c.")
    text = text[:anchor] + arrays + text[anchor:]

    enum_match = re.search(r"\n(\s*)DOOR_SIZE_COUNT,", text)
    if enum_match is None:
        raise MapEditorError("Could not find DOOR_SIZE_COUNT in field_door.c.")
    text = (
        text[: enum_match.start()]
        + f"\n{enum_match.group(1)}{new_size},"
        + text[enum_match.start() :]
    )

    size_match = re.search(
        r"(static const struct DoorSizeInfo sDoorSizeInfo\[DOOR_SIZE_COUNT\]\s*=\s*\{)"
        r"(.*?)(\n\};)",
        text,
        re.DOTALL,
    )
    if size_match is None:
        raise MapEditorError("Could not find the sDoorSizeInfo table in field_door.c.")
    entry = (
        f"\n    [{new_size}] =\n"
        "    {\n"
        f"        .openAnimFrames = {open_name},\n"
        f"        .closeAnimFrames = {close_name},\n"
        "    },"
    )
    text = text[: size_match.start(3)] + entry + text[size_match.start(3) :]

    entry_match = re.search(
        rf"(\{{[^{{}}]*\.metatileNum\s*=\s*{re.escape(door.metatile_label)}\s*,"
        r"[^{}]*\})",
        text,
        re.DOTALL,
    )
    if entry_match is None:
        raise MapEditorError(
            f"Could not find the DoorGraphics entry for {door.metatile_label}."
        )
    block = entry_match.group(1)
    new_block = re.sub(r"\.size\s*=\s*\w+\s*,", f".size = {new_size},", block, count=1)
    text = text[: entry_match.start(1)] + new_block + text[entry_match.end(1) :]

    if width == 2 and height == 2:
        text = text.replace(
            "gfx->size == DOOR_SIZE_2x2_LEFT || gfx->size == DOOR_SIZE_2x2_RIGHT",
            "gfx->size == DOOR_SIZE_2x2_LEFT || "
            "gfx->size == DOOR_SIZE_2x2_RIGHT || "
            f"gfx->size == {new_size}",
            1,
        )
    text = text.replace(
        f"    case {base_case}:",
        f"    case {base_case}:\n    case {new_size}:",
    )
    write_text_atomic(source, text)
    return new_size


def _refresh_door_sheet_rows(
    project: Project,
    pair: TilesetPair,
    result: AnimationApplyResult,
    sheet: Image.Image,
    manifest: dict,
    rows: list[dict],
    changed_keys: set[str],
    sheet_path: Path,
    manifest_path: Path,
    dry_run: bool,
) -> None:
    if not changed_keys:
        return
    doors = {door.key: door for door in scan_animation_catalog(project).door_defs}
    refreshed = 0
    for row_index, row in enumerate(rows):
        if row.get("kind") != "door" or row.get("key") not in changed_keys:
            continue
        door = doors.get(row["key"])
        if door is None:
            continue
        part = int(row.get("part", 0))
        states = min(int(row.get("states", 0)), len(door.frame_offsets))
        row["palette_banks"] = list(door.palette_banks)
        for state in range(states):
            cell = _render_door_part(project, pair, door, state, part)
            sheet.paste(
                Image.frombytes("RGBA", (CELL, CELL), cell),
                (state * CELL, row_index * CELL),
            )
        refreshed += 1
    if refreshed and not dry_run:
        sheet.save(sheet_path, format="PNG")
        manifest["sheet_sha256"] = hashlib.sha256(sheet_path.read_bytes()).hexdigest()
        write_text_atomic(manifest_path, json.dumps(manifest, indent=2) + "\n")
        result.changed = True
        result.warnings.append(
            f"Refreshed {refreshed} door sheet row(s) to match the updated palette "
            "banks."
        )


def _quantize_any_pattern(
    pair: TilesetPair, patch: bytes, preferred_bank: int, description: str
) -> tuple[bytes, int]:
    banks = pair.palette_bank()
    limit = min(len(banks), pair.project.settings.num_pals_total)
    candidates = [preferred_bank] + [
        index for index in range(limit) if index != preferred_bank
    ]
    failures: list[str] = []
    for bank in candidates:
        try:
            pattern, _, exact, substitutions = pair.secondary.quantize_patch(
                patch,
                allowed_palettes=[bank],
                preferred_palette=bank,
                mode="error",
                palette_bank=pair.palette_bank(),
            )
        except MapEditorError as exc:
            failures.append(str(exc))
            continue
        if exact and not substitutions:
            return pattern, bank
    if _ensure_palette_for_patch(pair, patch, preferred_bank, description):
        for bank in candidates:
            pattern, _, exact, substitutions = pair.secondary.quantize_patch(
                patch,
                allowed_palettes=[bank],
                preferred_palette=bank,
                mode="error",
                palette_bank=pair.palette_bank(),
            )
            if exact and not substitutions:
                return pattern, bank
    detail = failures[0] if failures else "no palette matched"
    raise MapEditorError(
        f"{description}: the edited colours do not fit any tileset palette ({detail})."
    )


def _write_new_pattern_image(
    destination: Path,
    patterns: list[bytes],
    palette_source: Path,
    *,
    tiles_per_row: int = 2,
) -> None:
    with Image.open(palette_source) as opened:
        palette = opened.getpalette() or []
    rows = max(1, (len(patterns) + tiles_per_row - 1) // tiles_per_row)
    width = tiles_per_row * TILE_SIZE
    height = rows * TILE_SIZE
    data = bytearray(width * height)
    for local, pattern in enumerate(patterns):
        tx = local % tiles_per_row
        ty = local // tiles_per_row
        for row in range(TILE_SIZE):
            dest = (ty * TILE_SIZE + row) * width + tx * TILE_SIZE
            data[dest : dest + TILE_SIZE] = pattern[
                row * TILE_SIZE : (row + 1) * TILE_SIZE
            ]
    image = Image.frombytes("P", (width, height), bytes(data))
    if len(palette) < 768:
        palette = palette + [0] * (768 - len(palette))
    image.putpalette(palette[:768])
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")


def _function_span(text: str, header: str) -> tuple[int, int] | None:
    start = text.find(header)
    if start < 0:
        return None
    brace = text.find("{", start)
    if brace < 0:
        return None
    depth = 1
    index = brace + 1
    while index < len(text) and depth:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    if depth:
        return None
    return start, index


def _add_new_animation_to_source(
    project: Project,
    tileset,
    name: str,
    global_start: int,
    frame_paths: list[str],
    divisor: int,
    dry_run: bool = False,
) -> TilesetAnimationDef:
    source = project.root / "src" / "tileset_anims.c"
    text = source.read_text(encoding="utf-8", errors="replace")
    suffix = tileset.name.removeprefix("gTileset_")
    raw = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    words = [word for word in raw.split("_") if word]
    if not words:
        words = ["custom", "animation"]
    base_camel = "".join(word[:1].upper() + word[1:] for word in words)
    camel = base_camel
    counter = 2
    while (
        f"gTilesetAnims_{suffix}_{camel}" in text
        or f"QueueAnimTiles_{suffix}_{camel}" in text
    ):
        camel = f"{base_camel}{counter}"
        counter += 1
    array_name = f"gTilesetAnims_{suffix}_{camel}"
    queue_name = f"QueueAnimTiles_{suffix}_{camel}"
    init_name = f"InitTilesetAnim_{suffix}"
    anim_name = f"TilesetAnim_{suffix}"
    frame_names = [f"{array_name}_Frame{index}" for index in range(len(frame_paths))]

    declarations = "\n".join(
        f'const u16 {frame_name}[] = INCGFX_U16("{path}", ".4bpp");'
        for frame_name, path in zip(frame_names, frame_paths)
    )
    array_body = ",\n".join(f"    {frame_name}" for frame_name in frame_names)
    array_block = f"const u16 *const {array_name}[] = {{\n{array_body},\n}};\n"
    queue_block = (
        f"static void {queue_name}(u16 timer)\n"
        "{\n"
        f"    u16 i = timer % ARRAY_COUNT({array_name});\n"
        f"    AppendTilesetAnimToBuffer({array_name}[i], "
        f"(u16 *)(BG_VRAM + TILE_OFFSET_4BPP({global_start})), "
        "4 * TILE_SIZE_4BPP);\n"
        "}\n"
    )
    insert_anchor = text.find("void InitTilesetAnimations(void)")
    if insert_anchor < 0:
        raise MapEditorError(
            "Could not find InitTilesetAnimations in src/tileset_anims.c."
        )
    block = (
        f"static void {queue_name}(u16);\n\n"
        f"{declarations}\n"
        f"{array_block}\n"
        f"{queue_block}\n"
    )
    text = text[:insert_anchor] + block + text[insert_anchor:]

    call = (
        f"    if (timer % {divisor} == {{residue}})\n"
        f"        {queue_name}(timer / {divisor});\n"
    )

    def add_call(function_name: str) -> bool:
        nonlocal text
        span = _function_span(text, f"static void {function_name}(u16 timer)")
        if span is None:
            return False
        start, end = span
        body = text[start:end]
        used = {
            int(value) for value in re.findall(r"timer\s*%\s*\d+\s*==\s*(\d+)", body)
        }
        residue = next((value for value in range(divisor) if value not in used), 0)
        closing = text.rfind("}", start, end)
        text = text[:closing] + call.format(residue=residue) + text[closing:]
        return True

    if not add_call(anim_name):
        init_span = _function_span(text, f"void {init_name}(void)")
        callback_name = ""
        if init_span is not None:
            init_body = text[init_span[0] : init_span[1]]
            callback = re.search(
                r"s(?:Primary|Secondary)TilesetAnimCallback\s*=\s*(\w+)\s*;",
                init_body,
            )
            if callback is not None and callback.group(1) != "NULL":
                callback_name = callback.group(1)
        if callback_name:
            if not add_call(callback_name):
                raise MapEditorError(
                    f"Could not find the callback function {callback_name}."
                )
        else:
            new_function = (
                f"static void {anim_name}(u16 timer)\n"
                "{\n" + call.format(residue=0) + "}\n\n"
            )
            fresh_anchor = text.find("void InitTilesetAnimations(void)")
            text = text[:fresh_anchor] + new_function + text[fresh_anchor:]
            kind_name = "Primary" if tileset.kind == "primary" else "Secondary"
            init_span = _function_span(text, f"void {init_name}(void)")
            if init_span is None:
                init_block = (
                    f"void {init_name}(void)\n"
                    "{\n"
                    f"    s{kind_name}TilesetAnimCounter = 0;\n"
                    f"    s{kind_name}TilesetAnimCounterMax = {divisor};\n"
                    f"    s{kind_name}TilesetAnimCallback = {anim_name};\n"
                    "}\n\n"
                )
                init_anchor = text.find("void InitTilesetAnimations(void)")
                text = text[:init_anchor] + init_block + text[init_anchor:]
                header = project.root / "include" / "tileset_anims.h"
                if header.is_file():
                    header_text = header.read_text(encoding="utf-8", errors="replace")
                    if f"void {init_name}(void);" not in header_text:
                        marker = header_text.rfind("#endif")
                        declaration = f"void {init_name}(void);\n"
                        if marker >= 0:
                            header_text = (
                                header_text[:marker]
                                + declaration
                                + header_text[marker:]
                            )
                        else:
                            header_text += declaration
                        if not dry_run:
                            write_text_atomic(header, header_text)
                headers = project.root / "src" / "data" / "tilesets" / "headers.h"
                if not headers.is_file():
                    raise MapEditorError(
                        "Cannot register the new tileset animation: "
                        "src/data/tilesets/headers.h is missing."
                    )
                header_text = headers.read_text(encoding="utf-8", errors="replace")
                table_span = _function_span(
                    header_text, f"const struct Tileset {tileset.name} ="
                )
                if table_span is None:
                    raise MapEditorError(f"Could not find {tileset.name} in headers.h.")
                block_start, block_end = table_span
                block_text = header_text[block_start:block_end]
                line = f"    .callback = {init_name},\n"
                callback_match = re.search(r"\.callback\s*=\s*\w+\s*,\n", block_text)
                if callback_match:
                    block_text = (
                        block_text[: callback_match.start()]
                        + line
                        + block_text[callback_match.end() :]
                    )
                else:
                    closing = block_text.rfind("};")
                    block_text = block_text[:closing] + line + block_text[closing:]
                header_text = (
                    header_text[:block_start] + block_text + header_text[block_end:]
                )
                if not dry_run:
                    write_text_atomic(headers, header_text)
            else:
                init_start, init_end = init_span
                init_body = text[init_start:init_end]
                init_body = re.sub(
                    r"(s(?:Primary|Secondary)TilesetAnimCounterMax\s*=\s*)[^;]+;",
                    rf"\g<1>{divisor};",
                    init_body,
                )
                init_body = re.sub(
                    r"(s(?:Primary|Secondary)TilesetAnimCallback\s*=\s*)NULL;",
                    rf"\g<1>{anim_name};",
                    init_body,
                )
                text = text[:init_start] + init_body + text[init_end:]

    if not dry_run:
        write_text_atomic(source, text)
    return TilesetAnimationDef(
        key=f"{tileset.name}/{camel.lower()}",
        tileset=tileset.name,
        kind=tileset.kind,
        name=camel,
        ranges=[(global_start, 4)],
        frame_paths=list(frame_paths),
        frame_sequence=list(range(len(frame_paths))),
        frame_names=frame_names,
        frame_array_name=array_name,
        frame_declarations=dict(zip(frame_names, frame_paths)),
    )


def _refresh_animation_sheet_rows(
    project: Project,
    pair: TilesetPair,
    result: AnimationApplyResult,
    sheet: Image.Image,
    manifest: dict,
    sheet_path: Path,
    manifest_path: Path,
) -> None:
    if not pair.palette_rewrites:
        return
    catalog = scan_animation_catalog(project)
    rows = manifest.get("rows", [])
    refreshed = 0
    for row_index, row in enumerate(rows):
        if row.get("kind") == "metatile":
            try:
                metatile = pair.metatile(int(row["metatile_id"]))
            except (MapEditorError, KeyError, ValueError):
                continue
            definitions = [
                definition
                for definition in (
                    catalog.by_key(animation["key"])
                    for animation in row.get("animations", [])
                )
                if definition is not None
            ]
            if not definitions:
                continue
            for state in range(min(int(row.get("states", 0)), 256)):
                cell = _render_metatile_state(
                    project, pair, metatile, definitions, state
                )
                sheet.paste(
                    Image.frombytes("RGBA", (CELL, CELL), cell),
                    (state * CELL, row_index * CELL),
                )
            refreshed += 1
        elif row.get("kind") == "door":
            door = next(
                (entry for entry in catalog.door_defs if entry.key == row.get("key")),
                None,
            )
            if door is None:
                door = next(
                    (
                        entry
                        for entry in catalog.door_defs
                        if entry.metatile_id == row.get("metatile_id")
                        and entry.metatile_label == row.get("metatile_label")
                    ),
                    None,
                )
            if door is None:
                continue
            part = int(row.get("part", 0))
            for state in range(min(int(row.get("states", 0)), len(door.frame_offsets))):
                cell = _render_door_part(project, pair, door, state, part)
                sheet.paste(
                    Image.frombytes("RGBA", (CELL, CELL), cell),
                    (state * CELL, row_index * CELL),
                )
            refreshed += 1
    if not refreshed:
        return
    sheet.save(sheet_path, format="PNG")
    manifest["sheet_sha256"] = hashlib.sha256(sheet_path.read_bytes()).hexdigest()
    write_text_atomic(manifest_path, json.dumps(manifest, indent=2) + "\n")
    result.changed = True
    result.warnings.append(
        f"Refreshed {refreshed} animated sheet row(s) after palette rewrites."
    )


def _append_new_animation_rows(
    project: Project,
    pair: TilesetPair,
    result: AnimationApplyResult,
    sheet: Image.Image,
    manifest: dict,
    sheet_path: Path,
    manifest_path: Path,
    row_count: int,
    new_metatile_rows: list[tuple[int, int, object, int, object, list]],
    new_door_rows: list[tuple[int, DoorAnimationDef]],
) -> None:
    extra_row_count = sheet.height // CELL - row_count
    if extra_row_count <= 0:
        return
    extras: list[dict | None] = [None] * extra_row_count
    rendered: dict[int, list[bytes]] = {}
    ignored_columns: list[str] = []

    for row_y, new_id, tileset, painted, definition, _paths in new_metatile_rows:
        metatile = tileset.metatile_at(new_id)
        states = len(definition.frame_paths)
        if painted > states:
            ignored_columns.append(
                f"row {row_y + 1}: {painted - states} extra column(s) beyond "
                f"{definition.key}'s {states} frame(s)"
            )
        cells = [
            _render_metatile_state(project, pair, metatile, [definition], state)
            for state in range(states)
        ]
        extras[row_y - row_count] = {
            "kind": "metatile",
            "metatile_id": new_id,
            "tileset": tileset.name,
            "states": states,
            "animations": [
                {
                    "key": definition.key,
                    "tileset": definition.tileset,
                    "ranges": definition.ranges,
                    "frame_paths": definition.frame_paths,
                    "frame_count": len(definition.frame_paths),
                }
            ],
        }
        rendered[row_y - row_count] = cells

    for start_extra, door in new_door_rows:
        parts = door.parts[0] * door.parts[1]
        states = len(door.frame_offsets)
        patterns = _door_dest_patterns(project, door)
        for part in range(parts):
            extra_index = start_extra + part
            if extra_index >= extra_row_count:
                break
            subtile_offsets: list[int] = []
            for subtile in range(4):
                tile_index = door.frame_offsets[0] + part * 4 + subtile
                pattern = (
                    patterns[tile_index] if tile_index < len(patterns) else bytes(64)
                )
                subtile_offsets.append(
                    16 if any(value >= 16 for value in pattern) else 0
                )
            extras[extra_index] = {
                "kind": "door",
                "key": door.key,
                "metatile_id": door.metatile_id,
                "metatile_label": door.metatile_label,
                "tileset": door.tileset,
                "part": part,
                "parts": list(door.parts),
                "states": states,
                "frame_path": door.frame_path,
                "frame_offsets": door.frame_offsets,
                "frame_tile_count": door.frame_tile_count,
                "palette_banks": door.palette_banks,
                "subtile_offsets": subtile_offsets,
            }
            rendered[extra_index] = [
                _render_door_part(project, pair, door, state, part)
                for state in range(states)
            ]
            row_y = row_count + extra_index
            painted = 0
            for column in range(sheet.width // CELL):
                if _cell_has_pixels(_cell_rgba(sheet, column, row_y)):
                    painted = max(painted, column + 1)
            if painted > states:
                ignored_columns.append(
                    f"row {row_y + 1}: {painted - states} extra column(s) beyond "
                    f"the door's {states} state(s)"
                )

    max_states = max(
        [sheet.width // CELL] + [len(cells) for cells in rendered.values()],
        default=sheet.width // CELL,
    )
    if max_states > sheet.width // CELL:
        expanded = Image.new("RGBA", (max_states * CELL, sheet.height), (0, 0, 0, 0))
        expanded.paste(sheet, (0, 0))
        sheet = expanded

    transparent = Image.new("RGBA", (sheet.width, CELL), (0, 0, 0, 0))
    for extra_index, cells in rendered.items():
        row_y = row_count + extra_index
        sheet.paste(transparent, (0, row_y * CELL))
        for state, cell in enumerate(cells):
            sheet.paste(
                Image.frombytes("RGBA", (CELL, CELL), cell),
                (state * CELL, row_y * CELL),
            )

    manifest_rows = list(manifest.get("rows", []))
    manifest_rows.extend(extra or {"kind": "empty", "states": 0} for extra in extras)
    manifest["rows"] = manifest_rows
    manifest["columns"] = sheet.width // CELL
    sheet.save(sheet_path, format="PNG")
    manifest["sheet_sha256"] = hashlib.sha256(sheet_path.read_bytes()).hexdigest()
    write_text_atomic(manifest_path, json.dumps(manifest, indent=2) + "\n")
    result.changed = True
    result.warnings.append(
        f"Added {len(rendered)} new row(s) to {manifest_path.name} and refreshed "
        f"{sheet_path.name}; the next import will not ask about them again."
    )
    if ignored_columns:
        result.warnings.append(
            "New animation rows had unusable extra columns, which were cleared: "
            + "; ".join(ignored_columns)
            + "."
        )


def _quantize_door_pattern(
    pair: TilesetPair,
    patch: bytes,
    preferred_bank: int,
    description: str,
) -> tuple[bytes, int]:
    banks = pair.palette_bank()
    limit = min(len(banks), pair.project.settings.num_pals_total)
    candidates = [preferred_bank] + [
        index for index in range(limit) if index != preferred_bank
    ]
    failures: list[str] = []
    for bank in candidates:
        try:
            pattern, _, exact, substitutions = pair.secondary.quantize_patch(
                patch,
                allowed_palettes=[bank],
                preferred_palette=bank,
                mode="error",
                palette_bank=pair.palette_bank(),
            )
        except MapEditorError as exc:
            failures.append(str(exc))
            continue
        if exact and not substitutions:
            return pattern, bank
    if _ensure_palette_for_patch(pair, patch, preferred_bank, description):
        for bank in candidates:
            pattern, _, exact, substitutions = pair.secondary.quantize_patch(
                patch,
                allowed_palettes=[bank],
                preferred_palette=bank,
                mode="error",
                palette_bank=pair.palette_bank(),
            )
            if exact and not substitutions:
                return pattern, bank
    detail = failures[0] if failures else "no palette matched"
    raise MapEditorError(
        f"{description}: the edited colours do not fit any tileset palette ({detail})."
    )


def apply_animation_sheet(
    project: Project,
    pair: TilesetPair,
    directory: Path,
    *,
    dry_run: bool = False,
    backup_func=None,
    prompt_extra_row=None,
) -> AnimationApplyResult:
    result = AnimationApplyResult()
    manifest_path = directory / ANIMATION_MANIFEST_NAME
    sheet_path = directory / ANIMATION_SHEET_NAME
    if not manifest_path.is_file() or not sheet_path.is_file():
        return result
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MapEditorError(f"Could not read {manifest_path}: {exc}") from exc
    if manifest.get("format") != SHEET_FORMAT:
        raise MapEditorError(f"{manifest_path} is not a Poryeasy animation sheet.")
    expected_hash = manifest.get("sheet_sha256")
    if (
        expected_hash
        and hashlib.sha256(sheet_path.read_bytes()).hexdigest() == expected_hash
    ):
        return result
    with Image.open(sheet_path) as opened:
        opened.load()
        sheet = opened.convert("RGBA")
    columns = int(manifest.get("columns", 0))
    rows = manifest.get("rows", [])
    if sheet.width < columns * CELL or sheet.height < len(rows) * CELL:
        raise MapEditorError(
            f"{sheet_path} is smaller than its manifest expects "
            f"({columns} columns, {len(rows)} rows)."
        )
    sheet_columns = sheet.width // CELL

    catalog = scan_animation_catalog(project)
    original_primary_palettes = [list(colors) for colors in pair.primary.palettes]
    original_secondary_palettes = [list(colors) for colors in pair.secondary.palettes]
    frame_updates: dict[str, dict[int, dict[int, bytes]]] = {}
    static_updates: dict[int, bytes] = {}
    door_updates: dict[str, dict[int, bytes]] = {}
    door_new_patterns: dict[str, dict[int, dict[int, bytes]]] = {}
    door_new_state_count: dict[str, int] = {}
    door_palette_updates: dict[str, dict[int, int]] = {}
    original_door_banks: dict[str, list[int]] = {}
    doors_to_refresh: set[str] = set()
    door_by_path = {}
    new_metatile_rows: list[tuple[int, int, object, int, str, list]] = []
    new_door_rows: list[tuple[int, DoorAnimationDef]] = []
    new_animation_paths: list[Path] = []

    for row_index, row in enumerate(rows):
        if row.get("kind") == "metatile":
            metatile_id = int(row["metatile_id"])
            try:
                metatile = pair.metatile(metatile_id)
            except MapEditorError:
                continue
            owner = pair.metatile_owner(metatile_id)
            row_definitions = [
                definition
                for definition in (
                    catalog.by_key(animation["key"])
                    for animation in row.get("animations", [])
                )
                if definition is not None
            ]
            if not row_definitions:
                continue
            states_total = int(row.get("states", 0))
            for state in range(states_total):
                cell = _cell_rgba(sheet, state, row_index)
                with _using_palettes(
                    pair, original_primary_palettes, original_secondary_palettes
                ):
                    rendered = _render_metatile_state(
                        project, pair, metatile, row_definitions, state
                    )
                if rendered == cell:
                    continue
                for definition in row_definitions:
                    frame_count = len(definition.frame_paths)
                    if state >= frame_count:
                        continue
                    for layer in range(3):
                        entries = owner.entries_for_layer(metatile, layer)
                        for subtile, entry in enumerate(entries):
                            if entry is None:
                                continue
                            local_tile = None
                            range_start = 0
                            for start, count in definition.ranges:
                                if start <= entry.tile_index < start + count:
                                    local_tile = entry.tile_index - start
                                    range_start = start
                                    break
                            if local_tile is None:
                                continue
                            sx = (subtile % 2) * TILE_SIZE
                            sy = (subtile // 2) * TILE_SIZE
                            patch = patch_from_rgba(cell, CELL, sx, sy, TILE_SIZE)
                            pattern = _quantize_pattern(
                                pair,
                                patch,
                                entry.palette,
                                f"{definition.key} frame {state} tile {local_tile}",
                            )
                            canonical = flip_pattern(pattern, entry.hflip, entry.vflip)
                            frame_map = frame_updates.setdefault(
                                definition.key, {}
                            ).setdefault(state, {})
                            _set_pattern(
                                frame_map,
                                local_tile,
                                canonical,
                                f"{definition.key} frame {state} tile {local_tile}",
                            )
                            if state == 0:
                                _set_pattern(
                                    static_updates,
                                    range_start + local_tile,
                                    canonical,
                                    f"static tile 0x{range_start + local_tile:03X}",
                                )
            extra_last = -1
            for column in range(states_total, sheet_columns):
                if _cell_has_pixels(_cell_rgba(sheet, column, row_index)):
                    extra_last = column
            if extra_last >= states_total:
                if len(row_definitions) != 1:
                    result.warnings.append(
                        f"Metatile 0x{metatile_id:03X}: added states are only supported "
                        "for rows with a single animation; extra columns were ignored."
                    )
                else:
                    definition = row_definitions[0]
                    frame_count = len(definition.frame_paths)
                    if states_total != frame_count:
                        result.warnings.append(
                            f"{definition.key}: added states start after column "
                            f"{states_total}, but the animation has {frame_count} frames; "
                            "extra columns were ignored."
                        )
                    else:
                        for state in range(states_total, extra_last + 1):
                            cell = _cell_rgba(sheet, state, row_index)
                            for layer in range(3):
                                entries = owner.entries_for_layer(metatile, layer)
                                for subtile, entry in enumerate(entries):
                                    if entry is None:
                                        continue
                                    local_tile = None
                                    for start, count in definition.ranges:
                                        if start <= entry.tile_index < start + count:
                                            local_tile = entry.tile_index - start
                                            break
                                    if local_tile is None:
                                        continue
                                    sx = (subtile % 2) * TILE_SIZE
                                    sy = (subtile // 2) * TILE_SIZE
                                    patch = patch_from_rgba(
                                        cell, CELL, sx, sy, TILE_SIZE
                                    )
                                    pattern = _quantize_pattern(
                                        pair,
                                        patch,
                                        entry.palette,
                                        f"{definition.key} new frame {state}",
                                    )
                                    canonical = flip_pattern(
                                        pattern, entry.hflip, entry.vflip
                                    )
                                    _set_pattern(
                                        frame_updates.setdefault(
                                            definition.key, {}
                                        ).setdefault(state, {}),
                                        local_tile,
                                        canonical,
                                        f"{definition.key} new frame {state} tile {local_tile}",
                                    )

        elif row.get("kind") == "door":
            door = next(
                (entry for entry in catalog.door_defs if entry.key == row["key"]),
                None,
            )
            if door is None:
                door = next(
                    (
                        entry
                        for entry in catalog.door_defs
                        if entry.metatile_id == row.get("metatile_id")
                        and entry.metatile_label == row.get("metatile_label")
                    ),
                    None,
                )
            if door is None:
                result.warnings.append(
                    f"Door animation {row['key']} is no longer present; skipped."
                )
                continue
            part = int(row.get("part", 0))
            door_by_path[door.frame_path] = door
            original_banks = original_door_banks.setdefault(
                door.key, list(door.palette_banks)
            )
            compare_door = replace(door, palette_banks=original_banks)
            subtile_offsets = row.get(
                "subtile_offsets", [int(row.get("index_offset", 0))] * 4
            )
            states = min(int(row.get("states", 0)), len(door.frame_offsets))
            for state in range(states):
                cell = _cell_rgba(sheet, state, row_index)
                with _using_palettes(
                    pair, original_primary_palettes, original_secondary_palettes
                ):
                    rendered_door = _render_door_part(
                        project, pair, compare_door, state, part
                    )
                if rendered_door == cell:
                    continue
                start = door.frame_offsets[state] + part * 4
                for subtile in range(4):
                    bank_index = part * 4 + subtile
                    palette_bank = (
                        door.palette_banks[bank_index]
                        if bank_index < len(door.palette_banks)
                        else 1
                    )
                    sx = (subtile % 2) * TILE_SIZE
                    sy = (subtile // 2) * TILE_SIZE
                    patch = patch_from_rgba(cell, CELL, sx, sy, TILE_SIZE)
                    pattern, chosen_bank = _quantize_door_pattern(
                        pair,
                        patch,
                        palette_bank,
                        f"{door.key} state {state} part {part} subtile {subtile}",
                    )
                    if chosen_bank != palette_bank:
                        door_palette_updates.setdefault(door.palettes_var, {})[
                            bank_index
                        ] = chosen_bank
                        if bank_index < len(door.palette_banks):
                            door.palette_banks[bank_index] = chosen_bank
                        doors_to_refresh.add(door.key)
                    offset = (
                        subtile_offsets[subtile]
                        if subtile < len(subtile_offsets)
                        else 0
                    )
                    stored = bytes(
                        (offset + value) if value else 0 for value in pattern
                    )
                    _set_pattern(
                        door_updates.setdefault(door.frame_path, {}),
                        start + subtile,
                        stored,
                        f"{door.key} state {state} part {part}",
                    )
            extra_last = -1
            for column in range(states, sheet_columns):
                if _cell_has_pixels(_cell_rgba(sheet, column, row_index)):
                    extra_last = column
            if extra_last >= states:
                for state in range(states, extra_last + 1):
                    cell = _cell_rgba(sheet, state, row_index)
                    new_offset = (
                        door.frame_offsets[-1]
                        + (state - states + 1) * door.frame_tile_count
                    )
                    for subtile in range(4):
                        bank_index = part * 4 + subtile
                        palette_bank = (
                            door.palette_banks[bank_index]
                            if bank_index < len(door.palette_banks)
                            else 1
                        )
                        sx = (subtile % 2) * TILE_SIZE
                        sy = (subtile // 2) * TILE_SIZE
                        patch = patch_from_rgba(cell, CELL, sx, sy, TILE_SIZE)
                        pattern, chosen_bank = _quantize_door_pattern(
                            pair,
                            patch,
                            palette_bank,
                            f"{door.key} new state {state} part {part} "
                            f"subtile {subtile}",
                        )
                        if chosen_bank != palette_bank:
                            door_palette_updates.setdefault(door.palettes_var, {})[
                                bank_index
                            ] = chosen_bank
                            if bank_index < len(door.palette_banks):
                                door.palette_banks[bank_index] = chosen_bank
                            doors_to_refresh.add(door.key)
                        offset = (
                            subtile_offsets[subtile]
                            if subtile < len(subtile_offsets)
                            else 0
                        )
                        stored = bytes(
                            (offset + value) if value else 0 for value in pattern
                        )
                        _set_pattern(
                            door_new_patterns.setdefault(door.key, {}).setdefault(
                                state, {}
                            ),
                            new_offset + part * 4 + subtile,
                            stored,
                            f"{door.key} new state {state} part {part}",
                        )
                door_new_state_count[door.key] = max(
                    door_new_state_count.get(door.key, 0), extra_last + 1
                )
    extra_row_count = sheet.height // CELL - len(rows)
    door_plans: list[NewDoorPlan] = []
    used_door_names: set[str] = set()
    extra_index = 0
    while extra_index < extra_row_count:
        row_y = len(rows) + extra_index
        cells: list[tuple[int, bytes]] = []
        for column in range(sheet_columns):
            cell = _cell_rgba(sheet, column, row_y)
            if _cell_has_pixels(cell):
                cells.append((column, cell))
        if not cells:
            extra_index += 1
            continue
        if prompt_extra_row is None:
            result.warnings.append(
                f"Animated sheet row {row_y} has no manifest entry and was ignored. "
                "Re-export after adding the animated metatile to the map, or run "
                "interactively to define it."
            )
            extra_index += 1
            continue
        choice = prompt_extra_row(
            extra_index, max(column for column, _cell in cells) + 1
        )
        if not choice or choice.get("kind") == "skip":
            extra_index += 1
            continue
        if choice.get("kind") == "door":
            base = next(
                (door for door in catalog.door_defs if door.key == choice.get("base")),
                None,
            )
            if base is None or base.tileset not in (
                pair.primary.name,
                pair.secondary.name,
            ):
                result.warnings.append(
                    f"Unknown or unavailable base door '{choice.get('base')}' for "
                    f"extra sheet row {row_y}; ignored."
                )
                extra_index += 1
                continue
            part_count = base.parts[0] * base.parts[1]
            part_rows = [cells]
            consumed = 1
            while (
                part_count > len(part_rows) and extra_index + consumed < extra_row_count
            ):
                next_y = len(rows) + extra_index + consumed
                next_cells: list[tuple[int, bytes]] = []
                for column in range(sheet_columns):
                    cell = _cell_rgba(sheet, column, next_y)
                    if _cell_has_pixels(cell):
                        next_cells.append((column, cell))
                part_rows.append(next_cells)
                consumed += 1
            if len(part_rows) < part_count:
                result.warnings.append(
                    f"New door from sheet row {row_y} needs {part_count} consecutive "
                    f"rows (one per part) but only {len(part_rows)} are available; "
                    "ignored."
                )
                extra_index += consumed
                continue
            name = str(choice.get("name") or f"custom_door_{extra_index + 1}")
            base_name = name
            suffix = 2
            while name.lower() in used_door_names:
                name = f"{base_name}_{suffix}"
                suffix += 1
            used_door_names.add(name.lower())
            door_plans.append(
                NewDoorPlan(
                    base=base,
                    name=name,
                    part_rows=part_rows,
                    start_extra=extra_index,
                )
            )
            extra_index += consumed
            continue
        if choice.get("kind") == "new_animation":
            target_kind = choice.get("tileset", "secondary")
            target_tileset = (
                pair.secondary if target_kind == "secondary" else pair.primary
            )
            frame_count = max(1, int(choice.get("frames", len(cells)) or len(cells)))
            by_column = dict(cells)
            last_cell = cells[0][1]
            frame_cells: list[bytes] = []
            for frame in range(frame_count):
                cell = by_column.get(frame)
                if cell is None:
                    cell = last_cell
                frame_cells.append(cell)
                last_cell = cell
            frame_patterns: list[list[bytes]] = []
            first_palettes: list[int] = []
            for frame, cell in enumerate(frame_cells):
                patterns: list[bytes] = []
                for subtile in range(4):
                    sx = (subtile % 2) * TILE_SIZE
                    sy = (subtile // 2) * TILE_SIZE
                    patch = patch_from_rgba(cell, CELL, sx, sy, TILE_SIZE)
                    pattern, bank = _quantize_any_pattern(
                        pair,
                        patch,
                        1,
                        f"new animation {choice.get('name')} frame {frame} "
                        f"subtile {subtile}",
                    )
                    patterns.append(pattern)
                    if frame == 0:
                        first_palettes.append(bank)
                frame_patterns.append(patterns)
            global_start = target_tileset.append_tile_range(frame_patterns[0])
            snake = re.sub(
                r"[^a-z0-9_]+", "_", str(choice.get("name", "")).lower()
            ).strip("_")
            snake = snake or "custom_anim"
            folder = target_tileset.directory / "anim" / snake
            suffix = 2
            while folder.exists():
                folder = target_tileset.directory / "anim" / f"{snake}_{suffix}"
                suffix += 1
            frame_paths: list[str] = []
            for frame, patterns in enumerate(frame_patterns):
                path = folder / f"{frame}.png"
                frame_paths.append(path.relative_to(project.root).as_posix())
                if not dry_run:
                    _write_new_pattern_image(
                        path, patterns, target_tileset.directory / "tiles.png"
                    )
            definition = _add_new_animation_to_source(
                project,
                target_tileset,
                str(choice.get("name", "custom_anim")),
                global_start,
                frame_paths,
                int(choice.get("divisor", 16) or 16),
                dry_run=dry_run,
            )
            new_animation_paths.extend(
                [
                    project.root / "src" / "tileset_anims.c",
                    project.root / "include" / "tileset_anims.h",
                    project.root / "src" / "data" / "tilesets" / "headers.h",
                ]
            )
            transparent = TileRef(0, 0, False, False)
            animated_entries = [
                TileRef(global_start + index, first_palettes[index])
                for index in range(4)
            ]
            if target_tileset.entries_per_metatile == 12:
                entries = animated_entries + [transparent] * 8
            else:
                entries = animated_entries + [transparent] * 4
            attribute = bytes(target_tileset.attribute_size)
            attribute, behavior_warning = _attribute_with_behavior(
                project, attribute, choice.get("behavior")
            )
            if behavior_warning:
                result.warnings.append(behavior_warning)
            new_id = target_tileset.allocate_metatile(entries, attribute)
            metatile = target_tileset.metatile_at(new_id)
            if not dry_run:
                for column, _cell in cells:
                    if column >= len(definition.frame_paths):
                        continue
                    signature = _metatile_state_signature(
                        project, pair, metatile, [definition], column
                    )
                    result.extra_metatile_states.setdefault(signature, []).append(
                        new_id
                    )
            result.changed = True
            result.touched_tilesets.add((target_tileset.name, target_tileset.kind))
            verb = "Would create" if dry_run else "Created"
            suffix_text = (
                "Would add the frame PNGs and patch src/tileset_anims.c."
                if dry_run
                else "Added the frame PNGs and patched src/tileset_anims.c."
            )
            result.warnings.append(
                f"{verb} new tileset animation {definition.key} with "
                f"{len(frame_paths)} frame(s) and animated metatile "
                f"0x{new_id:03X} in {target_tileset.name} from extra sheet row "
                f"{row_y}. {suffix_text}"
            )
            new_metatile_rows.append(
                (
                    row_y,
                    new_id,
                    target_tileset,
                    len(cells),
                    definition,
                    list(definition.frame_paths),
                )
            )
            extra_index += 1
            continue
        definition = catalog.by_key(choice.get("animation", ""))
        if definition is None:
            result.warnings.append(
                f"Unknown animation '{choice.get('animation')}' for extra sheet row "
                f"{row_y}; ignored."
            )
            extra_index += 1
            continue
        target_kind = choice.get("tileset", "secondary")
        target_tileset = pair.secondary if target_kind == "secondary" else pair.primary
        found = _metatile_for_animation(pair, definition)
        if found is None:
            result.warnings.append(
                f"Could not find an existing metatile using {definition.key}; "
                f"extra sheet row {row_y} was ignored."
            )
            extra_index += 1
            continue
        owner, local, source_entries = found
        if target_tileset.kind == "primary" and any(
            entry.tile_index >= project.settings.num_tiles_in_primary
            for entry in source_entries
        ):
            result.warnings.append(
                f"{definition.key} is used by a secondary metatile with secondary "
                "tiles; choose the secondary tileset for this new metatile."
            )
            extra_index += 1
            continue
        entries = list(source_entries)
        attribute = owner.attributes[local]
        attribute, behavior_warning = _attribute_with_behavior(
            project, attribute, choice.get("behavior")
        )
        if behavior_warning:
            result.warnings.append(behavior_warning)
        new_id = target_tileset.allocate_metatile(entries, attribute)
        metatile = target_tileset.metatile_at(new_id)
        for state in range(len(definition.frame_paths)):
            signature = _metatile_state_signature(
                project, pair, metatile, [definition], state
            )
            result.extra_metatile_states.setdefault(signature, []).append(new_id)
        result.changed = True
        result.touched_tilesets.add((target_tileset.name, target_tileset.kind))
        result.warnings.append(
            f"Created new animated metatile 0x{new_id:03X} in {target_tileset.name} "
            f"from extra sheet row {row_y}, reusing {definition.key}'s existing art "
            "and all of its states. Choose 'new tileset animation' instead if you "
            "want the painted row to define new frames."
        )
        new_metatile_rows.append(
            (
                row_y,
                new_id,
                target_tileset,
                len(cells),
                definition,
                list(definition.frame_paths),
            )
        )
        extra_index += 1

    if (
        not frame_updates
        and not static_updates
        and not door_updates
        and not door_new_patterns
        and not result.extra_metatile_states
        and not door_plans
    ):
        return result

    new_frame_plan: dict[str, list[tuple[int, Path]]] = {}
    for definition_key, frames in frame_updates.items():
        definition = catalog.by_key(definition_key)
        if definition is None:
            continue
        new_indices = sorted(
            index for index in frames if index >= len(definition.frame_paths)
        )
        if not new_indices:
            continue
        folder = (project.root / definition.frame_paths[0]).parent
        numbers = [
            int(path.stem) for path in folder.glob("*.png") if path.stem.isdigit()
        ]
        next_number = max(numbers, default=-1) + 1
        plan: list[tuple[int, Path]] = []
        for index in new_indices:
            plan.append((index, folder / f"{next_number}.png"))
            next_number += 1
        new_frame_plan[definition_key] = plan

    affected: list[Path] = []
    for definition_key, frames in frame_updates.items():
        definition = catalog.by_key(definition_key)
        if definition is None:
            continue
        for frame_index in frames:
            if frame_index < len(definition.frame_paths):
                affected.append(project.root / definition.frame_paths[frame_index])
    for path in door_updates:
        affected.append(project.root / path)
    for door in door_by_path.values():
        if door.key in door_new_state_count:
            affected.append(project.root / door.frame_path)
    if new_frame_plan:
        affected.append(project.root / "src" / "tileset_anims.c")
    if door_new_state_count:
        affected.append(project.root / "src" / "field_door.c")
    for path in new_animation_paths:
        if path.is_file():
            affected.append(path)
    if door_palette_updates:
        affected.append(project.root / "src" / "field_door.c")
    if door_plans:
        affected.append(project.root / "src" / "field_door.c")
        affected.append(project.paths["metatile_labels"])
    if backup_func is not None and affected and not dry_run:
        backup_func(list(dict.fromkeys(affected)))

    for definition_key, frames in frame_updates.items():
        definition = catalog.by_key(definition_key)
        if definition is None:
            continue
        for frame_index, updates in frames.items():
            if frame_index >= len(definition.frame_paths):
                continue
            path = project.root / definition.frame_paths[frame_index]
            patterns = list(load_indexed_tile_patterns(str(path)))
            changed = False
            for local, pattern in updates.items():
                if local < len(patterns) and patterns[local] != pattern:
                    patterns[local] = pattern
                    changed = True
            if changed:
                result.changed = True
                result.written.append(path)
                if not dry_run:
                    _save_pattern_image(path, patterns)
        clear_tile_pattern_cache()

    for definition_key, plan in new_frame_plan.items():
        definition = catalog.by_key(definition_key)
        if definition is None:
            continue
        template = project.root / definition.frame_paths[0]
        base_patterns = list(load_indexed_tile_patterns(str(template)))
        new_relative_paths: list[str] = []
        for frame_index, destination in plan:
            patterns = list(base_patterns)
            updates = frame_updates[definition_key].get(frame_index, {})
            for local, pattern in updates.items():
                if local < len(patterns):
                    patterns[local] = pattern
            result.changed = True
            result.written.append(destination)
            new_relative_paths.append(destination.relative_to(project.root).as_posix())
            if not dry_run:
                _write_pattern_image_from_template(template, destination, patterns)
        if not dry_run:
            names = _append_animation_frames_to_source(
                project, definition, new_relative_paths
            )
            definition.frame_paths.extend(new_relative_paths)
            definition.frame_names.extend(names)
            result.written.append(project.root / "src" / "tileset_anims.c")
            result.warnings.append(
                f"{definition.key}: added {len(new_relative_paths)} new animation "
                "frame(s) and updated src/tileset_anims.c. Re-export the sheet to "
                "refresh its manifest."
            )
        clear_tile_pattern_cache()

    for global_tile, pattern in static_updates.items():
        tileset = pair.tileset_for_tile(global_tile)
        local = global_tile - tileset.global_tile_base
        if 0 <= local < len(tileset.patterns) and tileset.patterns[local] != pattern:
            tileset.patterns[local] = pattern
            tileset.dirty_tiles = True
            result.changed = True
            result.touched_tilesets.add((tileset.name, tileset.kind))

    for path_str, updates in door_updates.items():
        path = project.root / path_str
        door = door_by_path.get(path_str)
        if door is None:
            continue
        patterns = _door_dest_patterns(project, door)
        changed = False
        for local, pattern in updates.items():
            if local < len(patterns) and patterns[local] != pattern:
                patterns[local] = pattern
                changed = True
        if changed:
            result.changed = True
            result.written.append(path)
            if not dry_run:
                source_patterns = _door_source_patterns_from_dest(
                    project, door, patterns
                )
                _save_pattern_image(path, source_patterns)
    clear_tile_pattern_cache()
    if (
        door_palette_updates
        and not dry_run
        and _patch_door_palette_arrays(project, door_palette_updates)
    ):
        result.changed = True
        result.written.append(project.root / "src" / "field_door.c")
        result.warnings.append(
            "Updated door palette bank references in src/field_door.c to fit "
            "the edited door colours."
        )
    if not dry_run and doors_to_refresh:
        _refresh_door_sheet_rows(
            project,
            pair,
            result,
            sheet,
            manifest,
            rows,
            doors_to_refresh,
            sheet_path,
            manifest_path,
            dry_run,
        )
    if door_new_state_count and not dry_run:
        doors = {door.key: door for door in scan_animation_catalog(project).door_defs}
        for key, state_patterns in door_new_patterns.items():
            door = doors.get(key)
            if door is None:
                continue
            old_states = len(door.frame_offsets)
            new_states = door_new_state_count[key]
            parts = door.parts[0] * door.parts[1]
            patterns = _door_dest_patterns(project, door)
            new_length = new_states * door.frame_tile_count
            if len(patterns) < new_length:
                patterns.extend([bytes(64)] * (new_length - len(patterns)))
            for state in range(old_states, new_states):
                updates = state_patterns.get(state, {})
                offset = (
                    door.frame_offsets[-1]
                    + (state - old_states + 1) * door.frame_tile_count
                )
                previous = door.frame_offsets[state - 1]
                for part in range(parts):
                    for subtile in range(4):
                        destination = offset + part * 4 + subtile
                        if destination in updates:
                            patterns[destination] = updates[destination]
                        elif previous + part * 4 + subtile < len(
                            patterns
                        ) and destination < len(patterns):
                            patterns[destination] = patterns[
                                previous + part * 4 + subtile
                            ]
            source_patterns = _door_source_patterns_from_dest(project, door, patterns)
            door_png = project.root / door.frame_path
            _write_pattern_image_from_template(door_png, door_png, source_patterns)
            new_offsets = list(door.frame_offsets) + [
                door.frame_offsets[-1] + (index + 1) * door.frame_tile_count
                for index in range(new_states - old_states)
            ]
            new_size = _add_door_size_to_source(project, door, new_offsets)
            door.frame_offsets = new_offsets
            door.size_name = new_size
            for row in rows:
                if row.get("kind") == "door" and row.get("key") == key:
                    row["states"] = new_states
                    row["frame_offsets"] = list(new_offsets)
                    row["palette_banks"] = list(door.palette_banks)
            doors_to_refresh.add(key)
            result.changed = True
            result.written.extend([door_png, project.root / "src" / "field_door.c"])
            result.warnings.append(
                f"{door.key}: added {new_states - old_states} door state(s), "
                f"extended {door.frame_path}, and created dedicated size {new_size} "
                "so other doors of the same size keep their timing."
            )
        required_columns = max(
            [sheet.width // CELL] + [int(row.get("states", 0)) for row in rows]
        )
        if required_columns > sheet.width // CELL:
            expanded = Image.new(
                "RGBA", (required_columns * CELL, sheet.height), (0, 0, 0, 0)
            )
            expanded.paste(sheet, (0, 0))
            sheet = expanded
        manifest["columns"] = sheet.width // CELL
        _refresh_door_sheet_rows(
            project,
            pair,
            result,
            sheet,
            manifest,
            rows,
            doors_to_refresh,
            sheet_path,
            manifest_path,
            dry_run,
        )
    for plan in door_plans:
        new_door = _apply_new_door_plan(project, pair, plan, result, dry_run)
        new_door_rows.append((plan.start_extra, new_door))
        clear_tile_pattern_cache()
    if not dry_run:
        _refresh_animation_sheet_rows(
            project,
            pair,
            result,
            sheet,
            manifest,
            sheet_path,
            manifest_path,
        )
    if not dry_run and (new_metatile_rows or new_door_rows):
        _append_new_animation_rows(
            project,
            pair,
            result,
            sheet,
            manifest,
            sheet_path,
            manifest_path,
            len(rows),
            new_metatile_rows,
            new_door_rows,
        )
    if result.changed:
        pair.invalidate_signature_index()
    if pair.palette_rewrites:
        result.palette_changed = True
        seen_plans = set()
        for plan in pair.palette_rewrites:
            key = (plan.bank, tuple(plan.replaced_slots), tuple(plan.colors))
            if key in seen_plans:
                continue
            seen_plans.add(key)
            result.warnings.append(
                f"Animated art rewrote palette bank {plan.bank} slot(s) "
                f"{', '.join(str(slot) for slot in plan.replaced_slots)} to fit "
                "outside colours."
            )
        pair.palette_rewrites.clear()
    return result
