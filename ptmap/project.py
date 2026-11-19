"""Project discovery and layout/map plumbing for Poryeasy.

Poryeasy deliberately works on Porymap's compiled artifacts
(``data/layouts``, ``data/tilesets``) rather than on Porytiles source assets.
That makes it usable on stock pokeemerald / pokeemerald-expansion projects
which were never imported by Porytiles.
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import MapEditorError

_HEADER_CANDIDATES = (
    "include/fieldmap.h",
    "include/global.fieldmap.h",
    "include/global.h",
    "include/constants/global.h",
)

_DEFAULT_SETTINGS = {
    "NUM_TILES_IN_PRIMARY": 512,
    "NUM_TILES_TOTAL": 1024,
    "NUM_METATILES_IN_PRIMARY": 512,
    "NUM_METATILES_TOTAL": 1024,
    "NUM_PALS_IN_PRIMARY": 6,
    "NUM_PALS_TOTAL": 13,
    "NUM_TILES_PER_METATILE": 8,
    "MAPGRID_METATILE_ID_MASK": 0x03FF,
    "MAPGRID_COLLISION_MASK": 0x0C00,
    "MAPGRID_ELEVATION_MASK": 0xF000,
}


@dataclass(frozen=True)
class ProjectSettings:
    num_tiles_in_primary: int = 512
    num_tiles_total: int = 1024
    num_metatiles_in_primary: int = 512
    num_metatiles_total: int = 1024
    num_pals_in_primary: int = 6
    num_pals_total: int = 13
    num_tiles_per_metatile: int = 8
    metatile_id_mask: int = 0x03FF
    collision_mask: int = 0x0C00
    elevation_mask: int = 0xF000

    @property
    def entries_per_metatile(self) -> int:
        return self.num_tiles_per_metatile

    @property
    def num_metatiles_secondary(self) -> int:
        return self.num_metatiles_total - self.num_metatiles_in_primary

    @property
    def num_tiles_secondary(self) -> int:
        return self.num_tiles_total - self.num_tiles_in_primary

    def with_layer_mode(self, layer_mode: str | None) -> ProjectSettings:
        if layer_mode is None:
            return self
        if layer_mode == "dual":
            return ProjectSettings(**{**self.__dict__, "num_tiles_per_metatile": 8})
        if layer_mode == "triple":
            return ProjectSettings(**{**self.__dict__, "num_tiles_per_metatile": 12})
        raise MapEditorError(
            f"Unknown layer mode '{layer_mode}' (expected dual or triple)."
        )


@dataclass(frozen=True)
class LayoutEntry:
    id: str
    name: str
    width: int
    height: int
    primary_tileset: str
    secondary_tileset: str
    border_filepath: str
    blockdata_filepath: str
    layer_version: str = "emerald"
    raw: dict = field(default_factory=dict)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


@dataclass(frozen=True)
class MapEntry:
    id: str
    name: str
    folder: Path
    layout_id: str
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Target:
    query: str
    map: MapEntry | None
    layout: LayoutEntry

    @property
    def display_name(self) -> str:
        if self.map is not None:
            return self.map.name
        return self.layout.name.removesuffix("_Layout")


class Project:
    """A Pokémon decomp project rooted at a directory containing ``data/``."""

    def __init__(self, root: Path, *, layer_mode: str | None = None) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise MapEditorError(f"Project root does not exist: {self.root}")

        self.cfg = self._load_project_cfg()
        self.paths = {
            "layouts_json": self._path_from_cfg(
                "json_layouts", "data/layouts/layouts.json"
            ),
            "maps_dir": self._path_from_cfg("data_map_folders", "data/maps"),
            "primary_dir": self._path_from_cfg(
                "data_primary_tilesets_folders", "data/tilesets/primary"
            ),
            "secondary_dir": self._path_from_cfg(
                "data_secondary_tilesets_folders", "data/tilesets/secondary"
            ),
            "graphics_h": self._path_from_cfg(
                "tilesets_graphics", "src/data/tilesets/graphics.h"
            ),
            "metatile_labels": self._path_from_cfg(
                "constants_metatile_labels", "include/constants/metatile_labels.h"
            ),
        }

        if not self.paths["layouts_json"].is_file():
            raise MapEditorError(
                "Could not find data/layouts/layouts.json. "
                f"Run from a decomp project root or pass a correct project root (got {self.root})."
            )

        self._defined = self._read_defines()
        self.settings = self._build_settings().with_layer_mode(layer_mode)
        self.layouts = self._load_layouts()
        self._maps: list[MapEntry] | None = None
        self._tileset_cache: dict[tuple[str, str], object] = {}
        self._tileset_asset_dir_cache: dict[
            tuple[str, str], tuple[Path, Path]
        ] = {}
        self._global_tile_usage_cache: dict[str, set[int]] = {}
        self._metatile_usage_cache: dict[tuple[str, str], set[int]] | None = None
        self._metatile_ref_counts_cache: dict[int, int] | None = None
        self._metatile_usage_incomplete: set[tuple[str, str]] = set()
        self._metatile_usage_incomplete_layouts: dict[tuple[str, str], set[str]] = {}
        self._animation_info_cache: Any = None
        self._animation_catalog_cache: Any = None
        self._protected_metatiles_by_tileset = self._load_protected_metatiles()
        self._protected_metatiles = (
            set().union(*self._protected_metatiles_by_tileset.values())
            if self._protected_metatiles_by_tileset
            else set()
        )
        self._graphics_h_tile_limit_cache: dict[str, int | None] = {}
        self._tile_reference_counts_cache: dict[
            tuple[str, str, str | None], dict[int, int]
        ] = {}

    # Project config -----------------------------------------------------

    def _path_from_cfg(self, key: str, fallback: str) -> Path:
        value = self.cfg.get(key, fallback).strip().strip('"')
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def _load_project_cfg(self) -> dict[str, str]:
        for name in ("porymap.project.cfg", ".porymap.project.cfg"):
            path = self.root / name
            if not path.is_file():
                continue
            cfg: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
            return cfg
        return {}

    def _read_defines(self) -> dict[str, int]:
        defines: dict[str, int] = {}
        seen: set[Path] = set()
        for rel in _HEADER_CANDIDATES:
            path = self.root / rel
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            text = path.read_text(encoding="utf-8", errors="replace")
            for name, value in re.findall(
                r"^\s*#define\s+([A-Za-z_]\w*)\s+([^/\r\n]+)", text, re.MULTILINE
            ):
                value = value.strip()
                if value not in _DEFAULT_SETTINGS and not re.fullmatch(
                    r"(?:0[xX][0-9a-fA-F]+|\d+)", value
                ):
                    continue
                try:
                    defines[name] = int(value, 0)
                except ValueError:
                    pass
        return defines

    def _build_settings(self) -> ProjectSettings:
        values = {**_DEFAULT_SETTINGS, **self._defined}
        return ProjectSettings(
            num_tiles_in_primary=values["NUM_TILES_IN_PRIMARY"],
            num_tiles_total=values["NUM_TILES_TOTAL"],
            num_metatiles_in_primary=values["NUM_METATILES_IN_PRIMARY"],
            num_metatiles_total=values["NUM_METATILES_TOTAL"],
            num_pals_in_primary=values["NUM_PALS_IN_PRIMARY"],
            num_pals_total=values["NUM_PALS_TOTAL"],
            num_tiles_per_metatile=values["NUM_TILES_PER_METATILE"],
            metatile_id_mask=values["MAPGRID_METATILE_ID_MASK"],
            collision_mask=values["MAPGRID_COLLISION_MASK"],
            elevation_mask=values["MAPGRID_ELEVATION_MASK"],
        )

    # Layouts and maps ---------------------------------------------------

    def _load_layouts(self) -> dict[str, LayoutEntry]:
        data = json.loads(self.paths["layouts_json"].read_text(encoding="utf-8"))
        entries = data.get("layouts", data) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise MapEditorError("layouts.json has an unexpected structure.")

        result: dict[str, LayoutEntry] = {}
        for raw in entries:
            try:
                entry = LayoutEntry(
                    id=str(raw["id"]),
                    name=str(raw["name"]),
                    width=int(raw["width"]),
                    height=int(raw["height"]),
                    primary_tileset=str(raw["primary_tileset"]),
                    secondary_tileset=str(raw["secondary_tileset"]),
                    border_filepath=str(raw.get("border_filepath", "")),
                    blockdata_filepath=str(raw["blockdata_filepath"]),
                    layer_version=str(raw.get("layout_version", "emerald")),
                    raw=raw,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MapEditorError(
                    f"Invalid layout entry in layouts.json: {raw!r}"
                ) from exc
            result[entry.id] = entry
        return result

    def _load_maps(self) -> list[MapEntry]:
        if self._maps is not None:
            return self._maps
        maps: list[MapEntry] = []
        maps_dir = self.paths["maps_dir"]
        if maps_dir.is_dir():
            for map_json in sorted(maps_dir.glob("*/map.json")):
                try:
                    raw = json.loads(map_json.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                layout_id = raw.get("layout")
                if not layout_id or layout_id not in self.layouts:
                    continue
                maps.append(
                    MapEntry(
                        id=str(raw.get("id", map_json.parent.name)),
                        name=str(raw.get("name", map_json.parent.name)),
                        folder=map_json.parent,
                        layout_id=str(layout_id),
                        raw=raw,
                    )
                )
        self._maps = maps
        return maps

    @staticmethod
    def _normalize_query(value: str) -> str:
        value = value.strip().lower().replace("-", "_").replace(" ", "_")
        return re.sub(r"_+", "_", value)

    def resolve(self, query: str) -> Target:
        """Resolves a map folder, map symbol, map name, layout ID, or layout name."""
        needle = self._normalize_query(query)
        if not needle:
            raise MapEditorError("Empty map/layout name.")

        maps = self._load_maps()
        compact = needle.replace("_", "")

        def matches(value: str) -> bool:
            normalized = self._normalize_query(value)
            return normalized == needle or normalized.replace("_", "") == compact

        # Exact map folder / symbol / name first.
        for entry in maps:
            if matches(entry.folder.name):
                return Target(query, entry, self.layouts[entry.layout_id])
        for entry in maps:
            if matches(entry.id) or matches(entry.name):
                return Target(query, entry, self.layouts[entry.layout_id])

        # Layout ID / layout name.
        layout = next(
            (
                entry
                for entry in self.layouts.values()
                if matches(entry.id) or matches(entry.name)
            ),
            None,
        )
        if layout is None:
            # Friendly fallback: substring match, but only when unambiguous.
            candidates = [
                entry
                for entry in maps
                if needle in self._normalize_query(entry.id)
                or compact in self._normalize_query(entry.id).replace("_", "")
                or needle in self._normalize_query(entry.name)
                or compact in self._normalize_query(entry.name).replace("_", "")
                or needle in self._normalize_query(entry.folder.name)
                or compact in self._normalize_query(entry.folder.name).replace("_", "")
            ]
            if len(candidates) == 1:
                entry = candidates[0]
                return Target(query, entry, self.layouts[entry.layout_id])
            raise MapEditorError(
                f"Could not find a map or layout matching '{query}'. "
                "Try a symbol such as MAP_LITTLEROOT_TOWN or a layout such as LAYOUT_LITTLEROOT_TOWN."
            )

        for entry in maps:
            if entry.layout_id == layout.id:
                return Target(query, entry, layout)
        return Target(query, None, layout)

    def maps_using_layout(self, layout_id: str) -> list[MapEntry]:
        return [entry for entry in self._load_maps() if entry.layout_id == layout_id]

    # Tilesets -----------------------------------------------------------

    def _tileset_root(self, kind: str) -> Path:
        if kind == "primary":
            return self.paths["primary_dir"]
        if kind == "secondary":
            return self.paths["secondary_dir"]
        raise MapEditorError(f"Invalid tileset kind '{kind}'.")

    @staticmethod
    def tileset_folder_name(tileset_name: str) -> str:
        base = tileset_name.removeprefix("gTileset_").removeprefix("Tileset_")
        # CamelCase -> snake_case, including acronym boundaries.
        base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", base)
        base = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", base)
        return base.lower()

    def tileset_dir(
        self, tileset_name: str, kind: str, version: str | None = None
    ) -> Path:
        root = self._tileset_root(kind)
        folder = self.tileset_folder_name(tileset_name)
        candidates = []
        if version == "frlg":
            candidates.append(folder + "_frlg")
        candidates.append(folder)
        candidates.append(folder + "_frlg")
        for name in dict.fromkeys(candidates):
            candidate = root / name
            if candidate.is_dir():
                return candidate

        def norm(value: str) -> str:
            return re.sub(r"[^a-z0-9]", "", value.lower())

        for name in dict.fromkeys(candidates):
            wanted = norm(name)
            matches = [
                path
                for path in root.iterdir()
                if path.is_dir() and norm(path.name) == wanted
            ]
            if len(matches) == 1:
                return matches[0]
        wanted = norm(tileset_name)
        matches = [
            path
            for path in root.iterdir()
            if path.is_dir() and norm(path.name) == wanted
        ]
        if len(matches) == 1:
            return matches[0]
        raise MapEditorError(
            f"Could not locate the {kind} tileset directory for '{tileset_name}' under {root}."
        )

    def _tileset_asset_dirs(
        self, tileset_name: str, directory: Path
    ) -> tuple[Path, Path]:
        """Resolves shared tiles.png/palette directories from headers.h."""
        cache_key = (tileset_name, str(directory))
        cached = self._tileset_asset_dir_cache.get(cache_key)
        if cached is not None:
            return cached
        tiles_dir = directory
        palettes_dir = directory / "palettes"
        headers_h = self.root / "src" / "data" / "tilesets" / "headers.h"
        graphics_h = self.paths["graphics_h"]
        if headers_h.is_file() and graphics_h.is_file():
            header_text = headers_h.read_text(encoding="utf-8", errors="replace")
            block_match = re.search(
                r"const\s+struct\s+Tileset\s+"
                + re.escape(tileset_name)
                + r"\s*=\s*\{(.*?)\};",
                header_text,
                re.DOTALL,
            )
            if block_match is not None:
                block = block_match.group(1)
                graphics_text = graphics_h.read_text(
                    encoding="utf-8", errors="replace"
                )
                graphics_c = self.root / "src" / "graphics.c"
                if graphics_c.is_file():
                    graphics_text += "\n" + graphics_c.read_text(
                        encoding="utf-8", errors="replace"
                    )

                def resolve(variable: str | None) -> Path | None:
                    if not variable:
                        return None
                    definition = re.search(
                        r"\b"
                        + re.escape(variable)
                        + r"\b[^;=]*?=\s*(.*?);",
                        graphics_text,
                        re.DOTALL,
                    )
                    if definition is None:
                        return None
                    for relative in re.findall(
                        r'"([^"]+)"', definition.group(1)
                    ):
                        path = self.root / relative
                        if path.is_file():
                            return path.parent
                    return None

                tiles_match = re.search(r"\.tiles\s*=\s*(\w+)", block)
                palettes_match = re.search(r"\.palettes\s*=\s*(\w+)", block)
                resolved_tiles = resolve(
                    tiles_match.group(1) if tiles_match else None
                )
                resolved_palettes = resolve(
                    palettes_match.group(1) if palettes_match else None
                )
                if resolved_tiles is not None:
                    tiles_dir = resolved_tiles
                if resolved_palettes is not None:
                    palettes_dir = resolved_palettes
        self._tileset_asset_dir_cache[cache_key] = (tiles_dir, palettes_dir)
        return tiles_dir, palettes_dir

    def load_tileset(
        self, tileset_name: str, kind: str, version: str | None = None
    ):
        from .tileset import Tileset

        directory = self.tileset_dir(tileset_name, kind, version)
        key = (kind, str(directory))
        cached = self._tileset_cache.get(key)
        if cached is not None:
            return cached
        tiles_dir, palettes_dir = self._tileset_asset_dirs(
            tileset_name, directory
        )
        tileset = Tileset.load(
            self,
            tileset_name,
            kind,
            directory,
            tiles_directory=tiles_dir,
            palettes_directory=palettes_dir,
        )
        self._tileset_cache[key] = tileset
        return tileset

    def load_tileset_pair(self, layout: LayoutEntry):
        from .tileset import TilesetPair

        if not layout.secondary_tileset:
            raise MapEditorError(
                f"Layout '{layout.id}' has no secondary tileset; this tool currently expects a paired secondary tileset."
            )
        primary = self.load_tileset(
            layout.primary_tileset, "primary", layout.layer_version
        )
        secondary = self.load_tileset(
            layout.secondary_tileset, "secondary", layout.layer_version
        )
        return TilesetPair(self, primary, secondary)

    # Map block data -----------------------------------------------------

    def layout_path(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else self.root / path

    def read_blocks(self, layout: LayoutEntry) -> list[int]:
        path = self.layout_path(layout.blockdata_filepath)
        if not path.is_file():
            raise MapEditorError(f"Missing block data file: {path}")
        data = path.read_bytes()
        expected = layout.width * layout.height * 2
        if len(data) != expected:
            raise MapEditorError(
                f"Block data {path} has {len(data)} bytes, expected {expected} "
                f"for a {layout.width}x{layout.height} layout."
            )
        return list(struct.unpack(f"<{layout.width * layout.height}H", data))

    def write_blocks(self, layout: LayoutEntry, blocks: Iterable[int]) -> None:
        path = self.layout_path(layout.blockdata_filepath)
        values = list(blocks)
        expected = layout.width * layout.height
        if len(values) != expected:
            raise MapEditorError(
                f"Refusing to write {len(values)} blocks to {layout.width}x{layout.height} layout."
            )
        write_bytes_atomic(path, struct.pack(f"<{expected}H", *values))

    def read_border(self, layout: LayoutEntry) -> tuple[int, ...]:
        if not layout.border_filepath:
            return ()
        path = self.layout_path(layout.border_filepath)
        if not path.is_file():
            return ()
        data = path.read_bytes()
        if len(data) % 2:
            raise MapEditorError(f"Border file {path} has an odd byte length.")
        return struct.unpack(f"<{len(data) // 2}H", data)

    # Usage indexes ------------------------------------------------------

    def _iter_tileset_dirs(self, kind: str) -> Iterator[Path]:
        root = self._tileset_root(kind)
        if not root.is_dir():
            return
        for path in sorted(root.iterdir()):
            if path.is_dir() and (path / "metatiles.bin").is_file():
                yield path

    def tile_usage_for(self, tileset) -> set[int]:
        """Returns global tile indices referenced by metatiles relevant to ``tileset``.

        Primary tile slots (0..511) are shared by every secondary tileset, so
        deciding whether a primary slot is free requires scanning every
        metatiles.bin in the project. A secondary tile slot only exists in its
        own secondary tileset, so only that tileset's metatiles matter.
        """
        from .tileset import Tileset

        if not isinstance(tileset, Tileset):
            raise MapEditorError("tile_usage_for expects a Tileset.")
        if tileset.kind == "primary":
            cache_key = "primary"
        else:
            cache_key = f"secondary:{tileset.directory}"
        cached = self._global_tile_usage_cache.get(cache_key)
        if cached is not None:
            return cached

        used: set[int] = set()
        if tileset.kind == "primary":
            directories = [
                ("primary", directory)
                for directory in self._iter_tileset_dirs("primary")
            ] + [
                ("secondary", directory)
                for directory in self._iter_tileset_dirs("secondary")
            ]
        else:
            directories = [("secondary", tileset.directory)]

        for kind, directory in directories:
            loaded = self._tileset_cache.get((kind, str(directory)))
            if loaded is not None and getattr(loaded, "metatiles", None):
                for metatile in loaded.metatiles:  # type: ignore[union-attr]
                    for entry in metatile:
                        index = entry.tile_index
                        if tileset.kind == "primary":
                            if index < self.settings.num_tiles_in_primary:
                                used.add(index)
                        elif index >= self.settings.num_tiles_in_primary:
                            used.add(index)
                continue
            path = directory / "metatiles.bin"
            data = path.read_bytes()
            if len(data) % 2:
                continue
            values = struct.unpack(f"<{len(data) // 2}H", data)
            for word in values:
                index = word & 0x03FF
                if tileset.kind == "primary":
                    if index < self.settings.num_tiles_in_primary:
                        used.add(index)
                elif index >= self.settings.num_tiles_in_primary:
                    used.add(index)
        self._global_tile_usage_cache[cache_key] = used
        return used

    def _scan_metatile_references(self) -> None:
        if self._metatile_usage_cache is not None:
            return
        usage: dict[tuple[str, str], set[int]] = {}
        counts: dict[int, int] = {}
        incomplete: set[tuple[str, str]] = set()
        incomplete_layouts: dict[tuple[str, str], set[str]] = {}

        def mark_incomplete(
            kind: str,
            name: str,
            directory: Path | None,
            layout_id: str,
        ) -> None:
            keys: list[tuple[str, str]] = []
            if name:
                keys.append((kind, "name:" + self._normalize_query(name)))
            if directory is not None:
                keys.append((kind, str(directory)))
            for key in keys:
                incomplete.add(key)
                incomplete_layouts.setdefault(key, set()).add(layout_id)

        for layout in self.layouts.values():
            try:
                primary_dir: Path | None = self.tileset_dir(
                    layout.primary_tileset, "primary", layout.layer_version
                )
            except MapEditorError:
                primary_dir = None
            secondary_dir: Path | None = None
            if layout.secondary_tileset:
                try:
                    secondary_dir = self.tileset_dir(
                        layout.secondary_tileset,
                        "secondary",
                        layout.layer_version,
                    )
                except MapEditorError:
                    secondary_dir = None

            if primary_dir is None:
                mark_incomplete("primary", layout.primary_tileset, None, layout.id)
            if layout.secondary_tileset and secondary_dir is None:
                mark_incomplete("secondary", layout.secondary_tileset, None, layout.id)

            primary_key = ("primary", str(primary_dir)) if primary_dir else None
            secondary_key = ("secondary", str(secondary_dir)) if secondary_dir else None

            try:
                blocks = self.read_blocks(layout)
            except MapEditorError:
                mark_incomplete(
                    "primary", layout.primary_tileset, primary_dir, layout.id
                )
                if layout.secondary_tileset:
                    mark_incomplete(
                        "secondary", layout.secondary_tileset, secondary_dir, layout.id
                    )
                continue

            border: tuple[int, ...] = ()
            border_missing = (
                bool(layout.border_filepath)
                and not self.layout_path(layout.border_filepath).is_file()
            )
            if border_missing:
                mark_incomplete(
                    "primary", layout.primary_tileset, primary_dir, layout.id
                )
                if layout.secondary_tileset:
                    mark_incomplete(
                        "secondary", layout.secondary_tileset, secondary_dir, layout.id
                    )
            else:
                try:
                    border = self.read_border(layout)
                except MapEditorError:
                    mark_incomplete(
                        "primary", layout.primary_tileset, primary_dir, layout.id
                    )
                    if layout.secondary_tileset:
                        mark_incomplete(
                            "secondary",
                            layout.secondary_tileset,
                            secondary_dir,
                            layout.id,
                        )

            for word in blocks:
                metatile_id = word & self.settings.metatile_id_mask
                counts[metatile_id] = counts.get(metatile_id, 0) + 1
                if metatile_id < self.settings.num_metatiles_in_primary:
                    if primary_key is not None:
                        usage.setdefault(primary_key, set()).add(metatile_id)
                elif secondary_key is not None:
                    usage.setdefault(secondary_key, set()).add(
                        metatile_id - self.settings.num_metatiles_in_primary
                    )
            for word in border:
                metatile_id = word & self.settings.metatile_id_mask
                counts[metatile_id] = counts.get(metatile_id, 0) + 1
                if metatile_id < self.settings.num_metatiles_in_primary:
                    if primary_key is not None:
                        usage.setdefault(primary_key, set()).add(metatile_id)
                elif secondary_key is not None:
                    usage.setdefault(secondary_key, set()).add(
                        metatile_id - self.settings.num_metatiles_in_primary
                    )

        self._metatile_usage_cache = usage
        self._metatile_ref_counts_cache = counts
        self._metatile_usage_incomplete = incomplete
        self._metatile_usage_incomplete_layouts = incomplete_layouts

    def tile_reference_counts(
        self, tileset, exclude_layout: str | None = None
    ) -> dict[int, int]:
        key = (tileset.kind, str(tileset.directory), exclude_layout)
        cached = self._tile_reference_counts_cache.get(key)
        if cached is not None:
            return cached
        counts: dict[int, int] = {}
        for layout in self.layouts.values():
            if exclude_layout is not None and layout.id == exclude_layout:
                continue
            if tileset.kind == "primary":
                if layout.primary_tileset != tileset.name:
                    continue
            elif layout.secondary_tileset != tileset.name:
                continue
            try:
                pair = self.load_tileset_pair(layout)
                blocks = list(self.read_blocks(layout))
                try:
                    blocks.extend(self.read_border(layout))
                except MapEditorError:
                    pass
            except MapEditorError:
                continue
            for word in blocks:
                metatile_id = word & self.settings.metatile_id_mask
                try:
                    owner = pair.metatile_owner(metatile_id)
                    metatile = owner.metatile_at(metatile_id)
                except MapEditorError:
                    continue
                for entry in metatile:
                    local = entry.tile_index - tileset.global_tile_base
                    if 0 <= local < len(tileset.patterns):
                        counts[local] = counts.get(local, 0) + 1
        self._tile_reference_counts_cache[key] = counts
        return counts

    def global_metatile_usage(self) -> dict[tuple[str, str], set[int]]:
        """Returns local metatile indices referenced by any map or border.

        The key is ``(kind, resolved_tileset_directory)``. If a layout cannot be
        read completely, the affected tilesets are recorded as incomplete and
        metatile removal/reuse is disabled for them (see
        :meth:`metatile_usage_complete`).
        """
        self._scan_metatile_references()
        assert self._metatile_usage_cache is not None
        return self._metatile_usage_cache

    def metatile_reference_counts(self) -> dict[int, int]:
        """Returns how many map/border cells reference each global metatile ID."""
        self._scan_metatile_references()
        assert self._metatile_ref_counts_cache is not None
        return self._metatile_ref_counts_cache

    def metatile_usage_complete(self, tileset) -> bool:
        """Whether all potentially-relevant layouts were readable during the usage scan."""
        self._scan_metatile_references()
        directory_key = (tileset.kind, str(tileset.directory))
        name_key = (tileset.kind, "name:" + self._normalize_query(tileset.name))
        return (
            directory_key not in self._metatile_usage_incomplete
            and name_key not in self._metatile_usage_incomplete
        )

    def metatile_usage_incomplete_layouts(self, tileset) -> list[str]:
        """Layout IDs that could not be read while scanning metatile usage."""
        self._scan_metatile_references()
        layouts: set[str] = set()
        for key in (
            (tileset.kind, str(tileset.directory)),
            (tileset.kind, "name:" + self._normalize_query(tileset.name)),
        ):
            layouts.update(self._metatile_usage_incomplete_layouts.get(key, set()))
        return sorted(layouts)

    def find_metatile_references(
        self, metatile_id: int
    ) -> list[tuple[str, int, int, str]]:
        """Returns ``(layout_id, x, y, source)`` for every map/border cell using a metatile."""
        references: list[tuple[str, int, int, str]] = []
        for layout in self.layouts.values():
            try:
                blocks = self.read_blocks(layout)
            except MapEditorError:
                continue
            for index, word in enumerate(blocks):
                if (word & self.settings.metatile_id_mask) == metatile_id:
                    references.append(
                        (
                            layout.id,
                            index % layout.width,
                            index // layout.width,
                            "map",
                        )
                    )
            try:
                border = self.read_border(layout)
            except MapEditorError:
                border = ()
            for index, word in enumerate(border):
                if (word & self.settings.metatile_id_mask) == metatile_id:
                    references.append((layout.id, index, 0, "border"))
        return references

    def update_layout_size(
        self, layout_id: str, width: int, height: int
    ) -> LayoutEntry:
        """Updates one flat layout entry in layouts.json without reformatting the file."""
        layout = self.layouts.get(layout_id)
        if layout is None:
            raise MapEditorError(f"Unknown layout '{layout_id}'.")
        if width <= 0 or height <= 0:
            raise MapEditorError("Layout dimensions must be positive.")
        if layout.width == width and layout.height == height:
            return layout

        path = self.paths["layouts_json"]
        text = path.read_text(encoding="utf-8")
        id_match = re.search(r'"id"\s*:\s*"' + re.escape(layout_id) + r'"', text)
        if id_match is None:
            raise MapEditorError(
                f"Could not find layout '{layout_id}' in {path}; refusing a blind rewrite."
            )
        end = text.find("}", id_match.end())
        if end < 0:
            raise MapEditorError(
                f"Malformed layout object for '{layout_id}' in {path}."
            )
        block = text[id_match.start() : end]
        block, width_count = re.subn(
            r'("width"\s*:\s*)(\d+)', rf"\g<1>{width}", block, count=1
        )
        block, height_count = re.subn(
            r'("height"\s*:\s*)(\d+)', rf"\g<1>{height}", block, count=1
        )
        if width_count != 1 or height_count != 1:
            raise MapEditorError(
                f"Could not safely update width/height for layout '{layout_id}'."
            )
        new_text = text[: id_match.start()] + block + text[end:]
        write_text_atomic(path, new_text)
        new_layout = replace(layout, width=width, height=height)
        self.layouts[layout_id] = new_layout
        return new_layout

    def update_layout_tilesets(
        self, layout_id: str, *, primary_tileset: str, secondary_tileset: str
    ) -> LayoutEntry:
        layout = self.layouts.get(layout_id)
        if layout is None:
            raise MapEditorError(f"Unknown layout '{layout_id}'.")
        if (
            layout.primary_tileset == primary_tileset
            and layout.secondary_tileset == secondary_tileset
        ):
            return layout
        path = self.paths["layouts_json"]
        text = path.read_text(encoding="utf-8")
        id_match = re.search(r'"id"\s*:\s*"' + re.escape(layout_id) + r'"', text)
        if id_match is None:
            raise MapEditorError(
                f"Could not find layout '{layout_id}' in {path}; refusing a blind rewrite."
            )
        end = text.find("}", id_match.end())
        if end < 0:
            raise MapEditorError(
                f"Malformed layout object for '{layout_id}' in {path}."
            )
        block = text[id_match.start() : end]
        block, primary_count = re.subn(
            r'("primary_tileset"\s*:\s*")[^"]*(")',
            rf"\g<1>{primary_tileset}\g<2>",
            block,
            count=1,
        )
        block, secondary_count = re.subn(
            r'("secondary_tileset"\s*:\s*")[^"]*(")',
            rf"\g<1>{secondary_tileset}\g<2>",
            block,
            count=1,
        )
        if primary_count != 1 or secondary_count != 1:
            raise MapEditorError(
                f"Could not safely update tilesets for layout '{layout_id}'."
            )
        new_text = text[: id_match.start()] + block + text[end:]
        write_text_atomic(path, new_text)
        new_layout = replace(
            layout,
            primary_tileset=primary_tileset,
            secondary_tileset=secondary_tileset,
        )
        self.layouts[layout_id] = new_layout
        return new_layout

    def _variant_registry_path(self, base) -> Path:
        return base.directory.parent / f"{base.directory.name}_variants.json"

    def _variant_registry(self, base) -> dict:
        path = self._variant_registry_path(base)
        if not path.is_file():
            return {
                "format": "poryeasy-tileset-variants",
                "version": 1,
                "base": base.name,
                "kind": base.kind,
                "variants": [],
            }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MapEditorError(f"Could not read {path}: {exc}") from exc
        data.setdefault("variants", [])
        return data

    def _write_variant_registry(self, base, data: dict) -> Path:
        path = self._variant_registry_path(base)
        write_text_atomic(path, json.dumps(data, indent=2) + "\n")
        return path

    def _register_variant_source(self, base, variant, palette_count: int) -> list[Path]:
        suffix = variant.name.removeprefix("gTileset_")
        tiles_var = f"gTilesetTiles_{suffix}"
        palettes_var = f"gTilesetPalettes_{suffix}"
        metatiles_var = f"gMetatiles_{suffix}"
        attributes_var = f"gMetatileAttributes_{suffix}"
        relative = variant.directory.relative_to(self.root).as_posix()
        paths: list[Path] = []
        tilesets_h = self.root / "include" / "tilesets.h"
        graphics_c = self.root / "src" / "graphics.c"
        if not graphics_c.is_file():
            graphics_c = self.root / "src" / "data" / "tilesets" / "graphics.h"
        metatiles_h = self.root / "src" / "data" / "tilesets" / "metatiles.h"
        headers_h = self.root / "src" / "data" / "tilesets" / "headers.h"
        for path in (tilesets_h, graphics_c, metatiles_h, headers_h):
            if not path.is_file():
                raise MapEditorError(
                    f"Cannot register {variant.name}: {path} is missing."
                )

        text = tilesets_h.read_text(encoding="utf-8", errors="replace")
        if tiles_var not in text:
            lines = (
                f"extern const u32 {tiles_var}[];\n"
                f"extern const u16 {palettes_var}[][16];\n"
                f"extern const struct Tileset {variant.name};\n"
            )
            marker = text.rfind("#endif")
            text = (
                text[:marker] + lines + text[marker:] if marker >= 0 else text + lines
            )
            write_text_atomic(tilesets_h, text)
            paths.append(tilesets_h)

        text = graphics_c.read_text(encoding="utf-8", errors="replace")
        if tiles_var not in text:
            palette_lines = "\n".join(
                f'    INCGFX_U16("{relative}/palettes/{index:02d}.pal", ".gbapal"),'
                for index in range(palette_count)
            )
            block = (
                f"\nconst u32 {tiles_var}[] = "
                f'INCGFX_U32("{relative}/tiles.png", ".4bpp");\n'
                f"const u16 ALIGNED(4) {palettes_var}[][16] =\n"
                "{\n"
                f"{palette_lines}\n"
                "};\n"
            )
            write_text_atomic(graphics_c, text + block)
            paths.append(graphics_c)

        text = metatiles_h.read_text(encoding="utf-8", errors="replace")
        if metatiles_var not in text:
            block = (
                f"\nconst u16 {metatiles_var}[] = "
                f'INCBIN_U16("{relative}/metatiles.bin");\n'
                f"const u16 {attributes_var}[] = "
                f'INCBIN_U16("{relative}/metatile_attributes.bin");\n'
            )
            write_text_atomic(metatiles_h, text + block)
            paths.append(metatiles_h)

        text = headers_h.read_text(encoding="utf-8", errors="replace")
        callback = "NULL"
        base_match = re.search(
            rf"const struct Tileset {re.escape(base.name)} =\s*\{{(.*?)\}};",
            text,
            re.DOTALL,
        )
        if base_match is not None:
            callback_match = re.search(
                r"\.callback\s*=\s*([^,\n]+)", base_match.group(1)
            )
            if callback_match is not None:
                callback = callback_match.group(1).strip()
        if f"const struct Tileset {variant.name} =" not in text:
            block = (
                f"\nconst struct Tileset {variant.name} =\n"
                "{\n"
                "    .isCompressed = FALSE,\n"
                f"    .isSecondary = {'TRUE' if base.kind == 'secondary' else 'FALSE'},\n"
                f"    .tiles = {tiles_var},\n"
                f"    .palettes = {palettes_var},\n"
                f"    .metatiles = {metatiles_var},\n"
                f"    .metatileAttributes = {attributes_var},\n"
                f"    .callback = {callback},\n"
                "};\n"
            )
            write_text_atomic(headers_h, text + block)
            paths.append(headers_h)
        return paths

    def _find_tileset_block(self, text: str, name: str) -> tuple[int, int] | None:
        start = text.find(f"const struct Tileset {name} =")
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
        return (start, index) if depth == 0 else None

    def shift_map_events(self, map_folder: Path, dx: int, dy: int) -> bool:
        """Shifts x/y coordinates in a map.json when the layout origin moves."""
        if dx == 0 and dy == 0:
            return False
        path = map_folder / "map.json"
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MapEditorError(
                f"Could not read {path} to shift events: {exc}"
            ) from exc
        for key in ("object_events", "warp_events", "coord_events", "bg_events"):
            for event in data.get(key, []) or []:
                if isinstance(event, dict):
                    if "x" in event:
                        event["x"] = int(event["x"]) + dx
                    if "y" in event:
                        event["y"] = int(event["y"]) + dy
        write_text_atomic(path, json.dumps(data, indent=2) + "\n")
        return True

    def invalidate_tile_usage(self) -> None:
        self._global_tile_usage_cache.clear()
        self._tile_reference_counts_cache.clear()

    def invalidate_metatile_usage(self) -> None:
        self._metatile_usage_cache = None
        self._metatile_ref_counts_cache = None
        self._metatile_usage_incomplete.clear()
        self._metatile_usage_incomplete_layouts.clear()

    def _load_protected_metatiles(self) -> dict[str, set[int]]:
        """Labels named in metatile_labels.h, grouped by their gTileset comment."""
        path = self.paths["metatile_labels"]
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8", errors="replace")
        protected: dict[str, set[int]] = {}
        current_tileset = ""
        for line in text.splitlines():
            comment = re.search(r"//\s*(gTileset_[A-Za-z0-9_]+)", line)
            if comment is not None:
                current_tileset = comment.group(1)
            match = re.match(
                r"\s*#define\s+METATILE_\w+\s+(0[xX][0-9a-fA-F]+|\d+)", line
            )
            if match is None:
                continue
            try:
                value = int(match.group(1), 0)
            except ValueError:
                continue
            protected.setdefault(current_tileset, set()).add(value)
        return protected

    def is_protected_metatile_id(
        self, metatile_id: int, tileset_name: str | None = None
    ) -> bool:
        if tileset_name is None:
            return metatile_id in self._protected_metatiles
        unscoped = self._protected_metatiles_by_tileset.get("", set())
        if metatile_id in unscoped:
            return True
        return metatile_id in self._protected_metatiles_by_tileset.get(
            tileset_name, set()
        )

    # Build metadata -----------------------------------------------------

    def graphics_h_text(self) -> str | None:
        path = self.paths["graphics_h"]
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return None

    def metatile_behavior_values(self) -> dict[str, int]:
        path = self.root / "include" / "constants" / "metatile_behaviors.h"
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"//[^\n]*", "", text)
        values: dict[str, int] = {}
        for block in re.findall(r"enum\s*\{(.*?)\}", text, re.DOTALL):
            current = 0
            for item in block.split(","):
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
        return values

    def graphics_h_tile_limit(self, tileset) -> int | None:
        rel = tileset.directory.relative_to(self.root).as_posix()
        if rel in self._graphics_h_tile_limit_cache:
            return self._graphics_h_tile_limit_cache[rel]
        text = self.graphics_h_text()
        if text is None:
            self._graphics_h_tile_limit_cache[rel] = None
            return None
        match = re.search(
            r'INCGFX_U\d+\("'
            + re.escape(rel)
            + r'/tiles\.png"[^;\n]*?-num_tiles\s+(\d+)',
            text,
        )
        value = int(match.group(1)) if match else None
        self._graphics_h_tile_limit_cache[rel] = value
        return value

    def update_graphics_h_tile_count(self, tileset, count: int) -> bool:
        """Updates the generated ``-num_tiles N`` limit for a tileset, if present.

        pokeemerald-expansion's generated ``src/data/tilesets/graphics.h``
        limits some tilesets with ``-num_tiles``. If we extend ``tiles.png``
        but leave that limit untouched, the build silently drops the new
        tiles. Returns True when the file was changed.
        """
        path = self.paths["graphics_h"]
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        rel = tileset.directory.relative_to(self.root).as_posix()
        pattern = re.compile(
            r'(INCGFX_U\d+\("'
            + re.escape(rel)
            + r'/tiles\.png"[^;\n]*?-num_tiles\s+)(\d+)'
        )
        match = pattern.search(text)
        if not match:
            return False
        new_text = text[: match.start(2)] + str(count) + text[match.end(2) :]
        if new_text == text:
            return False
        write_text_atomic(path, new_text)
        self._graphics_h_tile_limit_cache.pop(
            tileset.directory.relative_to(self.root).as_posix(), None
        )
        return True


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".ptmap-tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".ptmap-tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    tmp.replace(path)
