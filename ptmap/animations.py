"""Static analysis of pokeemerald tileset animation destination ranges."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image

from .errors import MapEditorError
from .project import Project

TILE_SIZE = 8


@dataclass
class TilesetAnimationDef:
    key: str
    tileset: str
    kind: str
    name: str
    ranges: list[tuple[int, int]]
    frame_paths: list[str]
    frame_sequence: list[int]
    frame_names: list[str]
    frame_array_name: str
    frame_declarations: dict[str, str]


@dataclass
class DoorAnimationDef:
    key: str
    tileset: str
    kind: str
    metatile_id: int
    metatile_label: str
    parts: tuple[int, int]
    frame_path: str
    frame_offsets: list[int]
    frame_tile_count: int
    palette_banks: list[int]
    metatile_width: int = 1
    metatile_height: int = 1
    part_positions: list[tuple[int, int]] = field(default_factory=list)
    sound: str = "DOOR_SOUND_NORMAL"
    size_name: str = "DOOR_SIZE_1x1"
    tiles_var: str = ""
    palettes_var: str = ""


@dataclass
class AnimationCatalog:
    tileset_defs: list[TilesetAnimationDef] = field(default_factory=list)
    door_defs: list[DoorAnimationDef] = field(default_factory=list)
    palette_only_tilesets: set[tuple[str, str]] = field(default_factory=set)

    def defs_for(self, kind: str, tileset: str) -> list[TilesetAnimationDef]:
        return [
            definition
            for definition in self.tileset_defs
            if definition.kind == kind and definition.tileset == tileset
        ]

    def by_key(self, key: str) -> TilesetAnimationDef | None:
        return next(
            (definition for definition in self.tileset_defs if definition.key == key),
            None,
        )


@lru_cache(maxsize=512)
def load_indexed_tile_patterns(path_str: str) -> tuple[bytes, ...]:
    """Loads a Porymap-style indexed PNG as row-major 8x8 tile patterns."""
    path = Path(path_str)
    with Image.open(path) as opened:
        opened.load()
        if opened.mode != "P":
            raise ValueError(f"{path} is not an indexed PNG (mode {opened.mode}).")
        width, height = opened.size
        if width % TILE_SIZE or height % TILE_SIZE:
            raise ValueError(f"{path} dimensions are not multiples of 8.")
        data = opened.tobytes()
    tiles: list[bytes] = []
    for ty in range(height // TILE_SIZE):
        for tx in range(width // TILE_SIZE):
            pattern = bytearray(64)
            for row in range(TILE_SIZE):
                src = (ty * TILE_SIZE + row) * width + tx * TILE_SIZE
                pattern[row * TILE_SIZE : (row + 1) * TILE_SIZE] = data[
                    src : src + TILE_SIZE
                ]
            tiles.append(bytes(pattern))
    return tuple(tiles)


def clear_tile_pattern_cache() -> None:
    load_indexed_tile_patterns.cache_clear()


@dataclass
class AnimationInfo:
    # (kind, canonical tileset name) -> global tile indices overwritten at runtime
    tile_indices: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    # Tilesets that run palette-only animations; tile graphics are not overwritten.
    palette_only_tilesets: set[tuple[str, str]] = field(default_factory=set)

    def tiles(self, kind: str, name: str) -> set[int]:
        return self.tile_indices.get((kind, name), set())

    def palette_only(self, kind: str, name: str) -> bool:
        return (kind, name) in self.palette_only_tilesets


def _function_bodies(text: str):
    pattern = re.compile(
        r"(?:static\s+)?(?:void|u16|bool)\s+"
        r"(QueueAnimTiles_\w+|BlendAnimPalette_\w+|TilesetAnim_\w+)\s*\([^)]*\)\s*\{"
    )
    for match in pattern.finditer(text):
        depth = 1
        i = match.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        yield match.group(1), text[match.end() : i - 1]


def _eval_expr(expr: str, num_tiles_primary: int) -> int | None:
    expr = expr.strip()
    if not re.fullmatch(r"[0-9a-fA-FxX+\-*/() \t_]+|NUM_TILES_IN_PRIMARY", expr):
        # Also allow the constant inside arithmetic.
        expr = expr.replace("NUM_TILES_IN_PRIMARY", str(num_tiles_primary))
        if not re.fullmatch(r"[0-9a-fA-FxX+\-*/() \t]+", expr):
            return None
    else:
        expr = expr.replace("NUM_TILES_IN_PRIMARY", str(num_tiles_primary))
    try:
        value = eval(expr, {"__builtins__": {}}, {})
        return int(value)
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError):
        return None


def _extract_tile_offset(expr: str, num_tiles_primary: int) -> int | None:
    marker = "TILE_OFFSET_4BPP"
    index = expr.find(marker)
    if index >= 0:
        open_paren = expr.find("(", index + len(marker))
        if open_paren >= 0:
            depth = 1
            i = open_paren + 1
            while i < len(expr) and depth:
                if expr[i] == "(":
                    depth += 1
                elif expr[i] == ")":
                    depth -= 1
                i += 1
            if depth == 0:
                value = _eval_expr(expr[open_paren + 1 : i - 1], num_tiles_primary)
                if value is not None:
                    return value
    return _eval_expr(expr, num_tiles_primary)


def _parse_array_values(
    text: str, array_name: str, num_tiles_primary: int
) -> list[int]:
    pattern = re.compile(
        re.escape(array_name) + r"\s*\[\s*\]\s*=\s*\{(.*?)\}", re.DOTALL
    )
    match = pattern.search(text)
    if not match:
        return []
    values: list[int] = []
    for raw in match.group(1).split(","):
        raw = re.sub(r"//.*", "", raw).strip()
        if not raw:
            continue
        value = _eval_expr(raw, num_tiles_primary)
        if value is not None:
            values.append(value)
    return values


def _source_asset_to_png(path: str) -> str:
    if path.endswith(".png"):
        return path
    if ".4bpp" in path:
        return path.split(".4bpp", 1)[0] + ".png"
    if ".8bpp" in path:
        return path.split(".8bpp", 1)[0] + ".png"
    return path


def _tileset_from_asset_path(
    project: Project, path: str, candidates: list[tuple[str, str]]
) -> tuple[str, str] | None:
    match = re.match(r"data/tilesets/(primary|secondary)/([^/]+)/", path)
    if match is None:
        return None
    kind, folder = match.group(1), match.group(2)
    for candidate_kind, name in candidates:
        if candidate_kind != kind:
            continue
        try:
            directory = project.tileset_dir(name, kind)
        except MapEditorError:
            continue
        if directory.name == folder:
            return kind, name
    return None


def _declaration_paths_for_body(body: str, declarations: dict[str, str]) -> list[str]:
    paths: list[str] = []
    for array_name in dict.fromkeys(re.findall(r"(gTilesetAnims_\w+)", body)):
        for name, path in declarations.items():
            if name == array_name or name.startswith(array_name + "_"):
                paths.append(path)
    return list(dict.fromkeys(paths))


def _match_tileset(
    function_name: str, candidates: list[tuple[str, str]]
) -> tuple[str, str] | None:
    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower())

    remainder = function_name
    for prefix in ("QueueAnimTiles_", "BlendAnimPalette_", "TilesetAnim_"):
        if remainder.startswith(prefix):
            remainder = remainder[len(prefix) :]
            break
    normalized = norm(remainder)
    best: tuple[int, str, str] | None = None
    for kind, name in candidates:
        suffix = name.removeprefix("gTileset_")
        suffix_norm = norm(suffix)
        if not suffix_norm:
            continue
        if normalized.startswith(suffix_norm):
            score = len(suffix_norm)
            if best is None or score > best[0]:
                best = (score, kind, name)
    if best is None:
        return None
    return best[1], best[2]


def scan_project_animations(project: Project) -> AnimationInfo:
    """Parses src/tileset_anims.c for runtime tile destinations.

    The parser intentionally ignores anything it cannot understand and relies on
    the conservative anim/*.png pattern fallback in Tileset for those cases.
    """
    source = project.root / "src" / "tileset_anims.c"
    info = AnimationInfo()
    if not source.is_file():
        return info
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info

    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for layout in project.layouts.values():
        for kind, name in (
            ("primary", layout.primary_tileset),
            ("secondary", layout.secondary_tileset),
        ):
            if name and (kind, name) not in seen:
                seen.add((kind, name))
                candidates.append((kind, name))

    num_primary = project.settings.num_tiles_in_primary
    declarations = {
        name: _source_asset_to_png(path)
        for name, path in re.findall(
            r"(?:const\s+)?u(?:16|32)\s+(\w+)\[\]\s*=\s*"
            r'(?:INCGFX_U(?:16|32)|INCBIN_U(?:16|32))\(\s*"([^"]+)"',
            text,
        )
    }
    for function_name, body in _function_bodies(text):
        target = None
        for path in _declaration_paths_for_body(body, declarations):
            target = _tileset_from_asset_path(project, path, candidates)
            if target is not None:
                break
        if target is None:
            target = _match_tileset(function_name, candidates)
        if target is None:
            continue
        if function_name.startswith("BlendAnimPalette_"):
            info.palette_only_tilesets.add(target)
            continue
        if not function_name.startswith("QueueAnimTiles_"):
            continue
        for call in re.finditer(
            r"AppendTilesetAnimToBuffer\s*\(([^,]+),\s*(.+?),\s*([^;]+?)\)\s*;",
            body,
            re.DOTALL,
        ):
            dest_expr = call.group(2).strip()
            count_expr = call.group(3).strip()
            count_match = re.search(r"(\d+)\s*\*\s*TILE_SIZE_4BPP", count_expr)
            if not count_match:
                continue
            count = int(count_match.group(1))
            if "VDests" in dest_expr:
                array_match = re.search(r"(gTilesetAnims_\w+_VDests)", dest_expr)
                if not array_match:
                    continue
                for value in _parse_array_values(
                    text, array_match.group(1), num_primary
                ):
                    info.tile_indices.setdefault(target, set()).update(
                        range(value, value + count)
                    )
            else:
                value = _extract_tile_offset(dest_expr, num_primary)
                if value is None:
                    continue
                info.tile_indices.setdefault(target, set()).update(
                    range(value, value + count)
                )
    return info


def project_animation_tiles(project: Project, kind: str, name: str) -> set[int]:
    if project._animation_info_cache is None:
        project._animation_info_cache = scan_project_animations(project)
    return project._animation_info_cache.tiles(kind, name)


def project_palette_only_animation(project: Project, kind: str, name: str) -> bool:
    if project._animation_info_cache is None:
        project._animation_info_cache = scan_project_animations(project)
    return project._animation_info_cache.palette_only(kind, name)


def _parse_tileset_animation_defs(
    project: Project, text: str, candidates: list[tuple[str, str]]
) -> list[TilesetAnimationDef]:
    declarations = {
        name: _source_asset_to_png(path)
        for name, path in re.findall(
            r"(?:const\s+)?u(?:16|32)\s+(\w+)\[\]\s*=\s*"
            r'(?:INCGFX_U(?:16|32)|INCBIN_U(?:16|32))\(\s*"([^"]+)"',
            text,
        )
    }
    num_primary = project.settings.num_tiles_in_primary
    definitions: dict[str, TilesetAnimationDef] = {}

    for function_name, body in _function_bodies(text):
        if not function_name.startswith("QueueAnimTiles_"):
            continue
        target = _match_tileset(function_name, candidates)
        calls = list(
            re.finditer(
                r"AppendTilesetAnimToBuffer\s*\(([^,]+),\s*(.+?),\s*([^;]+?)\)\s*;",
                body,
                re.DOTALL,
            )
        )
        if not calls:
            continue
        array_match = re.search(r"(gTilesetAnims_\w+?)(?:\[|,)", calls[0].group(1))
        array_name = array_match.group(1) if array_match else ""

        frame_names: list[str] = []
        if array_name:
            array_pattern = re.compile(
                re.escape(array_name) + r"\s*\[\s*\]\s*=\s*\{(.*?)\};",
                re.DOTALL,
            )
            match = array_pattern.search(text)
            if match:
                frame_names = [
                    name
                    for name in re.findall(r"\b[A-Za-z_]\w*\b", match.group(1))
                    if name in declarations
                ]

        sequence_paths = [declarations[name] for name in frame_names]
        for path in _declaration_paths_for_body(body, declarations) + sequence_paths:
            path_target = _tileset_from_asset_path(project, path, candidates)
            if path_target is not None:
                target = path_target
                break
        if target is None:
            continue
        kind, tileset = target
        remainder = function_name.removeprefix("QueueAnimTiles_")
        suffix = tileset.removeprefix("gTileset_")
        if remainder.lower().startswith(suffix.lower()):
            animation_name = remainder[len(suffix) :].lstrip("_")
        else:
            animation_name = remainder
        if not animation_name:
            animation_name = "animation"

        if not sequence_paths:
            try:
                tileset_dir = project.tileset_dir(tileset, kind)
            except MapEditorError:
                continue
            folder = project.tileset_folder_name(animation_name)
            anim_dir = tileset_dir / "anim" / folder
            if anim_dir.is_dir():
                sequence_paths = [
                    path.relative_to(project.root).as_posix()
                    for path in sorted(
                        anim_dir.glob("*.png"),
                        key=lambda path: (
                            int(path.stem) if path.stem.isdigit() else path.stem
                        ),
                    )
                ]
        unique_paths: list[str] = []
        frame_sequence: list[int] = []
        for path in sequence_paths:
            if path not in unique_paths:
                unique_paths.append(path)
            frame_sequence.append(unique_paths.index(path))

        ranges: list[tuple[int, int]] = []
        for call in calls:
            dest_expr = call.group(2).strip()
            count_match = re.search(r"(\d+)\s*\*\s*TILE_SIZE_4BPP", call.group(3))
            if not count_match:
                continue
            count = int(count_match.group(1))
            if "VDests" in dest_expr:
                vdest_match = re.search(r"(gTilesetAnims_\w+_VDests)", dest_expr)
                if not vdest_match:
                    continue
                for value in _parse_array_values(
                    text, vdest_match.group(1), num_primary
                ):
                    ranges.append((value, count))
            else:
                value = _extract_tile_offset(dest_expr, num_primary)
                if value is not None:
                    ranges.append((value, count))
        if not unique_paths or not ranges:
            continue

        key = f"{tileset}/{animation_name.lower()}"
        existing = definitions.get(key)
        if existing is None:
            definitions[key] = TilesetAnimationDef(
                key=key,
                tileset=tileset,
                kind=kind,
                name=animation_name,
                ranges=ranges,
                frame_paths=unique_paths,
                frame_sequence=frame_sequence,
                frame_names=[
                    name for name, path in declarations.items() if path in unique_paths
                ],
                frame_array_name=array_name,
                frame_declarations={
                    name: path
                    for name, path in declarations.items()
                    if path in unique_paths
                },
            )
        else:
            for item in ranges:
                if item not in existing.ranges:
                    existing.ranges.append(item)
    return list(definitions.values())


def _parse_door_animation_defs(project: Project, text: str) -> list[DoorAnimationDef]:
    labels_path = project.paths["metatile_labels"]
    labels: dict[str, int] = {}
    if labels_path.is_file():
        labels_text = labels_path.read_text(encoding="utf-8", errors="replace")
        for name, value in re.findall(
            r"#define\s+(METATILE_\w+)\s+(0[xX][0-9a-fA-F]+|\d+)", labels_text
        ):
            labels[name] = int(value, 0)

    tile_declarations = {
        name: (_source_asset_to_png(path), options)
        for name, path, options in re.findall(
            r"static\s+const\s+u8\s+(\w+)\[\]\s*=\s*"
            r'(?:INCGFX_U8|INCBIN_U8)\(\s*"([^"]+)"([^)]*)\)',
            text,
        )
    }
    palettes = {
        name: [int(value.strip(), 0) for value in values.split(",") if value.strip()]
        for name, values in re.findall(
            r"static\s+const\s+u8\s+(sDoorAnimPalettes_\w+)\[\]\s*=\s*\{([^}]*)\}",
            text,
        )
    }
    frame_offsets: dict[str, list[int]] = {}
    for name, values in re.findall(
        r"static\s+const\s+struct\s+DoorAnimFrame\s+"
        r"(sDoorAnimFrames_Open\w+|sDoorOpenAnimFrames|sBigDoorOpenAnimFrames)"
        r"\[\]\s*=\s*\{(.*?)\};",
        text,
        re.DOTALL,
    ):
        offsets: list[int] = []
        for time, expr in re.findall(r"\{\s*(\d+)\s*,\s*([^}]+)\}", values):
            expr = expr.strip()
            if int(time) == 0 or expr == "-1":
                continue
            value = _eval_expr(
                expr.replace("TILE_SIZE_4BPP", "32"),
                project.settings.num_tiles_in_primary,
            )
            if value is not None and value >= 0:
                offsets.append(value // 32)
        frame_offsets[name] = offsets

    sizes: dict[str, tuple[int, int, str, int]] = {}
    for enum_name, open_name, _close_name in re.findall(
        r"\[(DOOR_SIZE_\w+)\]\s*=\s*\{\s*\.openAnimFrames\s*=\s*"
        r"(sDoorAnimFrames_Open\w+)\s*,\s*\.closeAnimFrames\s*=\s*"
        r"(sDoorAnimFrames_Close\w+)\s*,?\s*\}",
        text,
        re.DOTALL,
    ):
        dimension = re.match(r"DOOR_SIZE_(\d+)x(\d+)", enum_name)
        if dimension is None:
            continue
        width = int(dimension.group(1))
        height = int(dimension.group(2))
        sizes[enum_name] = (width, height, open_name, width * height * 4)
    if not sizes:
        sizes = {
            "DOOR_SIZE_1x1": (1, 1, "sDoorAnimFrames_Open1x1", 4),
            "DOOR_SIZE_1x2": (1, 2, "sDoorAnimFrames_Open1x2", 8),
            "DOOR_SIZE_2x2_LEFT": (2, 2, "sDoorAnimFrames_Open2x2", 16),
            "DOOR_SIZE_2x2_RIGHT": (2, 2, "sDoorAnimFrames_Open2x2", 16),
        }
    sizes.setdefault("DOOR_SIZE_1x2_LEGACY", (1, 2, "sDoorOpenAnimFrames", 8))
    sizes.setdefault("DOOR_SIZE_2x2_LEGACY", (2, 2, "sBigDoorOpenAnimFrames", 16))

    def part_positions(width: int, height: int) -> list[tuple[int, int]]:
        if width == 2 and height == 2:
            return [(0, 0), (0, 1), (1, 0), (1, 1)]
        return [(x, y) for y in range(height) for x in range(width)]

    table_match = re.search(
        r"sDoorAnimGraphicsTable\[\]\s*=\s*\{(.*?)\n\};", text, re.DOTALL
    )
    if table_match is None:
        return []
    table = table_match.group(1)

    defs: list[DoorAnimationDef] = []
    for block in re.findall(r"\{([^{}]*)\}", table):
        fields = dict(re.findall(r"\.(\w+)\s*=\s*([^,\n]+)", block))
        positional: list[str] = []
        if not fields:
            positional = [
                part.strip()
                for part in re.sub(r"//[^\n]*", "", block).split(",")
                if part.strip()
            ]
        label = fields.get("metatileNum", "").strip()
        tileset = fields.get("tileset", "").strip().removeprefix("&")
        size_name = fields.get("size", "").strip()
        tiles_name = fields.get("tiles", "").strip()
        palettes_name = fields.get("palettes", "").strip()
        sound = fields.get("sound", "DOOR_SOUND_NORMAL").strip()
        if positional and len(positional) >= 5:
            label = positional[0]
            sound = positional[1]
            size_code = positional[2]
            tiles_name = positional[3]
            palettes_name = positional[4]
            size_name = (
                "DOOR_SIZE_2x2_LEGACY"
                if size_code in ("2", "DOOR_SIZE_2x2")
                else "DOOR_SIZE_1x2_LEGACY"
            )
            tileset = ""
        if (
            label not in labels
            or size_name not in sizes
            or tiles_name not in tile_declarations
        ):
            continue
        width, height, frames_name, frame_tile_count = sizes[size_name]
        path, options = tile_declarations[tiles_name]
        mwidth = int((re.search(r"-mwidth\s+(\d+)", options) or [None, "1"])[1])
        mheight = int((re.search(r"-mheight\s+(\d+)", options) or [None, "1"])[1])
        offsets = frame_offsets.get(frames_name, [])
        if not offsets:
            continue
        kind = "primary"
        if not tileset:
            kind = (
                "primary"
                if labels[label] < project.settings.num_metatiles_in_primary
                else "secondary"
            )
        else:
            try:
                project.tileset_dir(tileset, "primary")
            except MapEditorError:
                kind = "secondary"
        defs.append(
            DoorAnimationDef(
                key=f"{tileset or 'legacy'}/{label}",
                tileset=tileset,
                kind=kind,
                metatile_id=labels[label],
                metatile_label=label,
                parts=(width, height),
                frame_path=path,
                frame_offsets=offsets,
                frame_tile_count=frame_tile_count,
                palette_banks=palettes.get(palettes_name, [1] * (width * height * 4)),
                metatile_width=mwidth,
                metatile_height=mheight,
                part_positions=part_positions(width, height),
                sound=sound,
                size_name=size_name,
                tiles_var=tiles_name,
                palettes_var=palettes_name,
            )
        )
    return defs


def scan_animation_catalog(project: Project) -> AnimationCatalog:
    if project._animation_catalog_cache is not None:
        return project._animation_catalog_cache

    catalog = AnimationCatalog()
    source = project.root / "src" / "tileset_anims.c"
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for layout in project.layouts.values():
        for kind, name in (
            ("primary", layout.primary_tileset),
            ("secondary", layout.secondary_tileset),
        ):
            if name and (kind, name) not in seen:
                seen.add((kind, name))
                candidates.append((kind, name))
    if source.is_file():
        text = source.read_text(encoding="utf-8", errors="replace")
        catalog.tileset_defs = _parse_tileset_animation_defs(project, text, candidates)
        info = scan_project_animations(project)
        catalog.palette_only_tilesets = info.palette_only_tilesets
    door_source = project.root / "src" / "field_door.c"
    if door_source.is_file():
        catalog.door_defs = _parse_door_animation_defs(
            project, door_source.read_text(encoding="utf-8", errors="replace")
        )
    project._animation_catalog_cache = catalog
    return catalog
