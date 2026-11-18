"""Tileset, palette, metatile and tile manipulation for Poryeasy."""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .errors import MapEditorError
from .project import Project, write_bytes_atomic, write_text_atomic

TILE_SIZE = 8
METATILE_SIZE = 16


@dataclass(frozen=True)
class TileRef:
    tile_index: int
    palette: int = 0
    hflip: bool = False
    vflip: bool = False

    def to_word(self) -> int:
        return (
            (self.tile_index & 0x03FF)
            | (0x0400 if self.hflip else 0)
            | (0x0800 if self.vflip else 0)
            | ((self.palette & 0x0F) << 12)
        )

    @staticmethod
    def from_word(word: int) -> TileRef:
        return TileRef(
            tile_index=word & 0x03FF,
            palette=(word >> 12) & 0x0F,
            hflip=bool(word & 0x0400),
            vflip=bool(word & 0x0800),
        )


def load_jasc_palette(path: Path) -> list[tuple[int, int, int]]:
    """Loads a JASC-PAL or 48-byte raw RGB palette, returning 16 RGB colors."""
    colors: list[tuple[int, int, int]] = []
    try:
        raw = path.read_bytes()
    except OSError:
        return [(0, 0, 0)] * 16
    text = raw.decode("ascii", errors="ignore")
    if text.startswith("JASC-PAL"):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 3:
            try:
                count = int(lines[2])
            except ValueError:
                count = 0
            for line in lines[3 : 3 + count]:
                parts = line.replace("\t", " ").split()
                if len(parts) >= 3:
                    try:
                        colors.append(tuple(int(v) & 0xFF for v in parts[:3]))  # type: ignore[arg-type]
                    except ValueError:
                        colors.append((0, 0, 0))
    elif len(raw) >= 48:
        # Some projects store raw RGB triples rather than JASC text.
        colors = [tuple(raw[i : i + 3]) for i in range(0, 48, 3)]  # type: ignore[misc]
    colors = colors[:16]
    colors.extend([(0, 0, 0)] * (16 - len(colors)))
    return colors


def _save_palette_file(path: Path, colors: list[tuple[int, int, int]]) -> None:
    padded = list(colors[:16])
    padded.extend([(0, 0, 0)] * (16 - len(padded)))
    try:
        raw = path.read_bytes()
    except OSError:
        raw = b""
    if raw.startswith(b"JASC-PAL"):
        lines = ["JASC-PAL", "0100", "16"]
        lines.extend(f"{r} {g} {b}" for r, g, b in padded)
        write_text_atomic(path, "\n".join(lines) + "\n")
    else:
        write_bytes_atomic(path, bytes(value for color in padded for value in color))


def flip_pattern(pattern: bytes, hflip: bool, vflip: bool) -> bytes:
    if not hflip and not vflip:
        return pattern
    out = bytearray(64)
    for y in range(TILE_SIZE):
        src_y = TILE_SIZE - 1 - y if vflip else y
        row = pattern[src_y * TILE_SIZE : (src_y + 1) * TILE_SIZE]
        if hflip:
            row = row[::-1]
        out[y * TILE_SIZE : (y + 1) * TILE_SIZE] = row
    return bytes(out)


def patch_from_rgba(data: bytes, width: int, x: int, y: int, size: int = 8) -> bytes:
    out = bytearray(size * size * 4)
    for row in range(size):
        src = ((y + row) * width + x) * 4
        out[row * size * 4 : (row + 1) * size * 4] = data[src : src + size * 4]
    return bytes(out)


class Tileset:
    kind: str
    name: str
    directory: Path
    tiles_width: int
    tiles_height: int
    entries_per_metatile: int
    metatiles: list[list[TileRef]]
    attributes: list[bytes]
    attribute_size: int
    palettes: list[list[tuple[int, int, int]]]
    patterns: list[bytes]
    dirty_tiles: bool
    dirty_metadata: bool
    _reserved_tiles: set[int]
    _reserved_metatiles: set[int]
    _tile_matcher: dict[bytes, list[tuple[int, bool, bool]]] | None
    _protected_tiles: set[int] | None

    def __init__(
        self,
        project: Project,
        name: str,
        kind: str,
        directory: Path,
        tiles_image: Image.Image,
        metatiles: list[list[TileRef]],
        attributes: list[bytes],
        attribute_size: int,
        palettes: list[list[tuple[int, int, int]]],
    ) -> None:
        self.project = project
        self.name = name
        self.kind = kind
        self.directory = directory
        self.tiles_width, self.tiles_height = tiles_image.size
        self.entries_per_metatile = project.settings.entries_per_metatile
        self.metatiles = metatiles
        self.attributes = attributes
        self.attribute_size = attribute_size
        self.palettes = palettes
        self.patterns = self._extract_patterns(tiles_image)
        self.dirty_tiles = False
        self.dirty_metadata = False
        self.dirty_palette_indices: set[int] = set()
        self.overflowed = False
        self._reserved_tiles = set()
        self._reserved_metatiles = set()
        self._tile_matcher = None
        self._protected_tiles = None
        self._uses_global_palette_indices: bool | None = None
        self._animated_indices_cache: set[int] | None = None

    @property
    def tiles_per_row(self) -> int:
        return self.tiles_width // TILE_SIZE

    @property
    def local_tile_count(self) -> int:
        return len(self.patterns)

    @property
    def global_tile_base(self) -> int:
        return (
            0 if self.kind == "primary" else self.project.settings.num_tiles_in_primary
        )

    @property
    def max_local_tiles(self) -> int:
        if self.kind == "primary":
            return self.project.settings.num_tiles_in_primary
        return self.project.settings.num_tiles_secondary

    @property
    def global_metatile_base(self) -> int:
        return (
            0
            if self.kind == "primary"
            else self.project.settings.num_metatiles_in_primary
        )

    @property
    def max_local_metatiles(self) -> int:
        if self.kind == "primary":
            return self.project.settings.num_metatiles_in_primary
        return self.project.settings.num_metatiles_secondary

    @property
    def tiles_png_path(self) -> Path:
        return self.directory / "tiles.png"

    @property
    def metatiles_path(self) -> Path:
        return self.directory / "metatiles.bin"

    @property
    def attributes_path(self) -> Path:
        return self.directory / "metatile_attributes.bin"

    def palette_path(self, local: int) -> Path:
        return self.directory / "palettes" / f"{local:02d}.pal"

    def uses_global_palette_indices(self) -> bool:
        """Whether this tileset's .pal files are numbered by global palette bank.

        Expansion stores sixteen entries in gTilesetPalettes_* and addresses
        secondary palettes by their final global index (6..12). Older projects
        store only the secondary window and use local indices (0..6).
        """
        if self.kind != "secondary":
            return False
        if self._uses_global_palette_indices is not None:
            return self._uses_global_palette_indices
        self._uses_global_palette_indices = False
        text = (
            self.project.graphics_h_text()
            if hasattr(self.project, "graphics_h_text")
            else None
        )
        if text is not None:
            palette_var = self.name.replace("gTileset_", "gTilesetPalettes_", 1)
            match = re.search(
                rf"const\s+u16\s+(?:ALIGNED\(4\)\s+)?{re.escape(palette_var)}"
                r"\[\]\[16\]\s*=\s*\{(.*?)\};",
                text,
                re.DOTALL,
            )
            if match is not None:
                count = len(re.findall(r"INC(?:GFX|BIN)_U16", match.group(1)))
                self._uses_global_palette_indices = count > (
                    self.project.settings.num_pals_total
                    - self.project.settings.num_pals_in_primary
                )
        return self._uses_global_palette_indices

    def palette_file_index(self, bank: int) -> int:
        if self.kind == "primary":
            return bank
        if self.uses_global_palette_indices():
            return bank
        return bank - self.project.settings.num_pals_in_primary

    # Loading ------------------------------------------------------------

    @classmethod
    def load(cls, project: Project, name: str, kind: str, directory: Path) -> Tileset:
        tiles_path = directory / "tiles.png"
        metatiles_path = directory / "metatiles.bin"
        attributes_path = directory / "metatile_attributes.bin"
        for path in (tiles_path, metatiles_path, attributes_path):
            if not path.is_file():
                raise MapEditorError(f"Missing tileset artifact: {path}")

        try:
            with Image.open(tiles_path) as image:
                image.load()
                if image.mode != "P":
                    raise MapEditorError(
                        f"{tiles_path} is not an indexed PNG (mode {image.mode}). "
                        "Porymap tilesets must be indexed PNGs."
                    )
                if image.width % TILE_SIZE or image.height % TILE_SIZE:
                    raise MapEditorError(
                        f"{tiles_path} dimensions are not multiples of 8."
                    )

                meta_bytes = metatiles_path.read_bytes()
                if len(meta_bytes) % 2:
                    raise MapEditorError(f"{metatiles_path} has an odd byte length.")
                words = struct.unpack(f"<{len(meta_bytes) // 2}H", meta_bytes)

                entries_per = project.settings.entries_per_metatile
                if words and len(words) % entries_per != 0:
                    # Header may be missing or stale: infer the only plausible layer mode.
                    candidates = [ep for ep in (8, 12) if len(words) % ep == 0]
                    if len(candidates) == 1:
                        entries_per = candidates[0]
                    else:
                        raise MapEditorError(
                            f"{metatiles_path} has {len(words)} 16-bit entries, which is not divisible by "
                            f"{entries_per}. Use --layer-mode dual|triple explicitly."
                        )
                metatiles = [
                    [TileRef.from_word(word) for word in words[i : i + entries_per]]
                    for i in range(0, len(words), entries_per)
                ]
                if not metatiles:
                    raise MapEditorError(f"{metatiles_path} contains no metatiles.")

                attr_bytes = attributes_path.read_bytes()
                if len(attr_bytes) % len(metatiles) != 0:
                    raise MapEditorError(
                        f"{attributes_path} length ({len(attr_bytes)}) is not divisible by the "
                        f"metatile count ({len(metatiles)})."
                    )
                attribute_size = len(attr_bytes) // len(metatiles)
                if attribute_size not in (2, 4):
                    raise MapEditorError(
                        f"{attributes_path} implies a {attribute_size}-byte attribute, "
                        "but only 2-byte (Emerald) and 4-byte (FRLG) attributes are supported."
                    )
                attributes = [
                    attr_bytes[i : i + attribute_size]
                    for i in range(0, len(attr_bytes), attribute_size)
                ]

                palettes_dir = directory / "palettes"
                palettes = [
                    load_jasc_palette(palettes_dir / f"{index:02d}.pal")
                    for index in range(16)
                ]

                tileset = cls(
                    project,
                    name,
                    kind,
                    directory,
                    image,
                    metatiles,
                    attributes,
                    attribute_size,
                    palettes,
                )
        except OSError as exc:
            raise MapEditorError(f"Could not read {tiles_path}: {exc}") from exc
        tileset.entries_per_metatile = entries_per
        return tileset

    def _extract_patterns(self, image: Image.Image) -> list[bytes]:
        data = image.tobytes()
        row_bytes = image.width
        patterns: list[bytes] = []
        for ty in range(image.height // TILE_SIZE):
            for tx in range(image.width // TILE_SIZE):
                out = bytearray(64)
                for row in range(TILE_SIZE):
                    src = (ty * TILE_SIZE + row) * row_bytes + tx * TILE_SIZE
                    out[row * TILE_SIZE : (row + 1) * TILE_SIZE] = data[
                        src : src + TILE_SIZE
                    ]
                patterns.append(bytes(out))
        return patterns

    # Basic access -------------------------------------------------------

    def pattern_at(self, global_index: int) -> bytes:
        if global_index < self.global_tile_base:
            return bytes(64)
        local = global_index - self.global_tile_base
        if 0 <= local < len(self.patterns):
            return self.patterns[local]
        return bytes(64)

    def metatile_at(self, global_id: int) -> list[TileRef]:
        local = global_id - self.global_metatile_base
        if not 0 <= local < len(self.metatiles):
            raise MapEditorError(
                f"Metatile 0x{global_id:03X} is out of range for {self.name} "
                f"({len(self.metatiles)} metatiles)."
            )
        return self.metatiles[local]

    def attribute_at(self, global_id: int) -> bytes:
        local = global_id - self.global_metatile_base
        if not 0 <= local < len(self.attributes):
            return bytes(self.attribute_size)
        return self.attributes[local]

    def entries_for_layer(
        self, metatile: list[TileRef], layer: int
    ) -> list[TileRef | None]:
        """Returns the four entries for a physical layer (0=bottom,1=middle,2=top)."""
        if not 0 <= layer <= 2:
            raise MapEditorError(f"Invalid layer index {layer}.")
        if self.entries_per_metatile == 12:
            return list(metatile[layer * 4 : (layer + 1) * 4])
        if layer == 0:
            return list(metatile[0:4])
        if layer == 2:
            return list(metatile[4:8])
        return [None, None, None, None]

    # Tile matching ------------------------------------------------------

    def _build_tile_matcher(self) -> dict[bytes, list[tuple[int, bool, bool]]]:
        if self._tile_matcher is not None:
            return self._tile_matcher
        matcher: dict[bytes, list[tuple[int, bool, bool]]] = {}
        for local, pattern in enumerate(self.patterns):
            if not any(pattern):
                continue
            global_index = self.global_tile_base + local
            seen: set[tuple[bool, bool]] = set()
            for hflip in (False, True):
                for vflip in (False, True):
                    flipped = flip_pattern(pattern, hflip, vflip)
                    key = (hflip, vflip)
                    if key in seen:
                        continue
                    seen.add(key)
                    matcher.setdefault(flipped, []).append((global_index, hflip, vflip))
        self._tile_matcher = matcher
        return matcher

    # Palettes / quantisation -------------------------------------------

    def palette(self, index: int) -> list[tuple[int, int, int]]:
        if 0 <= index < len(self.palettes):
            return self.palettes[index]
        return [(0, 0, 0)] * 16

    def quantize_patch(
        self,
        patch: bytes,
        *,
        allowed_palettes: list[int] | None = None,
        preferred_palette: int | None = None,
        mode: str = "nearest",
        palette_bank: list[list[tuple[int, int, int]]] | None = None,
    ) -> tuple[
        bytes, int, bool, list[tuple[tuple[int, int, int], tuple[int, int, int]]]
    ]:
        """Converts a 8x8 RGBA patch to palette indices.

        Returns ``(pattern, palette_index, exact, substitutions)``.
        Transparent pixels (alpha == 0) become index 0. Opaque pixels are
        matched against palette slots 1..15 (slot 0 is always transparent on
        GBA hardware).
        """
        if allowed_palettes is None:
            allowed_palettes = list(range(16))
        if palette_bank is None:
            palette_bank = self.palettes

        colors: list[tuple[int, int, int] | None] = []
        for i in range(0, len(patch), 4):
            r, g, b, a = patch[i : i + 4]
            colors.append(None if a == 0 else (r, g, b))

        opaque = [color for color in colors if color is not None]
        unique = list(dict.fromkeys(opaque))
        if not unique:
            return (
                bytes(64),
                preferred_palette if preferred_palette is not None else 0,
                True,
                [],
            )

        exact_palettes: list[int] = []
        for palette_index in allowed_palettes:
            palette = (
                palette_bank[palette_index]
                if palette_index < len(palette_bank)
                else self.palette(palette_index)
            )
            available = {palette[i] for i in range(1, 16)}
            if all(color in available for color in unique):
                exact_palettes.append(palette_index)
        if exact_palettes:
            chosen = (
                preferred_palette
                if preferred_palette in exact_palettes
                else exact_palettes[0]
            )
            palette = palette_bank[chosen]
            lookup = {palette[i]: i for i in range(1, 16)}
            indices = [0 if color is None else lookup[color] for color in colors]
            return bytes(indices), chosen, True, []

        if mode == "error":
            detail = ", ".join(f"#{r:02X}{g:02X}{b:02X}" for r, g, b in unique[:8])
            raise MapEditorError(
                "The edited tile uses colours that do not exist in any allowed palette: "
                + detail
            )

        # Nearest palette/colour fallback. Pick the palette that minimises the
        # total squared error over the unique colours in the patch.
        best_palette = allowed_palettes[0]
        best_indices: dict[tuple[int, int, int], int] = {}
        best_error = math.inf
        for palette_index in allowed_palettes:
            palette = (
                palette_bank[palette_index]
                if palette_index < len(palette_bank)
                else self.palette(palette_index)
            )
            indices: dict[tuple[int, int, int], int] = {}
            error = 0
            for color in unique:
                nearest = min(
                    range(1, 16),
                    key=lambda slot: (
                        (palette[slot][0] - color[0]) ** 2
                        + (palette[slot][1] - color[1]) ** 2
                        + (palette[slot][2] - color[2]) ** 2
                    ),
                )
                mapped = palette[nearest]
                indices[color] = nearest
                error += (
                    (mapped[0] - color[0]) ** 2
                    + (mapped[1] - color[1]) ** 2
                    + (mapped[2] - color[2]) ** 2
                )
            if error < best_error:
                best_error = error
                best_palette = palette_index
                best_indices = indices

        palette = palette_bank[best_palette]
        substitutions = [
            (color, palette[best_indices[color]])
            for color in unique
            if palette[best_indices[color]] != color
        ]
        indices = [0 if color is None else best_indices[color] for color in colors]
        return bytes(indices), best_palette, False, substitutions

    # Allocation ---------------------------------------------------------

    def animated_tile_indices(self) -> set[int]:
        from .animations import project_animation_tiles

        if self._animated_indices_cache is None:
            self._animated_indices_cache = set(
                project_animation_tiles(self.project, self.kind, self.name)
            )
        return self._animated_indices_cache

    def _protected_tile_indices(self) -> set[int]:
        if self._protected_tiles is not None:
            return self._protected_tiles
        protected: set[int] = set(self.animated_tile_indices())
        anim_dir = self.directory / "anim"
        if anim_dir.is_dir():
            patterns = {pattern for pattern in self.patterns if any(pattern)}
            for path in anim_dir.rglob("*.png"):
                try:
                    with Image.open(path) as image:
                        image.load()
                except OSError:
                    continue
                if image.mode != "P":
                    continue
                data = image.tobytes()
                width = image.width
                for ty in range(image.height // TILE_SIZE):
                    for tx in range(image.width // TILE_SIZE):
                        pattern = bytearray(64)
                        for row in range(8):
                            src = (ty * 8 + row) * width + tx * 8
                            pattern[row * 8 : (row + 1) * 8] = data[src : src + 8]
                        pattern_bytes = bytes(pattern)
                        if not any(pattern_bytes):
                            continue
                        if pattern_bytes in patterns:
                            for local, existing in enumerate(self.patterns):
                                if existing == pattern_bytes:
                                    protected.add(self.global_tile_base + local)
        self._protected_tiles = protected
        return protected

    def has_free_tile_slot(self) -> bool:
        used = (
            self.project.tile_usage_for(self)
            | self._protected_tile_indices()
            | self._reserved_tiles
        )
        if len(self.patterns) < self.max_local_tiles and (
            self.global_tile_base + len(self.patterns) not in used
        ):
            return True
        for local in range(1, len(self.patterns)):
            global_index = self.global_tile_base + local
            if global_index in used:
                continue
            return True
        return False

    def free_tile_slots(self) -> int:
        used = (
            self.project.tile_usage_for(self)
            | self._protected_tile_indices()
            | self._reserved_tiles
        )
        return sum(
            1
            for local in range(1, self.max_local_tiles)
            if self.global_tile_base + local not in used
        )

    def has_free_metatile_slot(self) -> bool:
        if len(self.metatiles) < self.max_local_metatiles:
            return True
        if not self.project.metatile_usage_complete(self):
            return False
        usage = self.project.global_metatile_usage().get(
            (self.kind, str(self.directory)), set()
        )
        for local in range(len(self.metatiles)):
            global_id = self.global_metatile_base + local
            if global_id in usage or local in self._reserved_metatiles:
                continue
            if self.project.is_protected_metatile_id(global_id, self.name):
                continue
            return True
        return False

    def allocate_tile(self, pattern: bytes) -> int:
        """Adds ``pattern`` to this tileset and returns its global tile index."""
        if not any(pattern):
            return 0
        used = (
            self.project.tile_usage_for(self)
            | self._protected_tile_indices()
            | self._reserved_tiles
        )
        local = None

        # Appending is the least invasive option.
        if len(self.patterns) < self.max_local_tiles:
            candidate = len(self.patterns)
            if self.global_tile_base + candidate not in used:
                local = candidate

        if local is None:
            for candidate in range(1, len(self.patterns)):
                global_index = self.global_tile_base + candidate
                if global_index in used:
                    continue
                local = candidate
                break

        if local is None:
            # Let the import finish and create a per-map variant before saving.
            # The temporary overflow index is never serialized.
            local = len(self.patterns)
            self.overflowed = True

        if local == len(self.patterns):
            self.patterns.append(pattern)
        else:
            self.patterns[local] = pattern
        self._reserved_tiles.add(self.global_tile_base + local)
        self._tile_matcher = None
        self.dirty_tiles = True
        return self.global_tile_base + local

    def append_tile_range(self, patterns: list[bytes]) -> int:
        start = len(self.patterns)
        if start + len(patterns) > self.max_local_tiles:
            raise MapEditorError(
                f"No contiguous tile slots remain in {self.name} for {len(patterns)} "
                "new animation tiles."
            )
        self.patterns.extend(patterns)
        self._reserved_tiles.update(
            range(
                self.global_tile_base + start,
                self.global_tile_base + start + len(patterns),
            )
        )
        self._tile_matcher = None
        self.dirty_tiles = True
        return self.global_tile_base + start

    def allocate_metatile(
        self,
        entries: list[TileRef],
        attribute: bytes | None = None,
        *,
        prefer_unused: bool = False,
    ) -> int:
        used = set(self._reserved_metatiles)
        local = None
        usage: set[int] | None = None
        if prefer_unused or len(self.metatiles) >= self.max_local_metatiles:
            if not self.project.metatile_usage_complete(self):
                if len(self.metatiles) < self.max_local_metatiles:
                    local = len(self.metatiles)
                else:
                    raise MapEditorError(
                        f"Cannot safely reuse a metatile slot in {self.name}: not every layout "
                        "could be read, so usage counts are incomplete. Use add-secondary or fix "
                        "the missing/invalid layout files first."
                    )
            else:
                usage = self.project.global_metatile_usage().get(
                    (self.kind, str(self.directory)), set()
                )
                for candidate in range(len(self.metatiles)):
                    global_id = self.global_metatile_base + candidate
                    if candidate in usage or candidate in used:
                        continue
                    if self.project.is_protected_metatile_id(global_id, self.name):
                        continue
                    local = candidate
                    break
        if local is None and len(self.metatiles) < self.max_local_metatiles:
            local = len(self.metatiles)
        if local is None:
            if usage is None:
                if not self.project.metatile_usage_complete(self):
                    raise MapEditorError(
                        f"Cannot safely reuse a metatile slot in {self.name}: not every layout "
                        "could be read, so usage counts are incomplete. Use add-secondary or fix "
                        "the missing/invalid layout files first."
                    )
                usage = self.project.global_metatile_usage().get(
                    (self.kind, str(self.directory)), set()
                )
            for candidate in range(len(self.metatiles)):
                global_id = self.global_metatile_base + candidate
                if candidate in usage or candidate in used:
                    continue
                if self.project.is_protected_metatile_id(global_id, self.name):
                    continue
                local = candidate
                break
        if local is None:
            raise MapEditorError(
                f"No free metatile slots remain in {self.name}. "
                "Use a different tileset or free some metatiles first."
            )

        if len(entries) != self.entries_per_metatile:
            raise MapEditorError(
                f"Refusing to add a metatile with {len(entries)} entries to a "
                f"{self.entries_per_metatile}-entry tileset."
            )
        if local == len(self.metatiles):
            self.metatiles.append(list(entries))
            self.attributes.append(attribute or bytes(self.attribute_size))
        else:
            self.metatiles[local] = list(entries)
            self.attributes[local] = attribute or bytes(self.attribute_size)
        self._reserved_metatiles.add(local)
        self.dirty_metadata = True
        return self.global_metatile_base + local

    # Saving -------------------------------------------------------------

    def _render_tiles_image_bytes(self) -> tuple[bytes, int, int]:
        count = len(self.patterns)
        tiles_per_row = max(self.tiles_per_row, 1)
        rows = max(1, math.ceil(count / tiles_per_row))
        width = tiles_per_row * TILE_SIZE
        height = max(self.tiles_height, rows * TILE_SIZE)
        # Keep the original height if it already fits; otherwise extend by rows.
        height = math.ceil(height / TILE_SIZE) * TILE_SIZE
        data = bytearray(width * height)
        for local, pattern in enumerate(self.patterns):
            tx = local % tiles_per_row
            ty = local // tiles_per_row
            for row in range(8):
                dest = (ty * 8 + row) * width + tx * 8
                data[dest : dest + 8] = pattern[row * 8 : (row + 1) * 8]
        return bytes(data), width, height

    def save(self) -> list[Path]:
        written: list[Path] = []
        if self.dirty_metadata:
            words: list[int] = []
            for metatile in self.metatiles:
                if len(metatile) != self.entries_per_metatile:
                    raise MapEditorError(
                        f"Metatile in {self.name} has the wrong entry count."
                    )
                words.extend(entry.to_word() for entry in metatile)
            write_bytes_atomic(
                self.metatiles_path, struct.pack(f"<{len(words)}H", *words)
            )
            attributes = b"".join(
                attribute[: self.attribute_size].ljust(self.attribute_size, b"\0")
                for attribute in self.attributes
            )
            write_bytes_atomic(self.attributes_path, attributes)
            written.extend([self.metatiles_path, self.attributes_path])
        if self.dirty_tiles:
            data, width, height = self._render_tiles_image_bytes()
            image = Image.frombytes("P", (width, height), data)
            palette = bytearray(768)
            for index in range(16):
                palette[index * 3 : index * 3 + 3] = bytes(
                    (index * 17, index * 17, index * 17)
                )
            image.putpalette(bytes(palette))
            tmp = self.tiles_png_path.with_name("tiles.png.ptmap-tmp")
            image.save(tmp, format="PNG", optimize=False, compress_level=9)
            tmp.replace(self.tiles_png_path)
            written.append(self.tiles_png_path)
            count = self.highest_non_transparent_tile() + 1
            if self.project.update_graphics_h_tile_count(self, count):
                written.append(self.project.paths["graphics_h"])
        for local in sorted(self.dirty_palette_indices):
            path = self.palette_path(local)
            _save_palette_file(path, self.palettes[local])
            written.append(path)
        self.dirty_palette_indices.clear()
        return written

    def highest_non_transparent_tile(self) -> int:
        for index in range(len(self.patterns) - 1, -1, -1):
            if self.global_tile_base + index in self._reserved_tiles or any(
                self.patterns[index]
            ):
                return index
        return 0

    def invalidate_caches(self) -> None:
        self._tile_matcher = None
        self.project._global_tile_usage_cache.clear()
        self.project.invalidate_metatile_usage()


class TilesetPair:
    """The primary/secondary tileset pair used by one layout."""

    def __init__(self, project: Project, primary: Tileset, secondary: Tileset) -> None:
        self.project = project
        self.primary = primary
        self.secondary = secondary
        self._signature_index: dict[tuple[bytes, bytes, bytes], list[int]] | None = None
        self._animated_index_cache: set[int] | None = None
        self.palette_mode = "smart"
        self.palette_rewrites = []
        self.tile_match_tolerance = 0
        self.last_match_was_fuzzy = False
        self.last_match_difference = 0

    @property
    def entries_per_metatile(self) -> int:
        return self.primary.entries_per_metatile

    def palette_bank(self) -> list[list[tuple[int, int, int]]]:
        """The 16 hardware palette banks visible with this layout."""
        banks: list[list[tuple[int, int, int]]] = []
        primary_count = min(self.project.settings.num_pals_in_primary, 16)
        for index in range(16):
            if index < primary_count:
                banks.append(self.primary.palette(index))
            else:
                banks.append(
                    self.secondary.palette(self.secondary.palette_file_index(index))
                )
        return banks

    def palette_for(self, index: int) -> list[tuple[int, int, int]]:
        banks = self.palette_bank()
        if 0 <= index < len(banks):
            return banks[index]
        return [(0, 0, 0)] * 16

    def palette_owner(self, index: int):
        if index < self.project.settings.num_pals_in_primary:
            return self.primary, index
        local = self.secondary.palette_file_index(index)
        if 0 <= local < len(self.secondary.palettes):
            return self.secondary, local
        return None, -1

    def animated_tile_indices(self) -> set[int]:
        if self._animated_index_cache is None:
            self._animated_index_cache = (
                self.primary.animated_tile_indices()
                | self.secondary.animated_tile_indices()
            )
        return self._animated_index_cache

    def animated_layers_for_metatile(self, metatile: list[TileRef]) -> list[int]:
        animated = self.animated_tile_indices()
        if not animated:
            return []
        layers: list[int] = []
        for layer in range(3):
            entries = self.metatile_owner(0).entries_for_layer(metatile, layer)
            if any(
                entry is not None and entry.tile_index in animated for entry in entries
            ):
                layers.append(layer)
        return layers

    def palette_animation_tilesets(self) -> list[str]:
        from .animations import project_palette_only_animation

        result: list[str] = []
        for tileset in (self.primary, self.secondary):
            if project_palette_only_animation(self.project, tileset.kind, tileset.name):
                result.append(tileset.name)
        return result

    def tileset_for_tile(self, global_index: int) -> Tileset:
        if global_index < self.project.settings.num_tiles_in_primary:
            return self.primary
        return self.secondary

    def metatile_owner(self, global_id: int) -> Tileset:
        if global_id < self.project.settings.num_metatiles_in_primary:
            return self.primary
        return self.secondary

    def metatile(self, global_id: int) -> list[TileRef]:
        return self.metatile_owner(global_id).metatile_at(global_id)

    def attribute(self, global_id: int) -> bytes:
        return self.metatile_owner(global_id).attribute_at(global_id)

    # Rendering ----------------------------------------------------------

    def render_layer_patch(
        self,
        metatile: list[TileRef],
        layer: int,
        overrides: dict[int, bytes] | None = None,
    ) -> bytes:
        """Renders one 16x16 layer as raw RGBA bytes (1024 * 4)."""
        owner = self.metatile_owner(0)  # only used for entries_per_metatile
        entries = owner.entries_for_layer(metatile, layer)
        out = bytearray(16 * 16 * 4)
        for subtile_index, entry in enumerate(entries):
            if entry is None:
                continue
            sx = (subtile_index % 2) * 8
            sy = (subtile_index // 2) * 8
            tileset = self.tileset_for_tile(entry.tile_index)
            pattern = overrides.get(entry.tile_index) if overrides is not None else None
            if pattern is None:
                pattern = tileset.pattern_at(entry.tile_index)
            pattern = flip_pattern(pattern, entry.hflip, entry.vflip)
            palette = self.palette_for(entry.palette)
            for row in range(8):
                for col in range(8):
                    index = pattern[row * 8 + col]
                    pos = ((sy + row) * 16 + sx + col) * 4
                    if index == 0:
                        continue
                    r, g, b = palette[index] if index < len(palette) else (0, 0, 0)
                    out[pos : pos + 4] = bytes((r, g, b, 255))
        return bytes(out)

    def render_layer_patch_for(self, metatile_global_id: int, layer: int) -> bytes:
        return self.render_layer_patch(self.metatile(metatile_global_id), layer)

    def render_map_layers(
        self, layout, blocks: list[int]
    ) -> tuple[bytes, bytes, bytes]:
        """Renders the full map layers as raw RGBA bytes."""
        width, height = layout.size
        pixel_width = width * METATILE_SIZE
        pixel_height = height * METATILE_SIZE
        layers = [bytearray(pixel_width * pixel_height * 4) for _ in range(3)]
        for index, word in enumerate(blocks):
            bx = index % width
            by = index // width
            metatile_id = word & self.project.settings.metatile_id_mask
            try:
                metatile = self.metatile(metatile_id)
            except MapEditorError:
                continue
            for layer in range(3):
                patch = self.render_layer_patch(metatile, layer)
                for row in range(METATILE_SIZE):
                    dest = (
                        (by * METATILE_SIZE + row) * pixel_width + bx * METATILE_SIZE
                    ) * 4
                    src = row * METATILE_SIZE * 4
                    layers[layer][dest : dest + METATILE_SIZE * 4] = patch[
                        src : src + METATILE_SIZE * 4
                    ]
        return tuple(bytes(layer) for layer in layers)  # type: ignore[return-value]

    def metatile_signature(self, global_id: int) -> tuple[bytes, bytes, bytes]:
        metatile = self.metatile(global_id)
        return tuple(self.render_layer_patch(metatile, layer) for layer in range(3))  # type: ignore[return-value]

    def signature_index(self) -> dict[tuple[bytes, bytes, bytes], list[int]]:
        if self._signature_index is None:
            index: dict[tuple[bytes, bytes, bytes], list[int]] = {}
            for local in range(len(self.primary.metatiles)):
                global_id = self.primary.global_metatile_base + local
                key = self.metatile_signature(global_id)
                index.setdefault(key, []).append(global_id)
            for local in range(len(self.secondary.metatiles)):
                global_id = self.secondary.global_metatile_base + local
                key = self.metatile_signature(global_id)
                index.setdefault(key, []).append(global_id)
            self._signature_index = index
        return self._signature_index

    def invalidate_signature_index(self) -> None:
        self._signature_index = None

    def find_matching_metatile(
        self, signatures: tuple[bytes, bytes, bytes], *, global_search: bool = True
    ) -> int | None:
        for global_id in self.signature_index().get(signatures, ()):
            if global_search:
                return global_id
            owner = self.metatile_owner(global_id)
            if owner.kind == "secondary":
                return global_id
        return None

    # Palette / tile resolution -----------------------------------------

    def resolve_tile(
        self,
        patch: bytes,
        *,
        scope: str,
        preferred_palette: int | None = None,
        preferred_tile: int | None = None,
        quantize: str = "nearest",
        tolerance: int | None = None,
    ) -> tuple[
        TileRef,
        bool,
        bytes,
        int,
        list[tuple[tuple[int, int, int], tuple[int, int, int]]],
    ]:
        """Resolves an edited 8x8 RGBA patch into a tile/palette entry.

        ``scope`` is ``primary`` (only primary tile slots may be referenced) or
        ``combined`` (primary and the current secondary).
        """
        allowed_tiles = range(self.project.settings.num_tiles_in_primary)
        if scope != "primary":
            allowed_tiles = range(self.project.settings.num_tiles_total)

        if scope == "primary":
            palette_candidates = list(range(self.project.settings.num_pals_in_primary))
        else:
            palette_candidates = list(range(self.project.settings.num_pals_total))
        if preferred_palette is not None:
            palette_candidates = [preferred_palette] + [
                index for index in palette_candidates if index != preferred_palette
            ]
        pattern, palette, exact, substitutions = self.secondary.quantize_patch(
            patch,
            allowed_palettes=palette_candidates,
            preferred_palette=preferred_palette,
            mode=quantize,
            palette_bank=self.palette_bank(),
        )

        result = self._find_existing_tile(
            pattern, allowed_tiles, preferred_tile, tolerance=tolerance
        )
        if result is not None:
            tile_index, hflip, vflip = result
            return (
                TileRef(tile_index, palette, hflip, vflip),
                exact,
                pattern,
                palette,
                substitutions,
            )
        raise NeedsNewTile(pattern, palette, exact, substitutions)

    def _find_existing_tile(
        self,
        pattern: bytes,
        allowed_tiles: range,
        preferred_tile: int | None,
        *,
        tolerance: int | None = None,
    ) -> tuple[int, bool, bool] | None:
        self.last_match_was_fuzzy = False
        self.last_match_difference = 0
        effective_tolerance = (
            self.tile_match_tolerance if tolerance is None else max(0, tolerance)
        )
        candidates: list[tuple[int, bool, bool]] = []
        for tileset in (self.primary, self.secondary):
            for global_index, hflip, vflip in tileset._build_tile_matcher().get(
                pattern, ()
            ):
                if global_index in allowed_tiles:
                    candidates.append((global_index, hflip, vflip))
        if candidates:
            animated = self.animated_tile_indices()

            def score(candidate: tuple[int, bool, bool]) -> tuple[int, int, int, int]:
                global_index, hflip, vflip = candidate
                return (
                    int(hflip) + int(vflip),
                    0 if global_index in animated else 1,
                    0
                    if preferred_tile is not None and global_index == preferred_tile
                    else 1,
                    0 if global_index < self.project.settings.num_tiles_in_primary else 1,
                    global_index,
                )

            return min(candidates, key=score)

        if effective_tolerance > 0:
            near = self._find_near_tile(
                pattern,
                allowed_tiles,
                preferred_tile,
                effective_tolerance,
            )
            if near is not None:
                tile_index, hflip, vflip, difference = near
                self.last_match_was_fuzzy = True
                self.last_match_difference = difference
                return tile_index, hflip, vflip
        return None

    @staticmethod
    def _pattern_difference(a: bytes, b: bytes, limit: int) -> int:
        difference = 0
        for left, right in zip(a, b):
            if left != right:
                difference += 1
                if difference > limit:
                    break
        return difference

    def _find_near_tile(
        self,
        pattern: bytes,
        allowed_tiles: range,
        preferred_tile: int | None,
        tolerance: int,
        *,
        preferred_tiles: set[int] | None = None,
    ) -> tuple[int, bool, bool, int] | None:
        """Finds an existing tile within ``tolerance`` differing pixels."""
        if tolerance <= 0:
            return None
        animated = self.animated_tile_indices()

        def best_orientation(candidate: bytes) -> tuple[int, int, bool, bool] | None:
            best: tuple[int, int, bool, bool] | None = None
            for hflip in (False, True):
                for vflip in (False, True):
                    oriented = (
                        candidate
                        if not hflip and not vflip
                        else flip_pattern(candidate, hflip, vflip)
                    )
                    difference = self._pattern_difference(pattern, oriented, tolerance)
                    if difference > tolerance:
                        continue
                    score = (difference, int(hflip) + int(vflip))
                    if best is None or score < (best[0], best[1]):
                        best = (difference, int(hflip) + int(vflip), hflip, vflip)
            return best

        def tile_for(global_index: int) -> bytes | None:
            if global_index not in allowed_tiles:
                return None
            owner = self.tileset_for_tile(global_index)
            local = global_index - owner.global_tile_base
            if not 0 <= local < len(owner.patterns):
                return None
            return owner.patterns[local]

        # The tile that the edited subtile replaced is the most likely match.
        preferred_order: list[int] = []
        if preferred_tile is not None:
            preferred_order.append(preferred_tile)
        if preferred_tiles:
            preferred_order.extend(
                tile for tile in sorted(preferred_tiles) if tile != preferred_tile
            )
        for global_index in preferred_order:
            candidate = tile_for(global_index)
            if candidate is None:
                continue
            best = best_orientation(candidate)
            if best is not None:
                difference, _flips, hflip, vflip = best
                return global_index, hflip, vflip, difference

        best_candidate: tuple[
            tuple[int, int, int, int, int, int], int, bool, bool, int
        ] | None = None
        for tileset in (self.primary, self.secondary):
            for local, candidate in enumerate(tileset.patterns):
                global_index = tileset.global_tile_base + local
                if global_index not in allowed_tiles:
                    continue
                best = best_orientation(candidate)
                if best is None:
                    continue
                difference, flips, hflip, vflip = best
                score = (
                    difference,
                    flips,
                    0 if global_index not in animated else 1,
                    0 if preferred_tile == global_index else 1,
                    0 if global_index < self.project.settings.num_tiles_in_primary else 1,
                    global_index,
                )
                if best_candidate is None or score < best_candidate[0]:
                    best_candidate = (score, global_index, hflip, vflip, difference)
        if best_candidate is None:
            return None
        _score, global_index, hflip, vflip, difference = best_candidate
        return global_index, hflip, vflip, difference

    def allocate_tile_for_scope(self, pattern: bytes, scope: str) -> int:
        if scope == "primary":
            return self.primary.allocate_tile(pattern)
        try:
            return self.secondary.allocate_tile(pattern)
        except MapEditorError:
            # A secondary metatile may reference primary tiles, so falling back
            # to the primary tileset is legal if the secondary is full.
            return self.primary.allocate_tile(pattern)

    def build_raw_entries(
        self,
        metatile: list[TileRef],
        per_layer: list[list[TileRef] | None],
    ) -> list[TileRef]:
        """Assembles 8/12 entries from per-layer overrides."""
        result = list(metatile)
        if self.entries_per_metatile == 12:
            for layer, layer_entries in enumerate(per_layer):
                if layer_entries is not None:
                    result[layer * 4 : (layer + 1) * 4] = layer_entries
        else:
            if per_layer[0] is not None:
                result[0:4] = per_layer[0]
            if per_layer[2] is not None:
                result[4:8] = per_layer[2]
        return result


class NeedsNewTile(Exception):
    def __init__(
        self,
        pattern: bytes,
        palette: int,
        exact: bool,
        substitutions: list[tuple[tuple[int, int, int], tuple[int, int, int]]],
    ) -> None:
        super().__init__("new tile required")
        self.pattern = pattern
        self.palette = palette
        self.exact = exact
        self.substitutions = substitutions
