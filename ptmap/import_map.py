"""Import edited map layer PNGs back into map blocks and tilesets.

This is the reverse of :mod:`ptmap.export`. The import works at the 16x16
metatile level (the unit users edit in GIMP/Aseprite):

* untouched cells are ignored;
* a cell that matches an existing metatile is remapped automatically;
* a genuinely new cell asks whether to modify the original metatile globally,
  modify the underlying 8x8 tiles in place, or add a new metatile to the
  primary/secondary tileset;
* metatiles that become unreferenced can optionally be cleared from the
  tileset.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .animation_sheet import (
    animated_state_signature_index,
    apply_animation_sheet,
    sheet_cell_index,
)
from .animations import scan_animation_catalog
from .errors import MapEditorError
from .palette_planner import (
    apply_palette_plan,
    build_index_remap,
    plan_bank_colors,
)
from .palette_sheet import apply_palette_sheet
from .project import Project, Target
from .tileset import (
    METATILE_SIZE,
    TILE_SIZE,
    NeedsNewTile,
    TileRef,
    Tileset,
    flip_pattern,
    patch_from_rgba,
)
from .tileset_variants import (
    create_layout_variants,
    preview_layout_variants,
    sync_variants_from_base,
)

LAYER_NAMES = ("bottom", "middle", "top")


def normalize_patch(patch: bytes) -> bytes:
    """Normalizes alpha-0 RGB and partial alpha so GIMP re-encodes compare equal."""
    out = bytearray(len(patch))
    for i in range(0, len(patch), 4):
        if patch[i + 3] == 0:
            out[i : i + 4] = b"\0\0\0\0"
        else:
            out[i : i + 4] = bytes((patch[i], patch[i + 1], patch[i + 2], 255))
    return bytes(out)


def visualize_diff(a: bytes, b: bytes) -> int:
    a = normalize_patch(a)
    b = normalize_patch(b)
    return sum(1 for i in range(0, len(a), 4) if a[i : i + 4] != b[i : i + 4])


def composite_layers(signature: tuple[bytes, bytes, bytes]) -> bytes:
    """Composites bottom/middle/top into one 16x16 RGBA cell."""
    out = bytearray(METATILE_SIZE * METATILE_SIZE * 4)
    for layer in signature:
        normalized = normalize_patch(layer)
        for index in range(0, len(normalized), 4):
            if normalized[index + 3]:
                out[index : index + 4] = normalized[index : index + 4]
    return bytes(out)


@dataclass
class Change:
    index: int
    x: int
    y: int
    original_id: int
    original_layers: dict[int, bytes]
    edited_layers: dict[int, bytes]
    changed_layers: tuple[int, ...]
    signature: tuple[bytes, bytes, bytes]
    old_index: int | None = None
    is_new_cell: bool = False
    animated_originals: list[tuple[int, int, int]] = field(default_factory=list)
    animated_state_remap: bool = False
    remap_target_id: int | None = None
    changed_pixels: int = 0
    fuzzy_tiles: int = 0

    @property
    def label(self) -> str:
        return f"({self.x},{self.y})"


@dataclass
class Group:
    key: tuple
    original_id: int
    changes: list[Change]
    changed_layers: tuple[int, ...]
    signature: tuple[bytes, bytes, bytes]
    action: str = "pending"
    is_new_cell: bool = False


@dataclass
class ReplaceUnit:
    signature: tuple[bytes, bytes, bytes]
    attribute: bytes
    changes: list[Change]
    preferred_kind: str
    assigned_id: int | None = None
    target_kind: str | None = None
    duplicate: bool = False
    source_ids: set[int] = field(default_factory=set)
    resolved_entries: list[TileRef] | None = None


@dataclass
class Analysis:
    needs_new_tiles: list[tuple[bytes, int]] = field(default_factory=list)
    new_tile_count: int = 0
    tile_edit_possible: bool = False
    tile_edit_conflict: str | None = None
    affected_tiles: set[int] = field(default_factory=set)
    substitutions: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=list
    )
    inexact: bool = False

    @property
    def exact(self) -> bool:
        return not self.substitutions and not self.needs_new_tiles and not self.inexact


@dataclass
class ImportResult:
    changes: int = 0
    remapped: int = 0
    groups: int = 0
    modified_metatiles: int = 0
    replaced_metatiles: int = 0
    added_metatiles: int = 0
    added_tiles: int = 0
    skipped: int = 0
    removed_metatiles: int = 0
    cleared_tiles: int = 0
    cleared_unused_primary: int = 0
    cleared_unused_secondary: int = 0
    kept_unused_primary: int = 0
    kept_unused_secondary: int = 0
    left_empty_replace_slots: int = 0
    tiny_edits: int = 0
    ignored_palette_colors: int = 0
    reused_tiles: int = 0
    max_reused_difference: int = 0
    backup_dir: Path | None = None
    written: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    touched_tilesets: list[tuple[str, str]] = field(default_factory=list)
    resized_from: tuple[int, int] | None = None
    resized_to: tuple[int, int] | None = None
    resized_offset: tuple[int, int] = (0, 0)
    animation_changed: bool = False
    palette_changed: bool = False
    replace_units: int = 0
    replace_assigned: int = 0
    target_primary: int = 0
    target_secondary: int = 0
    palette_exact_primary: int = 0
    palette_exact_secondary: int = 0
    preferred_secondary_overflow: int = 0
    freed_primary: int = 0
    freed_secondary: int = 0
    freed_protected: int = 0
    primary_free_tiles: int = 0
    secondary_free_tiles: int = 0


class Prompter:
    def __init__(self, *, interactive: bool) -> None:
        self.interactive = interactive

    def choice(self, prompt: str, valid: set[str], default: str) -> str:
        if not self.interactive:
            return default
        while True:
            try:
                answer = input(prompt).strip().lower()
            except EOFError as exc:
                raise MapEditorError(
                    "Input ended while waiting for an answer."
                ) from exc
            if not answer:
                return default
            if answer in valid:
                return answer
            print(f"  Please choose one of: {', '.join(sorted(valid))}")

    def yes_no(self, prompt: str, default: bool = False) -> bool:
        if not self.interactive:
            return default
        while True:
            try:
                answer = input(prompt).strip().lower()
            except EOFError as exc:
                raise MapEditorError(
                    "Input ended while waiting for an answer."
                ) from exc
            if not answer:
                return default
            if answer in ("y", "yes", "s", "si", "sí"):
                return True
            if answer in ("n", "no"):
                return False
            print("  Please answer y or n.")

    def text(self, prompt: str, default: str) -> str:
        if not self.interactive:
            return default
        try:
            answer = input(prompt).strip()
        except EOFError as exc:
            raise MapEditorError("Input ended while waiting for an answer.") from exc
        return answer or default


class ImportMap:
    def __init__(
        self,
        project: Project,
        target: Target,
        input_dir: Path,
        *,
        on_new: str = "ask",
        on_remove: str = "ask",
        on_remove_tiles: str = "keep",
        quantize: str = "nearest",
        backup: bool = True,
        dry_run: bool = False,
        interactive: bool = True,
        anchor: str = "top-left",
        offset_x: int | None = None,
        offset_y: int | None = None,
        allow_shrink: bool = False,
        fill_metatile: int = 0,
        shift_events: bool = True,
        allow_animated_edits: bool = False,
        on_extra_row: str = "ask",
        force_palette_edits: bool = False,
        palette_mode: str = "smart",
        force_variant: str | None = None,
        reuse_unused_metatiles: bool = True,
        empty_metatiles_only: bool = False,
        palette_min_share: float = 0.005,
        tiny_edit_pixels: int = 4,
        tiny_edit_policy: str = "warn",
        tile_match_tolerance: int = 1,
    ) -> None:
        self.project = project
        self.target = target
        self.old_layout = target.layout
        self.layout = target.layout
        self.input_dir = Path(input_dir)
        self.tilesets = project.load_tileset_pair(self.layout)
        self.on_new = on_new
        self.on_remove = on_remove
        self.on_remove_tiles = on_remove_tiles
        self.quantize = quantize
        self.backup = backup
        self.dry_run = dry_run
        self.prompter = Prompter(interactive=interactive)
        self.anchor = anchor
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.allow_shrink = allow_shrink
        self.fill_metatile = fill_metatile
        self.shift_events = shift_events
        self.allow_animated_edits = allow_animated_edits
        self.on_extra_row = on_extra_row
        self.force_palette_edits = force_palette_edits
        self.palette_mode = palette_mode
        self.force_variant = force_variant
        self.reuse_unused_metatiles = reuse_unused_metatiles
        self.empty_metatiles_only = empty_metatiles_only
        self.palette_min_share = max(0.0, palette_min_share)
        self.tiny_edit_pixels = max(0, tiny_edit_pixels)
        self.tiny_edit_policy = tiny_edit_policy
        self.tile_match_tolerance = max(0, tile_match_tolerance)
        self.tilesets.tile_match_tolerance = self.tile_match_tolerance
        self._metatiles_cleared = False
        self._sheet_cell_index: dict[bytes, list[int]] = {}
        self._change_allowed_banks: dict[int, range] = {}
        self._replace_plan_units: list[ReplaceUnit] | None = None
        self._replace_plan_freed: set[int] | None = None
        self._replace_snapshots: dict[int, list[TileRef]] = {}
        self._replace_attribute_snapshots: dict[int, bytes] = {}
        self._replace_recycle_pools: dict[str, list[int]] = {
            "primary": [],
            "secondary": [],
        }
        self._replace_protected_tiles: set[int] = set()
        self._replace_claimed_tiles: set[int] = set()
        self._replace_tile_writes: dict[int, bytes] = {}
        self._replace_tile_palettes: dict[int, int] = {}
        self._replace_write_journal: list[tuple[Tileset, int, bytes]] | None = None
        self._replace_unit_entries: dict[int, list[TileRef]] = {}
        self._critical_ids_cache: set[int] | None = None
        self._preferred_secondary_overflow = 0
        self.original_tile_counts = {
            "primary": len(self.tilesets.primary.patterns),
            "secondary": len(self.tilesets.secondary.patterns),
        }
        self.old_blocks = project.read_blocks(self.old_layout)
        self.blocks = list(self.old_blocks)
        self.old_index_by_new_index: list[int | None] = list(
            range(len(self.old_blocks))
        )
        self.resized = False
        self.dx = 0
        self.dy = 0
        self.result = ImportResult()
        self._animated_signature_index: dict[tuple[bytes, bytes, bytes], list[int]] = {}

    # Loading the edited images -----------------------------------------

    def _prepare_canvas(self, images: dict[str, Image.Image]) -> None:
        old_w, old_h = self.old_layout.width, self.old_layout.height
        sizes = {name: image.size for name, image in images.items()}
        if len(set(sizes.values())) != 1:
            detail = ", ".join(
                f"{name}={size[0]}x{size[1]}" for name, size in sizes.items()
            )
            raise MapEditorError(f"Layer images have different sizes: {detail}")
        width_px, height_px = next(iter(sizes.values()))
        if width_px % METATILE_SIZE or height_px % METATILE_SIZE:
            raise MapEditorError(
                f"Layer images must be multiples of {METATILE_SIZE}px; got "
                f"{width_px}x{height_px}."
            )
        new_w = width_px // METATILE_SIZE
        new_h = height_px // METATILE_SIZE
        if new_w <= 0 or new_h <= 0:
            raise MapEditorError("Layer images must contain at least one 16x16 block.")

        if (new_w, new_h) == (old_w, old_h):
            return

        if (new_w < old_w or new_h < old_h) and not self.allow_shrink:
            raise MapEditorError(
                f"Layer images are smaller than layout {self.layout.id} "
                f"({new_w}x{new_h} vs {old_w}x{old_h} blocks). Pass --allow-shrink "
                "and --offset-x/--offset-y if you really want to crop the map."
            )

        if self.offset_x is not None or self.offset_y is not None:
            dx = self.offset_x or 0
            dy = self.offset_y or 0
        else:
            anchors = {
                "top-left": (0, 0),
                "top-right": (new_w - old_w, 0),
                "bottom-left": (0, new_h - old_h),
                "bottom-right": (new_w - old_w, new_h - old_h),
                "center": ((new_w - old_w) // 2, (new_h - old_h) // 2),
            }
            if self.anchor not in anchors:
                raise MapEditorError(
                    f"Unknown anchor '{self.anchor}' (expected {', '.join(anchors)})."
                )
            dx, dy = anchors[self.anchor]

        if dx < 0 or dy < 0:
            raise MapEditorError(
                "The old map would start outside the new canvas. Use non-negative "
                "--offset-x/--offset-y or a different --anchor."
            )
        if (dx + old_w > new_w or dy + old_h > new_h) and not self.allow_shrink:
            raise MapEditorError(
                "The old map does not fit inside the new canvas. For cropping use "
                "--allow-shrink and explicit --offset-x/--offset-y."
            )

        new_blocks: list[int] = []
        mapping: list[int | None] = []
        fill_id = self.fill_metatile & self.project.settings.metatile_id_mask
        dropped = 0
        for y in range(new_h):
            for x in range(new_w):
                ox, oy = x - dx, y - dy
                if 0 <= ox < old_w and 0 <= oy < old_h:
                    old_index = oy * old_w + ox
                    new_blocks.append(self.old_blocks[old_index])
                    mapping.append(old_index)
                else:
                    new_blocks.append(fill_id)
                    mapping.append(None)
        if self.allow_shrink:
            kept = {index for index in mapping if index is not None}
            dropped = len(self.old_blocks) - len(kept)

        self.blocks = new_blocks
        self.old_index_by_new_index = mapping
        self.layout = replace(self.old_layout, width=new_w, height=new_h)
        self.resized = True
        self.dx = dx
        self.dy = dy
        self.result.resized_from = (old_w, old_h)
        self.result.resized_to = (new_w, new_h)
        self.result.resized_offset = (dx, dy)
        shared_maps = self.project.maps_using_layout(self.old_layout.id)
        if len(shared_maps) > 1:
            self.result.warnings.append(
                f"Layout {self.old_layout.id} is shared by {len(shared_maps)} maps: "
                + ", ".join(entry.name for entry in shared_maps[:8])
                + ". Resizing affects all of them."
            )
        if dropped:
            self.result.warnings.append(
                f"Shrinking dropped {dropped} old block cell(s) outside the new canvas. "
                "Review object/warp/coord/bg events that may now be outside the map."
            )
        if dx or dy:
            self.result.warnings.append(
                f"Old map placed at offset ({dx},{dy}) in the new canvas. "
                + (
                    "Map event coordinates will be shifted."
                    if self.shift_events
                    else "Map events were NOT shifted; review them manually."
                )
            )
        if dx or dy:
            self.result.warnings.append(
                "Map connections may need their offsets reviewed after moving the layout origin."
            )

    def _load_layer_image(self, name: str) -> Image.Image:
        path = self.input_dir / f"{name}.png"
        if not path.is_file():
            raise MapEditorError(f"Missing edited layer image: {path}")
        try:
            with Image.open(path) as opened:
                opened.load()
                image = opened.copy()
        except OSError as exc:
            raise MapEditorError(f"Could not read {path}: {exc}") from exc
        if image.mode != "RGBA":
            if image.mode in ("RGB", "P", "L", "LA"):
                if image.mode != "LA":
                    self.result.warnings.append(
                        f"{path.name} has no alpha-only transparency data (mode {image.mode}); "
                        "converted to RGBA with alpha 255."
                    )
                image = image.convert("RGBA")
            else:
                raise MapEditorError(f"{path} has unsupported mode {image.mode}.")
        return image

    def load_changes(self) -> list[Change]:
        images = {name: self._load_layer_image(name) for name in LAYER_NAMES}
        self._prepare_canvas(images)
        width = self.layout.width * METATILE_SIZE
        height = self.layout.height * METATILE_SIZE
        for name, image in images.items():
            if image.size != (width, height):
                raise MapEditorError(
                    f"{name}.png is {image.width}x{image.height}, expected {width}x{height} "
                    f"for layout {self.layout.id} ({self.layout.width}x{self.layout.height} metatiles)."
                )
        data = {name: image.tobytes() for name, image in images.items()}

        partial_alpha = sum(
            1
            for raw in data.values()
            for i in range(3, len(raw), 4)
            if 0 < raw[i] < 255
        )
        if partial_alpha:
            self.result.warnings.append(
                f"Found {partial_alpha} semi-transparent pixels. GBA tiles only have "
                "transparent/opaque pixels; semi-transparent pixels are treated as opaque."
            )

        originals: dict[int, bytes] = {}
        for layer in range(3):
            originals[layer] = bytes(width * height * 4)

        changes: list[Change] = []
        for index, word in enumerate(self.blocks):
            x = index % self.layout.width
            y = index // self.layout.width
            original_id = word & self.project.settings.metatile_id_mask
            try:
                metatile = self.tilesets.metatile(original_id)
            except MapEditorError:
                self.result.warnings.append(
                    f"Block ({x},{y}) references out-of-range metatile 0x{original_id:03X}; skipped."
                )
                continue

            original_layers: dict[int, bytes] = {}
            edited_layers: dict[int, bytes] = {}
            changed_layers: list[int] = []
            for layer in range(3):
                original_patch = normalize_patch(
                    self.tilesets.render_layer_patch(metatile, layer)
                )
                edited_patch = normalize_patch(
                    patch_from_rgba(
                        data[LAYER_NAMES[layer]],
                        width,
                        x * METATILE_SIZE,
                        y * METATILE_SIZE,
                        METATILE_SIZE,
                    )
                )
                original_layers[layer] = original_patch
                edited_layers[layer] = edited_patch
                if edited_patch != original_patch:
                    changed_layers.append(layer)
            if not changed_layers:
                continue

            if self.tilesets.entries_per_metatile == 8 and 1 in changed_layers:
                raise MapEditorError(
                    f"Block ({x},{y}) edits the middle layer, but this project uses dual-layer "
                    "metatiles. Enable triple-layer metatiles or leave middle.png untouched."
                )

            signature = tuple(
                edited_layers[layer]
                if layer in changed_layers
                else original_layers[layer]
                for layer in range(3)
            )
            animated_originals: list[tuple[int, int, int]] = []
            animated_indices = self.tilesets.animated_tile_indices()
            if animated_indices:
                owner = self.tilesets.metatile_owner(original_id)
                for layer in changed_layers:
                    entries = owner.entries_for_layer(metatile, layer)
                    for subtile in range(4):
                        original_sub = normalize_patch(
                            self._subtile_patch(original_layers[layer], subtile)
                        )
                        edited_sub = normalize_patch(
                            self._subtile_patch(edited_layers[layer], subtile)
                        )
                        if original_sub == edited_sub:
                            continue
                        entry = entries[subtile]
                        if entry is not None and entry.tile_index in animated_indices:
                            animated_originals.append(
                                (layer, subtile, entry.tile_index)
                            )
            old_index = self.old_index_by_new_index[index]
            changed_pixels = sum(
                visualize_diff(original_layers[layer], edited_layers[layer])
                for layer in changed_layers
            )
            changes.append(
                Change(
                    index=index,
                    x=x,
                    y=y,
                    original_id=original_id,
                    original_layers=original_layers,
                    edited_layers=edited_layers,
                    changed_layers=tuple(changed_layers),
                    signature=signature,  # type: ignore[arg-type]
                    old_index=old_index,
                    is_new_cell=old_index is None,
                    animated_originals=animated_originals,
                    changed_pixels=changed_pixels,
                )
            )

        self.result.changes = len(changes)
        return changes

    # Analysis -----------------------------------------------------------

    def _original_entry(self, change: Change, layer: int, subtile: int) -> TileRef:
        metatile = self.tilesets.metatile(change.original_id)
        entries = self.tilesets.metatile_owner(change.original_id).entries_for_layer(
            metatile, layer
        )
        entry = entries[subtile]
        return entry if entry is not None else TileRef(0, 0, False, False)

    @staticmethod
    def _subtile_patch(patch: bytes, subtile: int) -> bytes:
        x = (subtile % 2) * TILE_SIZE
        y = (subtile // 2) * TILE_SIZE
        return patch_from_rgba(patch, METATILE_SIZE, x, y, TILE_SIZE)

    def _quantize_mode_for(self, change: Change) -> str:
        """Tiny edits are harmonized to the nearest colour and never fail."""
        if self.tiny_edit_pixels and change.changed_pixels <= self.tiny_edit_pixels:
            return "nearest"
        return self.quantize

    def _tile_tolerance_for(self, change: Change) -> int:
        """Tiny edits may reuse a nearby tile up to the tiny-edit threshold."""
        if self.tile_match_tolerance <= 0:
            return 0
        if self.tiny_edit_pixels and change.changed_pixels <= self.tiny_edit_pixels:
            return max(self.tile_match_tolerance, self.tiny_edit_pixels)
        return self.tile_match_tolerance

    def analyze(self, change: Change, scope: str) -> Analysis:
        analysis = Analysis()
        seen_new: set[tuple[bytes, int]] = set()
        writes: dict[int, bytes] = {}
        for layer in change.changed_layers:
            entries = self.tilesets.metatile_owner(
                change.original_id
            ).entries_for_layer(self.tilesets.metatile(change.original_id), layer)
            for subtile in range(4):
                original_sub = normalize_patch(
                    self._subtile_patch(change.original_layers[layer], subtile)
                )
                edited_sub = normalize_patch(
                    self._subtile_patch(change.edited_layers[layer], subtile)
                )
                if original_sub == edited_sub:
                    continue
                original_entry = entries[subtile] or TileRef(0, 0, False, False)
                analysis.affected_tiles.add(original_entry.tile_index)
                try:
                    target_tileset = self.tilesets.tileset_for_tile(
                        original_entry.tile_index
                    )
                    if (
                        not self.allow_animated_edits
                        and original_entry.tile_index
                        in target_tileset._protected_tile_indices()
                    ):
                        analysis.tile_edit_conflict = (
                            f"tile 0x{original_entry.tile_index:03X} is used by a "
                            "tileset animation (runtime frames would overwrite it)"
                        )
                    _, exact, _, _, substitutions = self.tilesets.resolve_tile(
                        self._subtile_patch(change.edited_layers[layer], subtile),
                        scope=scope,
                        preferred_palette=original_entry.palette,
                        preferred_tile=original_entry.tile_index,
                        quantize=self._quantize_mode_for(change),
                        tolerance=self._tile_tolerance_for(change),
                    )
                    if not exact or substitutions:
                        analysis.inexact = True
                        analysis.substitutions.extend(substitutions)
                except NeedsNewTile as need:
                    if not need.exact or need.substitutions:
                        analysis.inexact = True
                    key = (need.pattern, need.palette)
                    if key not in seen_new:
                        seen_new.add(key)
                        analysis.needs_new_tiles.append(key)
                    analysis.substitutions.extend(need.substitutions)

                # Can the "modify underlying tiles" action reproduce this patch
                # exactly with the original palette?
                try:
                    pattern, _, exact, _ = self.tilesets.secondary.quantize_patch(
                        self._subtile_patch(change.edited_layers[layer], subtile),
                        allowed_palettes=[original_entry.palette],
                        preferred_palette=original_entry.palette,
                        mode="error",
                        palette_bank=self.tilesets.palette_bank(),
                    )
                    if not exact:
                        analysis.tile_edit_conflict = (
                            "colours do not fit the original palette exactly"
                        )
                    stored = flip_pattern(
                        pattern, original_entry.hflip, original_entry.vflip
                    )
                    previous = writes.get(original_entry.tile_index)
                    if previous is not None and previous != stored:
                        analysis.tile_edit_conflict = f"tile 0x{original_entry.tile_index:03X} is edited differently in two subtiles"
                    writes[original_entry.tile_index] = stored
                except MapEditorError as exc:
                    analysis.tile_edit_conflict = str(exc)

        analysis.new_tile_count = len(seen_new)
        analysis.tile_edit_possible = analysis.tile_edit_conflict is None and bool(
            writes
        )
        return analysis

    def _affected_metatile_count(self, change: Change) -> int:
        analysis = self.analyze(change, "combined")
        if not analysis.affected_tiles:
            return 0
        owner = self.tilesets.metatile_owner(change.original_id)
        count = 0
        for metatile in owner.metatiles:
            if any(entry.tile_index in analysis.affected_tiles for entry in metatile):
                count += 1
        return count

    def _original_ids(self, groups: list[Group], remaps: list[Change]) -> set[int]:
        return {group.original_id for group in groups} | {
            change.original_id for change in remaps
        }

    def _targeted_reference_counts(
        self,
        metatile_ids: set[int],
        *,
        exclude_layout_id: str | None = None,
    ) -> tuple[dict[int, int], set[int], dict[int, set[str]]]:
        """Counts map/border usages only in layouts that can reference these IDs.

        ``exclude_layout_id`` ignores one layout, which is useful to measure
        whether a metatile is used outside the map currently being imported.
        """
        counts = {metatile_id: 0 for metatile_id in metatile_ids}
        incomplete: set[int] = set()
        incomplete_layouts: dict[int, set[str]] = {
            metatile_id: set() for metatile_id in metatile_ids
        }
        if not metatile_ids:
            return counts, incomplete, incomplete_layouts

        primary_name = self.tilesets.primary.name
        secondary_name = self.tilesets.secondary.name
        primary_ids = {
            metatile_id
            for metatile_id in metatile_ids
            if metatile_id < self.project.settings.num_metatiles_in_primary
        }
        secondary_ids = metatile_ids - primary_ids

        for layout in self.project.layouts.values():
            if exclude_layout_id is not None and layout.id == exclude_layout_id:
                continue
            can_primary = layout.primary_tileset == primary_name
            can_secondary = layout.secondary_tileset == secondary_name
            if not can_primary and not can_secondary:
                continue
            relevant_ids = set()
            if can_primary:
                relevant_ids.update(primary_ids)
            if can_secondary:
                relevant_ids.update(secondary_ids)
            try:
                blocks = self.project.read_blocks(layout)
            except MapEditorError:
                for metatile_id in relevant_ids:
                    incomplete.add(metatile_id)
                    incomplete_layouts[metatile_id].add(layout.id)
                continue
            border: tuple[int, ...] = ()
            try:
                border = self.project.read_border(layout)
            except MapEditorError:
                for metatile_id in relevant_ids:
                    incomplete.add(metatile_id)
                    incomplete_layouts[metatile_id].add(layout.id)
            for word in blocks:
                metatile_id = word & self.project.settings.metatile_id_mask
                if metatile_id in relevant_ids:
                    counts[metatile_id] += 1
            for word in border:
                metatile_id = word & self.project.settings.metatile_id_mask
                if metatile_id in relevant_ids:
                    counts[metatile_id] += 1
        return counts, incomplete, incomplete_layouts

    # Tile resolution ----------------------------------------------------

    class _Allocator:
        def __init__(self, importer: ImportMap, scope: str) -> None:
            self.importer = importer
            self.scope = scope
            self.allocated: dict[tuple[bytes, int], int] = {}

        def get(
            self, need: NeedsNewTile, preferred_tile: int | None, tolerance: int
        ) -> int:
            key = (need.pattern, need.palette)
            if key in self.allocated:
                return self.allocated[key]
            pair = self.importer.tilesets
            allowed = range(self.importer.project.settings.num_tiles_in_primary)
            if self.scope != "primary":
                allowed = range(self.importer.project.settings.num_tiles_total)
            found = pair._find_existing_tile(
                need.pattern, allowed, preferred_tile, tolerance=tolerance
            )
            if found is not None:
                tile_index = found[0]
                if pair.last_match_was_fuzzy:
                    self.importer.result.reused_tiles += 1
                    self.importer.result.max_reused_difference = max(
                        self.importer.result.max_reused_difference,
                        pair.last_match_difference,
                    )
            else:
                tile_index = pair.allocate_tile_for_scope(need.pattern, self.scope)
                self.importer.result.added_tiles += 1
            self.allocated[key] = tile_index
            return tile_index

    def resolve_entries(
        self,
        change: Change,
        scope: str,
        allocator: _Allocator,
        original_metatile: list[TileRef] | None = None,
    ) -> tuple[list[TileRef], int]:
        """Builds the 8/12 desired entries for a change under ``scope``."""
        if original_metatile is None:
            original_metatile = self.tilesets.metatile(change.original_id)
        per_layer: list[list[TileRef] | None] = [None, None, None]
        substitutions = 0

        for layer in range(3):
            if layer not in change.changed_layers:
                continue
            original_entries = self.tilesets.metatile_owner(
                change.original_id
            ).entries_for_layer(original_metatile, layer)
            layer_entries: list[TileRef] = []
            for subtile in range(4):
                original_sub = normalize_patch(
                    self._subtile_patch(change.original_layers[layer], subtile)
                )
                edited_sub = normalize_patch(
                    self._subtile_patch(change.edited_layers[layer], subtile)
                )
                original_entry = original_entries[subtile] or TileRef(
                    0, 0, False, False
                )
                if original_sub == edited_sub:
                    layer_entries.append(original_entry)
                    continue
                try:
                    entry, exact, _, _, _ = self.tilesets.resolve_tile(
                        self._subtile_patch(change.edited_layers[layer], subtile),
                        scope=scope,
                        preferred_palette=original_entry.palette,
                        preferred_tile=original_entry.tile_index,
                        quantize=self._quantize_mode_for(change),
                        tolerance=self._tile_tolerance_for(change),
                    )
                    if not exact:
                        substitutions += 1
                    if self.tilesets.last_match_was_fuzzy:
                        self.result.reused_tiles += 1
                        self.result.max_reused_difference = max(
                            self.result.max_reused_difference,
                            self.tilesets.last_match_difference,
                        )
                        change.fuzzy_tiles += 1
                    layer_entries.append(entry)
                except NeedsNewTile as need:
                    if not need.exact:
                        substitutions += 1
                    tile_index = allocator.get(
                        need,
                        original_entry.tile_index,
                        self._tile_tolerance_for(change),
                    )
                    if self.tilesets.last_match_was_fuzzy:
                        self.result.reused_tiles += 1
                        self.result.max_reused_difference = max(
                            self.result.max_reused_difference,
                            self.tilesets.last_match_difference,
                        )
                        change.fuzzy_tiles += 1
                    layer_entries.append(
                        TileRef(tile_index, need.palette, False, False)
                    )
            per_layer[layer] = layer_entries

        entries = self.tilesets.build_raw_entries(original_metatile, per_layer)
        return entries, substitutions

    # Grouping and decisions --------------------------------------------

    def build_groups(self, changes: list[Change]) -> tuple[list[Group], list[Change]]:
        groups: dict[tuple, Group] = {}
        remaps: list[Change] = []
        for change in changes:
            sheet_targets = self._sheet_cell_index.get(
                composite_layers(change.signature)
            )
            if sheet_targets:
                existing = sheet_targets[0]
                change.animated_state_remap = True
                if existing == change.original_id:
                    change.remap_target_id = existing
                    remaps.append(change)
                    continue
            else:
                existing = self.tilesets.find_matching_metatile(
                    change.signature, global_search=True
                )
                if existing is None:
                    animated_candidates = self._animated_signature_index.get(
                        change.signature, []
                    )
                    if animated_candidates:
                        existing = animated_candidates[0]
                        change.animated_state_remap = True
            if existing is not None and existing != change.original_id:
                change.remap_target_id = existing
                remaps.append(change)
                continue
            key = (
                change.original_id,
                change.changed_layers,
                change.signature,
                change.is_new_cell,
            )
            group = groups.get(key)
            if group is None:
                group = Group(
                    key=key,
                    original_id=change.original_id,
                    changes=[],
                    changed_layers=change.changed_layers,
                    signature=change.signature,
                    is_new_cell=change.is_new_cell,
                )
                groups[key] = group
            group.changes.append(change)
        self.result.groups = len(groups)
        self.result.remapped = len(remaps)
        return list(groups.values()), remaps

    def _format_new_tile_info(self, analysis: Analysis) -> str:
        if not analysis.new_tile_count and not analysis.substitutions:
            return "all required 8x8 tiles already exist"
        parts = []
        if analysis.new_tile_count:
            parts.append(f"{analysis.new_tile_count} new 8x8 tile(s)")
        if analysis.substitutions:
            parts.append(f"{len(analysis.substitutions)} colour substitution(s)")
        return ", ".join(parts)

    def _primary_scope_possible(self, change: Change) -> bool:
        metatile = self.tilesets.metatile(change.original_id)
        return all(
            entry.tile_index < self.project.settings.num_tiles_in_primary
            for entry in metatile
        )

    def _primary_available(self, change: Change, analysis_primary: Analysis) -> bool:
        return (
            self._primary_scope_possible(change)
            and self.tilesets.primary.has_free_metatile_slot()
            and (
                analysis_primary.new_tile_count == 0
                or self.tilesets.primary.has_free_tile_slot()
            )
        )

    def _secondary_available(self, analysis_secondary: Analysis) -> bool:
        return self.tilesets.secondary.has_free_metatile_slot() and (
            analysis_secondary.new_tile_count == 0
            or self.tilesets.secondary.has_free_tile_slot()
            or self.tilesets.primary.has_free_tile_slot()
        )

    def decide_group(self, group: Group) -> None:
        is_new_cell = group.is_new_cell
        analysis_secondary = self.analyze(group.changes[0], "combined")
        layer_names = ", ".join(LAYER_NAMES[layer] for layer in group.changed_layers)
        secondary_name = self.layout.secondary_tileset
        primary_name = self.layout.primary_tileset

        if self.on_new != "ask":
            action = self.on_new
            group.action = action
            if is_new_cell and action in ("modify-metatile", "modify-tiles"):
                raise MapEditorError(
                    f"--on-new={action} cannot be used at {group.changes[0].label}: "
                    "that block is new map area with no original metatile to modify. "
                    "Use add-secondary, add-primary, replace, or skip."
                )
            if action == "modify-tiles" and not analysis_secondary.tile_edit_possible:
                raise MapEditorError(
                    f"--on-new=modify-tiles cannot be used for the edit at {group.changes[0].label}: "
                    f"{analysis_secondary.tile_edit_conflict}"
                )
            if action == "add-primary":
                primary_possible = self._primary_scope_possible(group.changes[0])
                if not primary_possible:
                    raise MapEditorError(
                        f"--on-new=add-primary cannot be used for the edit at "
                        f"{group.changes[0].label}: the original metatile references one or more "
                        "secondary-tileset tiles, which a primary metatile may not use. "
                        "Use add-secondary instead."
                    )
                analysis_primary = self.analyze(group.changes[0], "primary")
                if not self._primary_available(group.changes[0], analysis_primary):
                    raise MapEditorError(
                        f"--on-new=add-primary cannot be used for the edit at "
                        f"{group.changes[0].label}: the primary tileset has no free metatile or "
                        "tile slots. Use add-secondary instead."
                    )
            if action == "add-secondary" and not self._secondary_available(
                analysis_secondary
            ):
                raise MapEditorError(
                    f"--on-new=add-secondary cannot be used for the edit at "
                    f"{group.changes[0].label}: the secondary tileset has no free metatile or "
                    "tile slots."
                )
            return

        if not self.prompter.interactive:
            raise MapEditorError(
                "Edits need a decision, but stdin is not interactive. "
                "Pass --on-new modify-metatile|modify-tiles|add-secondary|add-primary|replace|skip."
            )

        primary_possible = self._primary_scope_possible(group.changes[0])
        analysis_primary = self.analyze(group.changes[0], "primary")
        primary_available = (
            self._primary_available(group.changes[0], analysis_primary)
            if primary_possible
            else False
        )
        secondary_available = self._secondary_available(analysis_secondary)
        counts, _, _ = self._targeted_reference_counts({group.original_id})
        refs = counts.get(group.original_id, 0)
        affected_metatiles = self._affected_metatile_count(group.changes[0])

        print()
        print(
            f"Edit at {group.changes[0].label} ({len(group.changes)} block(s), layer: {layer_names})"
        )
        if is_new_cell:
            print("  New map area: this cell was added by enlarging the canvas")
        else:
            print(
                f"  Original metatile: 0x{group.original_id:03X} ({refs} block reference(s) in all maps)"
            )
        print(f"  New content: {self._format_new_tile_info(analysis_secondary)}")
        print("  Options:")
        if not is_new_cell:
            print(
                "    [1] Modify the original metatile in place (all maps using it change)"
            )
            print(
                f"    [2] Modify the underlying 8x8 tiles in place (global tile edit; {affected_metatiles} metatile(s) use them)"
            )
        print(
            f"    [3] Add a new metatile to {secondary_name} (local edit; recommended)"
        )
        print(f"    [4] Add a new metatile to {primary_name}")
        print(
            "    [5] Replace existing metatile slots (full overhaul; other maps may change)"
        )
        print("    [s] Skip this edit")
        if not is_new_cell and not analysis_secondary.tile_edit_possible:
            print(
                f"        (option 2 unavailable: {analysis_secondary.tile_edit_conflict})"
            )
        if is_new_cell:
            print("        (options 1-2 unavailable: there is no original metatile)")
        if not primary_possible:
            print(
                "        (option 4 unavailable: the original metatile uses secondary tiles)"
            )
        elif not primary_available:
            print(
                "        (option 4 unavailable: the primary tileset has no free metatile/tile slots)"
            )
        if not secondary_available:
            print(
                "        (option 3 unavailable: the secondary tileset has no free metatile/tile slots)"
            )
        valid = {
            "1",
            "2",
            "3",
            "4",
            "5",
            "s",
            "modify-metatile",
            "modify-tiles",
            "add-secondary",
            "add-primary",
            "replace",
            "skip",
        }
        choice = self.prompter.choice("  Choice [3]: ", valid, "3")
        if choice in ("1", "modify-metatile"):
            if is_new_cell:
                raise MapEditorError(
                    f"Option 1 is not available at {group.changes[0].label}: no original metatile."
                )
            group.action = "modify-metatile"
        elif choice in ("2", "modify-tiles"):
            if is_new_cell:
                raise MapEditorError(
                    f"Option 2 is not available at {group.changes[0].label}: no original tiles."
                )
            if not analysis_secondary.tile_edit_possible:
                raise MapEditorError(
                    f"Option 2 is not available for the edit at {group.changes[0].label}: "
                    f"{analysis_secondary.tile_edit_conflict}"
                )
            group.action = "modify-tiles"
        elif choice in ("3", "add-secondary"):
            if not secondary_available:
                raise MapEditorError(
                    f"Option 3 is not available for the edit at {group.changes[0].label}: "
                    "the secondary tileset has no free metatile/tile slots."
                )
            group.action = "add-secondary"
        elif choice in ("4", "add-primary"):
            if not primary_possible:
                raise MapEditorError(
                    f"Option 4 is not available for the edit at {group.changes[0].label}: "
                    "the original metatile uses secondary-tileset tiles."
                )
            if not primary_available:
                raise MapEditorError(
                    f"Option 4 is not available for the edit at {group.changes[0].label}: "
                    "the primary tileset has no free metatile/tile slots."
                )
            group.action = "add-primary"
        elif choice in ("5", "replace"):
            group.action = "replace"
        else:
            group.action = "skip"

    # Applying -----------------------------------------------------------

    @staticmethod
    def _set_metatile_id(project: Project, word: int, metatile_id: int) -> int:
        return (word & ~project.settings.metatile_id_mask) | (
            metatile_id & project.settings.metatile_id_mask
        )

    def _find_metatile_with_entries(self, entries: list[TileRef]) -> int | None:
        wanted = [entry.to_word() for entry in entries]
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            for local, metatile in enumerate(tileset.metatiles):
                if [entry.to_word() for entry in metatile] == wanted:
                    return tileset.global_metatile_base + local
        return None

    def _critical_metatile_ids(self) -> set[int]:
        """Metatiles that must never be repurposed (doors and animated art)."""
        if self._critical_ids_cache is not None:
            return self._critical_ids_cache
        catalog = scan_animation_catalog(self.project)
        pair_names = (self.tilesets.primary.name, self.tilesets.secondary.name)
        critical = {
            door.metatile_id for door in catalog.door_defs if door.tileset in pair_names
        }
        behavior_values = self.project.metatile_behavior_values()
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
        if door_behaviors:
            for tileset in (self.tilesets.primary, self.tilesets.secondary):
                for local, attribute in enumerate(tileset.attributes):
                    if int.from_bytes(attribute, "little") & 0xFF in door_behaviors:
                        critical.add(tileset.global_metatile_base + local)
        self._critical_ids_cache = critical
        return critical

    def _replace_freed_ids(self, groups: list[Group], remaps: list[Change]) -> set[int]:
        """Metatile IDs that will have zero references in this map after the edits."""
        action_by_index: dict[int, str] = {}
        for group in groups:
            for change in group.changes:
                action_by_index[change.index] = group.action
        remap_target_by_index: dict[int, int] = {}
        for change in remaps:
            action_by_index[change.index] = "remap"
            if change.remap_target_id is not None:
                remap_target_by_index[change.index] = change.remap_target_id

        kept: dict[int, int] = {}
        old_ids: set[int] = set()
        for new_index, word in enumerate(self.blocks):
            old_index = self.old_index_by_new_index[new_index]
            if old_index is None:
                continue
            metatile_id = word & self.project.settings.metatile_id_mask
            old_ids.add(metatile_id)
            action = action_by_index.get(new_index)
            if action == "remap":
                target = remap_target_by_index.get(new_index)
                if target is not None:
                    kept[target] = kept.get(target, 0) + 1
                continue
            if action is None or action in ("skip", "modify-tiles", "modify-metatile"):
                kept[metatile_id] = kept.get(metatile_id, 0) + 1
        try:
            for word in self.project.read_border(self.layout):
                metatile_id = word & self.project.settings.metatile_id_mask
                old_ids.add(metatile_id)
                kept[metatile_id] = kept.get(metatile_id, 0) + 1
        except MapEditorError:
            pass
        return {metatile_id for metatile_id in old_ids if kept.get(metatile_id, 0) == 0}

    def _build_replace_units(self, groups: list[Group]) -> list[ReplaceUnit]:
        units: dict[tuple, ReplaceUnit] = {}
        for group in groups:
            if group.action != "replace":
                continue
            owner = self.tilesets.metatile_owner(group.original_id)
            attribute = self.tilesets.attribute(group.original_id)
            key = (group.signature, attribute, owner.kind)
            unit = units.get(key)
            if unit is None:
                unit = ReplaceUnit(
                    signature=group.signature,
                    attribute=attribute,
                    changes=[],
                    preferred_kind=owner.kind,
                )
                units[key] = unit
            unit.changes.extend(group.changes)
            unit.source_ids.add(group.original_id)
        return sorted(
            units.values(),
            key=lambda unit: (-len(unit.changes), min(c.index for c in unit.changes)),
        )

    def _old_tile_set(self, metatile_id: int) -> set[int]:
        metatile = self._replace_snapshots.get(metatile_id)
        if metatile is None:
            metatile = self.tilesets.metatile(metatile_id)
        return {entry.tile_index for entry in metatile if entry is not None}

    def _best_duplicate_for_slot(
        self, metatile_id: int, pools: dict[str, list[ReplaceUnit]]
    ) -> ReplaceUnit:
        kind = self.tilesets.metatile_owner(metatile_id).kind
        old_tiles = self._old_tile_set(metatile_id)
        best: ReplaceUnit | None = None
        best_score = -1.0
        for unit in pools[kind]:
            if unit.assigned_id is None:
                continue
            source_tiles: set[int] = set()
            for source_id in unit.source_ids:
                source_tiles.update(self._old_tile_set(source_id))
            if not source_tiles:
                continue
            score = len(old_tiles & source_tiles) / max(
                1, len(old_tiles | source_tiles)
            )
            if score > best_score:
                best_score = score
                best = unit
        if best is None:
            candidates = [
                unit
                for unit in pools[kind]
                if unit.assigned_id is not None and unit.target_kind == kind
            ]
            if not candidates:
                candidates = [
                    unit for unit in pools[kind] if unit.assigned_id is not None
                ]
            if not candidates:
                raise MapEditorError(
                    f"Cannot build replacement content for freed metatile slot "
                    f"0x{metatile_id:03X}: no replacement unit is available."
                )
            best = min(candidates, key=lambda unit: unit.assigned_id or 0)
        return ReplaceUnit(
            signature=best.signature,
            attribute=best.attribute,
            changes=[],
            preferred_kind=best.preferred_kind,
            assigned_id=metatile_id,
            target_kind=kind,
            duplicate=True,
            source_ids=set(best.source_ids),
            resolved_entries=(
                list(best.resolved_entries)
                if best.resolved_entries is not None
                else None
            ),
        )

    def _assign_replace_slots(
        self, units: list[ReplaceUnit], freed_ids: set[int]
    ) -> None:
        self.result.replace_units = len(units)
        self.result.primary_free_tiles = self.tilesets.primary.free_tile_slots()
        self.result.secondary_free_tiles = self.tilesets.secondary.free_tile_slots()
        critical = self._critical_metatile_ids()
        free_slots: dict[str, list[int]] = {"primary": [], "secondary": []}
        reserved_units: dict[str, list[ReplaceUnit]] = {
            "primary": [],
            "secondary": [],
        }
        for metatile_id in freed_ids:
            owner = self.tilesets.metatile_owner(metatile_id)
            if metatile_id in critical:
                self.result.freed_protected += 1
                self.result.warnings.append(
                    f"Metatile 0x{metatile_id:03X} is free in this map but is a door or "
                    f"animated metatile in {owner.name}; replace mode keeps it intact."
                )
                continue
            free_slots[owner.kind].append(metatile_id)
        self.result.freed_primary = len(free_slots["primary"])
        self.result.freed_secondary = len(free_slots["secondary"])

        old_usage: Counter[int] = Counter()
        for word in self.old_blocks:
            old_usage[word & self.project.settings.metatile_id_mask] += 1
        for slots in free_slots.values():
            slots.sort(key=lambda mid: (-old_usage.get(mid, 0), mid))

        unit_old_counts: dict[int, Counter[int]] = {
            id(unit): Counter(
                change.original_id for change in unit.changes if not change.is_new_cell
            )
            for unit in units
        }
        assigned_units: set[int] = set()

        # First pass: a freed old metatile is replaced by the new content that
        # came from its own cells, choosing the most-used such content.
        for kind in ("primary", "secondary"):
            for metatile_id in list(free_slots[kind]):
                candidates = [
                    unit
                    for unit in units
                    if id(unit) not in assigned_units
                    and unit_old_counts[id(unit)].get(metatile_id, 0) > 0
                ]
                if not candidates:
                    continue
                candidate = max(
                    candidates,
                    key=lambda unit: (
                        unit_old_counts[id(unit)][metatile_id],
                        len(unit.changes),
                        -min(change.index for change in unit.changes),
                    ),
                )
                candidate.assigned_id = metatile_id
                candidate.target_kind = kind
                assigned_units.add(id(candidate))
                reserved_units[kind].append(candidate)
                free_slots[kind].remove(metatile_id)

        # Second pass: fill the remaining freed slots. Prefer content from the
        # same tileset, then any not-yet-used replacement content.
        for kind in ("primary", "secondary"):
            for metatile_id in list(free_slots[kind]):
                pool = [
                    unit
                    for unit in units
                    if id(unit) not in assigned_units
                    and (kind != "primary" or unit.preferred_kind == "primary")
                ]
                if not pool and kind == "secondary":
                    pool = [unit for unit in units if id(unit) not in assigned_units]
                if not pool:
                    continue
                candidate = max(
                    pool,
                    key=lambda unit: (
                        unit_old_counts[id(unit)].get(metatile_id, 0),
                        len(unit.changes),
                        -min(change.index for change in unit.changes),
                    ),
                )
                candidate.assigned_id = metatile_id
                candidate.target_kind = kind
                assigned_units.add(id(candidate))
                reserved_units[kind].append(candidate)
                free_slots[kind].remove(metatile_id)

        # Secondary leftovers are only duplicated when unused-slot reuse is
        # disabled. With the default mode they stay empty for future content;
        # _apply_replace_groups clears them and reports the count.
        # Primary leftovers are handled in _apply_replace_groups.
        if not self.reuse_unused_metatiles:
            for metatile_id in list(free_slots["secondary"]):
                duplicate = self._best_duplicate_for_slot(metatile_id, reserved_units)
                units.append(duplicate)
                reserved_units["secondary"].append(duplicate)
                free_slots["secondary"].remove(metatile_id)

        self.result.replace_assigned = sum(
            1 for unit in units if unit.assigned_id is not None
        )
        self._replace_plan_units = units

    def _old_layout_ids(self) -> set[int]:
        old_ids = {
            word & self.project.settings.metatile_id_mask for word in self.old_blocks
        }
        try:
            old_ids.update(
                word & self.project.settings.metatile_id_mask
                for word in self.project.read_border(self.old_layout)
            )
        except MapEditorError:
            pass
        return old_ids

    def _build_replace_recycle_pools(
        self, freed_ids: set[int], critical: set[int], kept_ids: set[int]
    ) -> dict[str, list[int]]:
        """Tile slots owned by replacement metatiles, ready to be overwritten."""
        protected_tiles: set[int] = set()
        for metatile_id in kept_ids:
            try:
                metatile = self.tilesets.metatile(metatile_id)
            except MapEditorError:
                continue
            protected_tiles.update(
                entry.tile_index for entry in metatile if entry is not None
            )
        self._replace_protected_tiles = protected_tiles

        references: Counter[int] = Counter()
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            for metatile in tileset.metatiles:
                for entry in metatile:
                    references[entry.tile_index] += 1

        freed_tiles: dict[str, set[int]] = {"primary": set(), "secondary": set()}
        for metatile_id in freed_ids - critical:
            try:
                metatile = self.tilesets.metatile(metatile_id)
            except MapEditorError:
                continue
            owner = self.tilesets.metatile_owner(metatile_id)
            for entry in metatile:
                if entry is None:
                    continue
                tile_owner = self.tilesets.tileset_for_tile(entry.tile_index)
                if tile_owner != self.tilesets.primary and owner.kind == "primary":
                    continue
                freed_tiles[tile_owner.kind].add(entry.tile_index)

        pools: dict[str, list[int]] = {"primary": [], "secondary": []}
        for kind, tileset in (
            ("primary", self.tilesets.primary),
            ("secondary", self.tilesets.secondary),
        ):
            animated = tileset._protected_tile_indices()
            candidates = [
                tile
                for tile in freed_tiles[kind]
                if tile != 0
                and tile not in animated
                and tile not in protected_tiles
                and tileset.global_tile_base
                <= tile
                < tileset.global_tile_base + len(tileset.patterns)
            ]
            candidates.sort(key=lambda tile: (references.get(tile, 0), tile))
            pools[kind] = candidates
        return pools

    def _unit_source_tiles(self, unit: ReplaceUnit) -> set[int]:
        tiles: set[int] = set()
        for metatile_id in unit.source_ids:
            snapshot = self._replace_snapshots.get(metatile_id)
            if snapshot is None:
                continue
            tiles.update(entry.tile_index for entry in snapshot if entry is not None)
        return tiles

    def _unit_preferred_tiles(self, unit: ReplaceUnit) -> set[int]:
        """Tiles from the old metatile that the assigned slot actually replaces.

        This keeps a replacement floor on the floor's own tile slots instead of
        grabbing unrelated spare tiles, so other metatiles that share that floor
        follow the new art automatically.
        """
        source_ids: set[int] = set()
        if unit.assigned_id is not None:
            source_ids = {
                change.original_id
                for change in unit.changes
                if change.original_id == unit.assigned_id
            }
        if not source_ids:
            source_ids = set(unit.source_ids)
        tiles: set[int] = set()
        for metatile_id in source_ids:
            snapshot = self._replace_snapshots.get(metatile_id)
            if snapshot is None:
                continue
            tiles.update(entry.tile_index for entry in snapshot if entry is not None)
        return tiles

    def _entry_patch(self, entry: TileRef) -> bytes:
        owner = self.tilesets.tileset_for_tile(entry.tile_index)
        local = entry.tile_index - owner.global_tile_base
        if not 0 <= local < len(owner.patterns):
            return bytes(METATILE_SIZE * METATILE_SIZE * 4)
        pattern = flip_pattern(owner.patterns[local], entry.hflip, entry.vflip)
        palette = self.tilesets.palette_for(entry.palette)
        out = bytearray(TILE_SIZE * TILE_SIZE * 4)
        for index, value in enumerate(pattern):
            if value == 0:
                continue
            r, g, b = palette[value] if value < len(palette) else (0, 0, 0)
            out[index * 4 : index * 4 + 4] = bytes((r, g, b, 255))
        return bytes(out)

    def _entry_matches_patch(self, entry: TileRef, patch: bytes) -> bool:
        return self._entry_patch(entry) == patch

    def _entry_alpha_matches(self, entry: TileRef, patch: bytes) -> bool:
        rendered = self._entry_patch(entry)
        return all(
            bool(rendered[index + 3]) == bool(patch[index + 3])
            for index in range(0, len(patch), 4)
        )

    def _preferred_palette(
        self, unit: ReplaceUnit, layer: int, subtile: int
    ) -> int | None:
        for change in unit.changes:
            if change.is_new_cell:
                continue
            snapshot = self._replace_snapshots.get(change.original_id)
            if snapshot is None:
                continue
            owner = self.tilesets.metatile_owner(change.original_id)
            entries = owner.entries_for_layer(snapshot, layer)
            if subtile < len(entries) and entries[subtile] is not None:
                return entries[subtile].palette
        return None

    def _reuse_entry_for_patch(
        self, unit: ReplaceUnit, layer: int, subtile: int, patch: bytes
    ) -> TileRef | None:
        if not any(patch[index + 3] for index in range(0, len(patch), 4)):
            return TileRef(0, 0, False, False)
        for change in unit.changes:
            if change.is_new_cell:
                continue
            snapshot = self._replace_snapshots.get(change.original_id)
            if snapshot is None:
                continue
            owner = self.tilesets.metatile_owner(change.original_id)
            entries = owner.entries_for_layer(snapshot, layer)
            if subtile >= len(entries) or entries[subtile] is None:
                continue
            entry = entries[subtile]
            if self._entry_matches_patch(entry, patch):
                return entry
        return None

    class _ReplaceAllocator:
        def __init__(self, importer: ImportMap, unit: ReplaceUnit, scope: str) -> None:
            self.importer = importer
            self.unit = unit
            self.scope = scope
            if scope == "primary":
                self.allowed_tiles = range(
                    importer.project.settings.num_tiles_in_primary
                )
                self.allowed_palettes = range(
                    importer.project.settings.num_pals_in_primary
                )
            else:
                self.allowed_tiles = range(importer.project.settings.num_tiles_total)
                self.allowed_palettes = range(importer.project.settings.num_pals_total)
            self.preferred_tiles = importer._unit_preferred_tiles(unit)
            self.used_tiles: set[int] = set()
            self.tile_tolerance = importer.tile_match_tolerance
            if self.tile_tolerance > 0 and (
                importer.tiny_edit_pixels
                and unit.changes
                and all(
                    change.changed_pixels <= importer.tiny_edit_pixels
                    for change in unit.changes
                )
            ):
                self.tile_tolerance = max(
                    self.tile_tolerance, importer.tiny_edit_pixels
                )

        def get(self, pattern: bytes, palette: int, patch: bytes) -> TileRef:
            importer = self.importer
            if not any(pattern):
                if any(patch[index + 3] for index in range(0, len(patch), 4)):
                    raise MapEditorError(
                        f"Refusing to write transparent content for an opaque edit "
                        f"in replace mode ({self.unit.changes[0].label if self.unit.changes else 'duplicate slot'})."
                    )
                return TileRef(0, 0, False, False)
            found = importer._find_replace_tile(
                pattern,
                self.scope,
                self.preferred_tiles,
                self.used_tiles,
                self.tile_tolerance,
            )
            if found is not None:
                tile_index, hflip, vflip = found
                self.used_tiles.add(tile_index)
                if importer.tilesets.last_match_was_fuzzy:
                    importer.result.reused_tiles += 1
                    importer.result.max_reused_difference = max(
                        importer.result.max_reused_difference,
                        importer.tilesets.last_match_difference,
                    )
                return TileRef(tile_index, palette, hflip, vflip)
            tile_index = importer._allocate_replace_tile(
                pattern, self.scope, self.preferred_tiles, palette
            )
            self.used_tiles.add(tile_index)
            return TileRef(tile_index, palette, False, False)

    def _find_replace_tile(
        self,
        pattern: bytes,
        scope: str,
        preferred_tiles: set[int],
        used_tiles: set[int],
        tolerance: int,
    ) -> tuple[int, bool, bool] | None:
        allowed = (
            range(self.project.settings.num_tiles_in_primary)
            if scope == "primary"
            else range(self.project.settings.num_tiles_total)
        )
        animated = self.tilesets.animated_tile_indices()
        self.tilesets.last_match_was_fuzzy = False
        self.tilesets.last_match_difference = 0
        candidates: list[tuple[int, bool, bool]] = []
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            for tile_index, hflip, vflip in tileset._build_tile_matcher().get(
                pattern, ()
            ):
                if tile_index not in allowed:
                    continue
                candidates.append((tile_index, hflip, vflip))
        if not candidates:
            if tolerance > 0:
                near = self.tilesets._find_near_tile(
                    pattern,
                    allowed,
                    None,
                    tolerance,
                    preferred_tiles=preferred_tiles,
                )
                if near is not None:
                    tile_index, hflip, vflip, difference = near
                    self.tilesets.last_match_was_fuzzy = True
                    self.tilesets.last_match_difference = difference
                    return tile_index, hflip, vflip
            return None

        def score(
            candidate: tuple[int, bool, bool],
        ) -> tuple[int, int, int, int, int, int, int]:
            tile_index, hflip, vflip = candidate
            secondary_preferred = (
                0
                if scope == "primary"
                else (
                    0 if tile_index >= self.project.settings.num_tiles_in_primary else 1
                )
            )
            return (
                0 if tile_index in used_tiles else 1,
                0 if tile_index in preferred_tiles else 1,
                0 if tile_index not in self._replace_claimed_tiles else 1,
                1 if tile_index in animated else 0,
                secondary_preferred,
                tile_index,
                int(hflip) + int(vflip),
            )

        chosen = min(candidates, key=score)
        tile_index = chosen[0]
        if (
            tile_index in self._replace_recycle_pools["primary"]
            or tile_index in (self._replace_recycle_pools["secondary"])
        ):
            self._replace_claimed_tiles.add(tile_index)
        return chosen

    def _write_replace_tile(
        self, tile_index: int, pattern: bytes, palette: int | None = None
    ) -> None:
        tileset = self.tilesets.tileset_for_tile(tile_index)
        local = tile_index - tileset.global_tile_base
        if not 0 <= local < len(tileset.patterns):
            raise MapEditorError(
                f"Cannot overwrite tile 0x{tile_index:03X} outside {tileset.name}."
            )
        if tile_index in self._replace_protected_tiles:
            raise MapEditorError(
                f"Refusing to overwrite tile 0x{tile_index:03X}: it is still used "
                "by a metatile that remains in this map."
            )
        if self._replace_write_journal is not None:
            self._replace_write_journal.append(
                (tileset, local, tileset.patterns[local])
            )
        tileset.patterns[local] = pattern
        tileset._tile_matcher = None
        tileset.dirty_tiles = True
        tileset._reserved_tiles.add(tile_index)
        self._replace_claimed_tiles.add(tile_index)
        self._replace_tile_writes[tile_index] = pattern
        if palette is not None:
            self._replace_tile_palettes.setdefault(tile_index, palette)

    def _allocate_replace_tile(
        self,
        pattern: bytes,
        scope: str,
        preferred_tiles: set[int],
        palette: int | None = None,
    ) -> int:
        animated = self.tilesets.animated_tile_indices()
        primary_limit = self.project.settings.num_tiles_in_primary
        if scope == "primary":
            owned = sorted(
                tile
                for tile in preferred_tiles
                if 1 <= tile < primary_limit
                and tile not in self._replace_claimed_tiles
                and tile not in self._replace_protected_tiles
                and tile not in animated
            )
        else:
            owned = sorted(
                tile
                for tile in preferred_tiles
                if primary_limit <= tile < self.project.settings.num_tiles_total
                and tile not in self._replace_claimed_tiles
                and tile not in self._replace_protected_tiles
                and tile not in animated
            )
        for tile_index in owned:
            self._write_replace_tile(tile_index, pattern, palette)
            return tile_index

        if scope == "primary":
            pool = self._replace_recycle_pools["primary"]
            ordered = [tile for tile in pool if tile in preferred_tiles] + [
                tile for tile in pool if tile not in preferred_tiles
            ]
            for tile_index in ordered:
                if (
                    tile_index in self._replace_claimed_tiles
                    or tile_index in self._replace_protected_tiles
                ):
                    continue
                self._write_replace_tile(tile_index, pattern, palette)
                return tile_index
            raise MapEditorError(
                "Replace mode exhausted the primary tileset's recyclable 8x8 tile "
                "slots. Send the remaining content to the secondary tileset or free "
                "more primary metatiles."
            )

        secondary = self.tilesets.secondary
        pool = self._replace_recycle_pools["secondary"]
        ordered = [tile for tile in pool if tile in preferred_tiles] + [
            tile for tile in pool if tile not in preferred_tiles
        ]
        for tile_index in ordered:
            if (
                tile_index in self._replace_claimed_tiles
                or tile_index in self._replace_protected_tiles
            ):
                continue
            self._write_replace_tile(tile_index, pattern, palette)
            return tile_index

        before = len(secondary.patterns)
        tile_index = secondary.allocate_tile(pattern)
        if tile_index >= self.project.settings.num_tiles_total:
            raise MapEditorError(
                "Replace mode exhausted the secondary tileset's 8x8 tile slots."
            )
        if len(secondary.patterns) > before:
            self.result.added_tiles += 1
        self._replace_claimed_tiles.add(tile_index)
        self._replace_tile_writes[tile_index] = pattern
        if palette is not None:
            self._replace_tile_palettes.setdefault(tile_index, palette)
        return tile_index

    def _resolve_signature_entries(
        self, unit: ReplaceUnit, scope: str, allocator: _ReplaceAllocator
    ) -> tuple[list[TileRef], int]:
        """Builds the final metatile from the authoritative edited signature."""
        zeros = [
            TileRef(0, 0, False, False)
            for _ in range(self.tilesets.entries_per_metatile)
        ]
        quantize = self.quantize
        if self.tiny_edit_pixels and unit.changes and all(
            change.changed_pixels <= self.tiny_edit_pixels for change in unit.changes
        ):
            quantize = "nearest"
        fuzzy_count = 0
        per_layer: list[list[TileRef] | None] = [None, None, None]
        substitutions = 0
        for layer in range(3):
            if self.tilesets.entries_per_metatile == 8 and layer == 1:
                continue
            layer_entries: list[TileRef] = []
            for subtile in range(4):
                sx = (subtile % 2) * TILE_SIZE
                sy = (subtile // 2) * TILE_SIZE
                patch = normalize_patch(
                    patch_from_rgba(
                        unit.signature[layer],
                        METATILE_SIZE,
                        sx,
                        sy,
                        TILE_SIZE,
                    )
                )
                opaque = any(patch[index + 3] for index in range(0, len(patch), 4))
                if not opaque:
                    layer_entries.append(TileRef(0, 0, False, False))
                    continue
                entry = self._reuse_entry_for_patch(unit, layer, subtile, patch)
                if entry is not None and self._entry_alpha_matches(entry, patch):
                    layer_entries.append(entry)
                    continue
                preferred_palette = self._preferred_palette(unit, layer, subtile)
                pattern, palette, exact, _ = self.tilesets.secondary.quantize_patch(
                    patch,
                    allowed_palettes=list(allocator.allowed_palettes),
                    preferred_palette=preferred_palette,
                    mode=quantize,
                    palette_bank=self.tilesets.palette_bank(),
                )
                if not exact:
                    substitutions += 1
                entry = allocator.get(pattern, palette, patch)
                if self.tilesets.last_match_was_fuzzy:
                    fuzzy_count += 1
                if (
                    not self.tilesets.last_match_was_fuzzy
                    and not self._entry_alpha_matches(entry, patch)
                ):
                    raise MapEditorError(
                        f"Replace mode produced an invalid alpha mask for "
                        f"{unit.changes[0].label if unit.changes else 'a duplicate slot'}."
                    )
                layer_entries.append(entry)
            per_layer[layer] = layer_entries
        if fuzzy_count:
            for change in unit.changes:
                change.fuzzy_tiles += fuzzy_count
        return self.tilesets.build_raw_entries(zeros, per_layer), substitutions

    @staticmethod
    def _entry_key(entry: TileRef) -> tuple[int, int, bool, bool]:
        return (entry.tile_index, entry.palette, entry.hflip, entry.vflip)

    def _layer_entries(
        self, metatile: list[TileRef], layer: int
    ) -> list[TileRef | None]:
        owner = self.tilesets.metatile_owner(0)
        return owner.entries_for_layer(metatile, layer)

    def _remap_static_layer_sequences(
        self, units: list[ReplaceUnit], assigned_slots: set[int]
    ) -> int:
        """Carries unchanged static art over to its replacement tiles.

        A metatile such as 0x048 still has the old grass floor at the bottom
        while its slot was not replaced. If the floor metatile now uses new
        tiles, those unchanged metatiles must follow the new tiles/palettes or
        they render the old pattern with the rewritten palette.
        """
        entries_per = self.tilesets.entries_per_metatile
        animated = self.tilesets.animated_tile_indices()
        mappings: dict[
            tuple[int, int, tuple[int, int, bool, bool]],
            tuple[int, int, bool, bool],
        ] = {}
        for unit in units:
            if unit.duplicate or not unit.changes or unit.resolved_entries is None:
                continue
            new_entries = unit.resolved_entries
            for source_id in unit.source_ids:
                old_entries = self._replace_snapshots.get(source_id)
                if old_entries is None:
                    continue
                old_layers = {
                    layer
                    for layer in range(3)
                    if any(
                        entry is not None and entry.tile_index != 0
                        for entry in self._layer_entries(old_entries, layer)
                    )
                }
                new_layers = {
                    layer
                    for layer in range(3)
                    if any(
                        entry is not None and entry.tile_index != 0
                        for entry in self._layer_entries(new_entries, layer)
                    )
                }
                for layer in old_layers & new_layers:
                    old_layer = self._layer_entries(old_entries, layer)
                    new_layer = self._layer_entries(new_entries, layer)
                    for subtile in range(4):
                        old_entry = (
                            old_layer[subtile] if subtile < len(old_layer) else None
                        )
                        new_entry = (
                            new_layer[subtile] if subtile < len(new_layer) else None
                        )
                        if (
                            old_entry is None
                            or new_entry is None
                            or old_entry.tile_index == 0
                            or new_entry.tile_index == 0
                        ):
                            continue
                        old_key = self._entry_key(old_entry)
                        new_key = self._entry_key(new_entry)
                        if old_key == new_key:
                            continue
                        if (
                            old_entry.tile_index in animated
                            or new_entry.tile_index in animated
                            or old_entry.tile_index in self._replace_tile_writes
                        ):
                            continue
                        key = (layer, subtile, old_key)
                        mappings.setdefault(key, new_key)
        if not mappings:
            return 0

        changed_metatiles = 0
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            base = tileset.global_metatile_base
            for local, metatile in enumerate(tileset.metatiles):
                metatile_id = base + local
                if metatile_id in assigned_slots:
                    continue
                changed = False
                for (layer, subtile, old_key), new_key in mappings.items():
                    layer_entries = tileset.entries_for_layer(metatile, layer)
                    if subtile >= len(layer_entries):
                        continue
                    entry = layer_entries[subtile]
                    if entry is None or self._entry_key(entry) != old_key:
                        continue
                    layer_entries[subtile] = TileRef(
                        new_key[0], new_key[1], new_key[2], new_key[3]
                    )
                    if entries_per == 12:
                        metatile[layer * 4 : (layer + 1) * 4] = layer_entries
                    elif layer == 0:
                        metatile[0:4] = layer_entries
                    elif layer == 2:
                        metatile[4:8] = layer_entries
                    changed = True
                if changed:
                    tileset.dirty_metadata = True
                    changed_metatiles += 1
        if changed_metatiles:
            self.result.warnings.append(
                f"Updated {changed_metatiles} unchanged metatile(s) to follow the "
                "replacement tile colours (for example floor under doors/houses)."
            )
        return changed_metatiles

    def _apply_replace_tile_palettes(self) -> int:
        """Gives every metatile using a rewritten tile the tile's new palette bank."""
        if not self._replace_tile_palettes:
            return 0
        changed = 0
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            for local, metatile in enumerate(tileset.metatiles):
                changed_metatile = False
                for index, entry in enumerate(metatile):
                    if entry is None:
                        continue
                    palette = self._replace_tile_palettes.get(entry.tile_index)
                    if palette is not None and entry.palette != palette:
                        metatile[index] = TileRef(
                            entry.tile_index,
                            palette,
                            entry.hflip,
                            entry.vflip,
                        )
                        changed_metatile = True
                if changed_metatile:
                    tileset.dirty_metadata = True
                    changed += 1
        if changed:
            self.result.warnings.append(
                f"Assigned the replacement palette bank to {changed} metatile(s) "
                "that share overwritten tiles (including metatiles outside Littleroot)."
            )
        return changed

    def _apply_replace_groups(
        self, groups: list[Group], remaps: list[Change], working: list[int]
    ) -> None:
        units = self._replace_plan_units
        if units is None:
            units = self._build_replace_units(groups)
            if not units:
                return
            freed_ids = self._replace_freed_ids(groups, remaps)
            self._replace_plan_freed = freed_ids
            self._assign_replace_slots(units, freed_ids)
            self._replace_plan_units = units
        else:
            freed_ids = self._replace_plan_freed or set()

        critical = self._critical_metatile_ids()
        old_ids = self._old_layout_ids()
        source_ids = set(old_ids)
        for unit in units:
            source_ids.update(unit.source_ids)
            if unit.assigned_id is not None:
                source_ids.add(unit.assigned_id)
        self._replace_snapshots = {}
        self._replace_attribute_snapshots = {}
        for metatile_id in source_ids:
            try:
                self._replace_snapshots[metatile_id] = list(
                    self.tilesets.metatile(metatile_id)
                )
                self._replace_attribute_snapshots[metatile_id] = (
                    self.tilesets.attribute(metatile_id)
                )
            except MapEditorError:
                continue

        replaced_indices = {
            change.index
            for unit in units
            if not unit.duplicate
            for change in unit.changes
        }
        kept_ids = {
            word & self.project.settings.metatile_id_mask
            for index, word in enumerate(working)
            if index not in replaced_indices
        }
        try:
            kept_ids.update(
                word & self.project.settings.metatile_id_mask
                for word in self.project.read_border(self.layout)
            )
        except MapEditorError:
            pass
        kept_ids |= critical

        # Reusable tile slots must be collected before the old metatiles are
        # cleared, because tile ownership lives in those entries.
        self._replace_recycle_pools = self._build_replace_recycle_pools(
            freed_ids, critical, kept_ids
        )
        self._replace_claimed_tiles.clear()
        self._replace_tile_writes.clear()
        self._replace_tile_palettes.clear()
        self._replace_unit_entries.clear()

        # Clear every replacement slot first. The tile graphics stay in place
        # until a new tile actually claims that slot, which is what makes
        # tile-level replacement possible without growing the primary pool.
        # Freed leftovers whose art is still used outside this layout must stay
        # intact; the rest can remain empty for future additions.
        assigned_slots = {
            unit.assigned_id for unit in units if unit.assigned_id is not None
        }
        empty_slots: set[int] = set()
        external_incomplete: set[int] = set()
        external_usage: dict[int, int] = {}
        if self.reuse_unused_metatiles:
            candidates = freed_ids - critical
            if candidates:
                external_usage, external_incomplete, _ = (
                    self._targeted_reference_counts(
                        candidates, exclude_layout_id=self.layout.id
                    )
                )
            empty_slots = {
                metatile_id
                for metatile_id in candidates
                if metatile_id not in assigned_slots
                and metatile_id not in external_incomplete
                and external_usage.get(metatile_id, 0) == 0
            }
            self.result.left_empty_replace_slots += len(empty_slots)
            if empty_slots:
                self.result.warnings.append(
                    f"Left {len(empty_slots)} freed metatile slot(s) empty instead of "
                    "duplicating already-replaced content."
                )
            kept_leftovers = external_incomplete - assigned_slots
            if kept_leftovers:
                self.result.warnings.append(
                    f"Kept {len(kept_leftovers)} freed metatile slot(s) intact because "
                    "not every layout using this tileset pair could be read."
                )
        slots_to_clear = set(assigned_slots) | empty_slots
        for metatile_id in slots_to_clear:
            owner = self.tilesets.metatile_owner(metatile_id)
            local = metatile_id - owner.global_metatile_base
            owner.metatiles[local] = [
                TileRef(0, 0, False, False) for _ in range(owner.entries_per_metatile)
            ]
            owner.attributes[local] = bytes(owner.attribute_size)
            owner.dirty_metadata = True
        self.project.invalidate_tile_usage()
        self.tilesets.invalidate_signature_index()

        def write_assigned_unit(unit: ReplaceUnit, scope: str) -> None:
            assert unit.assigned_id is not None
            owner = self.tilesets.metatile_owner(unit.assigned_id)
            if unit.duplicate and unit.resolved_entries is not None:
                entries = list(unit.resolved_entries)
                substitutions = 0
            else:
                allocator = self._ReplaceAllocator(self, unit, scope)
                entries, substitutions = self._resolve_signature_entries(
                    unit, scope, allocator
                )
                unit.resolved_entries = list(entries)
                self._replace_unit_entries[id(unit)] = list(entries)
            local = unit.assigned_id - owner.global_metatile_base
            owner.metatiles[local] = entries
            owner.attributes[local] = unit.attribute[: owner.attribute_size].ljust(
                owner.attribute_size, b"\0"
            )
            owner.dirty_metadata = True
            if not unit.duplicate:
                for member in unit.changes:
                    working[member.index] = self._set_metatile_id(
                        self.project, working[member.index], unit.assigned_id
                    )
            self.result.replaced_metatiles += 1
            if substitutions:
                self.result.warnings.append(
                    f"Replaced metatile 0x{unit.assigned_id:03X} required "
                    f"{substitutions} palette substitution(s)."
                )

        def append_unit(unit: ReplaceUnit) -> None:
            # Excess content is appended to the secondary tileset. Secondary
            # metatiles may reference both primary and secondary tile slots.
            allocator = self._ReplaceAllocator(self, unit, "combined")
            entries, substitutions = self._resolve_signature_entries(
                unit, "combined", allocator
            )
            existing = self._find_metatile_with_entries(entries)
            if existing is not None:
                new_id = existing
            else:
                new_id = self.tilesets.secondary.allocate_metatile(
                    entries,
                    unit.attribute,
                    prefer_unused=self.reuse_unused_metatiles,
                )
                self.result.added_metatiles += 1
            for member in unit.changes:
                working[member.index] = self._set_metatile_id(
                    self.project, working[member.index], new_id
                )
            if substitutions:
                self.result.warnings.append(
                    f"New metatile for {unit.changes[0].label} required "
                    f"{substitutions} palette substitution(s)."
                )

        # Phase 1: real primary replacements. If the static primary tile pool
        # cannot express one of them, roll the trial back and append that
        # content to the secondary tileset instead.
        primary_slots = sorted(
            metatile_id
            for metatile_id in freed_ids - critical
            if self.tilesets.metatile_owner(metatile_id).kind == "primary"
        )
        successful_primary: list[ReplaceUnit] = []
        primary_real = sorted(
            (
                unit
                for unit in units
                if unit.assigned_id is not None
                and not unit.duplicate
                and self.tilesets.metatile_owner(unit.assigned_id).kind == "primary"
            ),
            key=lambda unit: (
                -len(unit.changes),
                unit.assigned_id if unit.assigned_id is not None else 1 << 30,
            ),
        )
        for unit in primary_real:
            claimed_before = set(self._replace_claimed_tiles)
            writes_before = set(self._replace_tile_writes)
            reserved_before = {
                id(tileset): set(tileset._reserved_tiles)
                for tileset in (self.tilesets.primary, self.tilesets.secondary)
            }
            self._replace_write_journal = []
            try:
                write_assigned_unit(unit, "primary")
                successful_primary.append(unit)
            except MapEditorError as exc:
                for tileset, local, old_pattern in reversed(
                    self._replace_write_journal
                ):
                    tileset.patterns[local] = old_pattern
                    tileset._tile_matcher = None
                    tileset.dirty_tiles = True
                self._replace_claimed_tiles = claimed_before
                for tile_index in set(self._replace_tile_writes) - writes_before:
                    self._replace_tile_writes.pop(tile_index, None)
                    self._replace_tile_palettes.pop(tile_index, None)
                for tileset in (self.tilesets.primary, self.tilesets.secondary):
                    tileset._reserved_tiles = reserved_before[id(tileset)]
                    tileset.invalidate_caches()
                self.tilesets.invalidate_signature_index()
                self.result.warnings.append(
                    f"Primary replace slot 0x{unit.assigned_id:03X} could not be "
                    f"built from the static primary tile pool; its content will be "
                    f"added to the secondary tileset instead ({exc})."
                )
                unit.assigned_id = None
                unit.target_kind = "secondary"
            finally:
                self._replace_write_journal = None

        # Phase 2: every primary freed slot that was not replaced by real
        # content gets a duplicate of the closest successfully built primary
        # content. Duplicates reuse existing patterns, so they never need a new
        # primary tile slot.
        successful_slots = {
            unit.assigned_id
            for unit in successful_primary
            if unit.assigned_id is not None
        }
        for metatile_id in primary_slots:
            if metatile_id in successful_slots:
                continue
            if self.reuse_unused_metatiles:
                if metatile_id in empty_slots:
                    continue
                if (
                    metatile_id not in external_incomplete
                    and external_usage.get(metatile_id, 0) == 0
                ):
                    empty_slots.add(metatile_id)
                    self.result.left_empty_replace_slots += 1
                    continue
                snapshot = self._replace_snapshots.get(metatile_id)
                if snapshot is not None:
                    owner = self.tilesets.metatile_owner(metatile_id)
                    local = metatile_id - owner.global_metatile_base
                    owner.metatiles[local] = snapshot
                    owner.attributes[local] = self._replace_attribute_snapshots.get(
                        metatile_id, bytes(owner.attribute_size)
                    )
                    owner.dirty_metadata = True
                    self.result.warnings.append(
                        f"Freed primary slot 0x{metatile_id:03X} is still used outside "
                        "this layout; its original metatile was kept."
                    )
                continue
            try:
                duplicate = self._best_duplicate_for_slot(
                    metatile_id, {"primary": successful_primary, "secondary": []}
                )
                write_assigned_unit(duplicate, "primary")
            except MapEditorError as exc:
                snapshot = self._replace_snapshots.get(metatile_id)
                if snapshot is not None:
                    owner = self.tilesets.metatile_owner(metatile_id)
                    local = metatile_id - owner.global_metatile_base
                    owner.metatiles[local] = snapshot
                    owner.attributes[local] = self._replace_attribute_snapshots.get(
                        metatile_id, bytes(owner.attribute_size)
                    )
                    owner.dirty_metadata = True
                self.result.warnings.append(
                    f"Could not fill freed primary slot 0x{metatile_id:03X}; "
                    f"the original metatile was restored ({exc})."
                )
                continue
            units.append(duplicate)
            successful_primary.append(duplicate)
            successful_slots.add(metatile_id)

        # Phase 3: secondary replacements first, then secondary duplicates,
        # then all excess/failed content appended to the secondary tileset.
        handled = {id(unit) for unit in successful_primary}
        secondary_real = sorted(
            (
                unit
                for unit in units
                if id(unit) not in handled
                and unit.assigned_id is not None
                and not unit.duplicate
            ),
            key=lambda unit: (
                -len(unit.changes),
                unit.assigned_id if unit.assigned_id is not None else 1 << 30,
            ),
        )
        for unit in secondary_real:
            write_assigned_unit(unit, "secondary")
            handled.add(id(unit))

        secondary_duplicates = sorted(
            (
                unit
                for unit in units
                if id(unit) not in handled
                and unit.assigned_id is not None
                and unit.duplicate
            ),
            key=lambda unit: unit.assigned_id or 0,
        )
        for unit in secondary_duplicates:
            write_assigned_unit(unit, "secondary")
            handled.add(id(unit))

        for unit in sorted(
            (unit for unit in units if id(unit) not in handled),
            key=lambda unit: (
                0 if unit.assigned_id is None else 1,
                min(change.index for change in unit.changes)
                if unit.assigned_id is None and unit.changes
                else 0,
                -len(unit.changes),
                unit.assigned_id if unit.assigned_id is not None else 1 << 30,
            ),
        ):
            if unit.assigned_id is not None:
                write_assigned_unit(unit, "secondary")
            else:
                append_unit(unit)
            handled.add(id(unit))

        self._remap_static_layer_sequences(units, assigned_slots)
        self._apply_replace_tile_palettes()

        if self._replace_tile_writes:
            primary_replaced = sum(
                1
                for tile in self._replace_tile_writes
                if tile < self.project.settings.num_tiles_in_primary
            )
            secondary_replaced = len(self._replace_tile_writes) - primary_replaced
            self.result.warnings.append(
                "Replace mode overwrote 8x8 tile slot(s) in place: "
                f"primary {primary_replaced}, secondary {secondary_replaced}. "
                "Metatiles and maps sharing those slots now show the new art."
            )

        self.project.invalidate_tile_usage()
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            tileset.invalidate_caches()
        self.tilesets.invalidate_signature_index()

    def _apply_tile_edits(self, groups: list[Group]) -> None:
        writes: dict[int, tuple[Tileset, int, bytes]] = {}
        for group in groups:
            if group.action != "modify-tiles":
                continue
            for change in group.changes:
                for layer in change.changed_layers:
                    entries = self.tilesets.metatile_owner(
                        change.original_id
                    ).entries_for_layer(
                        self.tilesets.metatile(change.original_id), layer
                    )
                    for subtile in range(4):
                        original_sub = normalize_patch(
                            self._subtile_patch(change.original_layers[layer], subtile)
                        )
                        edited_sub = normalize_patch(
                            self._subtile_patch(change.edited_layers[layer], subtile)
                        )
                        if original_sub == edited_sub:
                            continue
                        original_entry = entries[subtile] or TileRef(0, 0, False, False)
                        pattern, _, _, _ = self.tilesets.secondary.quantize_patch(
                            self._subtile_patch(change.edited_layers[layer], subtile),
                            allowed_palettes=[original_entry.palette],
                            preferred_palette=original_entry.palette,
                            mode="error",
                            palette_bank=self.tilesets.palette_bank(),
                        )
                        stored = flip_pattern(
                            pattern, original_entry.hflip, original_entry.vflip
                        )
                        tileset = self.tilesets.tileset_for_tile(
                            original_entry.tile_index
                        )
                        local = original_entry.tile_index - tileset.global_tile_base
                        if not 0 <= local < len(tileset.patterns):
                            raise MapEditorError(
                                f"Tile 0x{original_entry.tile_index:03X} is outside {tileset.name}."
                            )
                        if (
                            not self.allow_animated_edits
                            and original_entry.tile_index
                            in tileset._protected_tile_indices()
                        ):
                            raise MapEditorError(
                                f"Refusing to overwrite animated tile "
                                f"0x{original_entry.tile_index:03X}. Use add-secondary to "
                                "create a static block, or pass --allow-animated-edits "
                                "if you really want to edit the static frame."
                            )
                        previous = writes.get(original_entry.tile_index)
                        if previous is not None and previous[2] != stored:
                            raise MapEditorError(
                                f"Conflicting edits for shared tile 0x{original_entry.tile_index:03X}. "
                                "Use 'add-secondary' for one of the conflicting edits."
                            )
                        writes[original_entry.tile_index] = (tileset, local, stored)

        for tileset, local, stored in writes.values():
            if tileset.global_tile_base + local in tileset._protected_tile_indices():
                self.result.warnings.append(
                    f"Tile 0x{tileset.global_tile_base + local:03X} is used by a tileset "
                    "animation; runtime animation frames may overwrite this edit."
                )
            tileset.patterns[local] = stored
            tileset._tile_matcher = None
            tileset.dirty_tiles = True
        if writes:
            for tileset in {value[0] for value in writes.values()}:
                tileset.invalidate_caches()
            self.tilesets.invalidate_signature_index()

    def _warn_animated_loss(self, groups: list[Group], working: list[int]) -> None:
        for group in groups:
            if group.action in ("skip", "modify-tiles"):
                continue
            lost = 0
            for change in group.changes:
                if not change.animated_originals:
                    continue
                final_id = (
                    working[change.index] & self.project.settings.metatile_id_mask
                )
                final_metatile = self.tilesets.metatile(final_id)
                owner = self.tilesets.metatile_owner(final_id)
                for layer, subtile, original_tile in change.animated_originals:
                    entries = owner.entries_for_layer(final_metatile, layer)
                    entry = entries[subtile] if subtile < len(entries) else None
                    if entry is None or entry.tile_index != original_tile:
                        lost += 1
            if lost:
                self.result.warnings.append(
                    f"{lost} animated 8x8 cell(s) in this edit were replaced by static "
                    "tiles. The animation will be lost there; use add-secondary with the "
                    "animated tile unchanged, or edit the animation frames/source too."
                )

    def _warn_animated_remap_loss(
        self, remaps: list[Change], working: list[int]
    ) -> None:
        animated = self.tilesets.animated_tile_indices()
        for change in remaps:
            if not change.animated_originals:
                continue
            final_id = working[change.index] & self.project.settings.metatile_id_mask
            final_metatile = self.tilesets.metatile(final_id)
            owner = self.tilesets.metatile_owner(final_id)
            lost = 0
            for layer, subtile, original_tile in change.animated_originals:
                entries = owner.entries_for_layer(final_metatile, layer)
                entry = entries[subtile] if subtile < len(entries) else None
                if entry is None or entry.tile_index not in animated:
                    lost += 1
            if lost:
                self.result.warnings.append(
                    f"{change.label}: remapped block contains {lost} animated cell(s) "
                    "that no longer reference an animated tile; the animation is lost there."
                )

    def apply(
        self,
        groups: list[Group],
        remaps: list[Change],
    ) -> None:
        working = list(self.blocks)
        map_dirty = False

        # Snapshot pre-edit usage counts. Deletion is only offered when the
        # post-edit count is zero, and this snapshot lets the prompt show both.
        pre_counts: dict[int, int] = {}
        if self.on_remove != "keep" and (groups or remaps):
            pre_counts, _, _ = self._targeted_reference_counts(
                self._original_ids(groups, remaps)
            )

        # 1. Global tile edits first, so later tile reuse sees final graphics.
        self._apply_tile_edits(groups)

        # 2. Auto-remaps to existing metatiles.
        for change in remaps:
            existing = change.remap_target_id
            if existing is None:
                existing = self.tilesets.find_matching_metatile(
                    change.signature, global_search=True
                )
            assert existing is not None
            working[change.index] = self._set_metatile_id(
                self.project, working[change.index], existing
            )
            map_dirty = True

        # 3. Replace-mode groups (full overhaul): reuse free/original slots.
        had_replace = any(group.action == "replace" for group in groups)
        self._apply_replace_groups(groups, remaps, working)
        if had_replace:
            map_dirty = True
        if self.result.preferred_secondary_overflow:
            self.result.warnings.append(
                f"{self.result.preferred_secondary_overflow} replace unit(s) would have "
                "overflowed the primary tileset; they were targeted to the secondary "
                "tileset to prefer a secondary variant if one is needed."
            )

        # 4. Modify-metatile groups. They must not disagree about the final
        #    contents of the same original metatile.
        modify_by_original: dict[int, tuple[tuple, list[Change]]] = {}
        for group in groups:
            if group.action != "modify-metatile":
                continue
            if group.original_id in modify_by_original:
                other_signature, other_changes = modify_by_original[group.original_id]
                if other_signature != group.signature:
                    raise MapEditorError(
                        f"Two different edits at {other_changes[0].label} and "
                        f"{group.changes[0].label} both modify metatile 0x{group.original_id:03X}. "
                        "One of them must use 'add-secondary' instead."
                    )
                other_changes.extend(group.changes)
            else:
                modify_by_original[group.original_id] = (
                    group.signature,
                    list(group.changes),
                )

        for original_id, (_, changes) in modify_by_original.items():
            owner = self.tilesets.metatile_owner(original_id)
            scope = owner.kind
            allocator = self._Allocator(self, scope)
            entries, substitutions = self.resolve_entries(changes[0], scope, allocator)
            local = original_id - owner.global_metatile_base
            owner.metatiles[local] = entries
            owner.dirty_metadata = True
            if substitutions:
                self.result.warnings.append(
                    f"Metatile 0x{original_id:03X} required {substitutions} palette substitution(s)."
                )
            self.result.modified_metatiles += 1
        if modify_by_original:
            self.tilesets.invalidate_signature_index()

        # 4. Add-metatile groups.
        for group in groups:
            if group.action not in ("add-secondary", "add-primary"):
                continue
            target_kind = "secondary" if group.action == "add-secondary" else "primary"
            allocator = self._Allocator(
                self, "combined" if target_kind == "secondary" else "primary"
            )
            entries, substitutions = self.resolve_entries(
                group.changes[0],
                "combined" if target_kind == "secondary" else "primary",
                allocator,
            )
            existing = self._find_metatile_with_entries(entries)
            if existing is not None:
                new_id = existing
            else:
                target_tileset = (
                    self.tilesets.secondary
                    if target_kind == "secondary"
                    else self.tilesets.primary
                )
                attribute = self.tilesets.attribute(group.original_id)
                new_id = target_tileset.allocate_metatile(
                    entries,
                    attribute,
                    prefer_unused=self.reuse_unused_metatiles,
                )
                self.result.added_metatiles += 1
            for change in group.changes:
                working[change.index] = self._set_metatile_id(
                    self.project, working[change.index], new_id
                )
                map_dirty = True
            if substitutions:
                self.result.warnings.append(
                    f"New metatile for {group.changes[0].label} required {substitutions} "
                    "palette substitution(s)."
                )

        # 5. Skip groups.
        self.result.skipped = sum(
            len(group.changes) for group in groups if group.action == "skip"
        )

        self._warn_animated_loss(groups, working)
        self._warn_animated_remap_loss(remaps, working)

        force_variant_kinds: set[str] = set()
        if self.force_variant in ("primary", "both"):
            force_variant_kinds.add("primary")
        if self.force_variant in ("secondary", "both"):
            force_variant_kinds.add("secondary")
        overflow_variants = any(
            len(tileset.patterns) > tileset.max_local_tiles
            for tileset in (self.tilesets.primary, self.tilesets.secondary)
        )
        needs_variants = overflow_variants or bool(force_variant_kinds)
        preview_variants: dict[str, object] = {}
        saved_primary = self.tilesets.primary
        saved_secondary = self.tilesets.secondary
        if needs_variants:
            preview_variants = preview_layout_variants(
                self.project,
                self.tilesets,
                self.layout,
                self.target,
                working,
                self.original_tile_counts,
                force_kinds=force_variant_kinds,
            )
            if "primary" in preview_variants:
                self.tilesets.primary = preview_variants["primary"]
            if "secondary" in preview_variants:
                self.tilesets.secondary = preview_variants["secondary"]
            self.tilesets.invalidate_signature_index()

        # 6. Validate exact edits before touching disk.
        try:
            for change in remaps:
                if change.animated_state_remap:
                    continue
                final_id = (
                    working[change.index] & self.project.settings.metatile_id_mask
                )
                metatile = self.tilesets.metatile(final_id)
                for layer in range(3):
                    final_patch = normalize_patch(
                        self.tilesets.render_layer_patch(metatile, layer)
                    )
                    if final_patch != change.signature[layer]:
                        raise MapEditorError(
                            f"Internal validation failed while remapping {change.label}, layer "
                            f"{LAYER_NAMES[layer]}. Nothing was written."
                        )

            for group in groups:
                if group.action == "skip":
                    continue
                change = group.changes[0]
                analysis = self.analyze(change, "combined")
                for edited in group.changes:
                    final_id = (
                        working[edited.index] & self.project.settings.metatile_id_mask
                    )
                    final_patches = {}
                    try:
                        metatile = self.tilesets.metatile(final_id)
                    except MapEditorError as exc:
                        raise MapEditorError(
                            f"Internal validation failed at {edited.label}: {exc}"
                        ) from exc
                    for layer in range(3):
                        final_patches[layer] = normalize_patch(
                            self.tilesets.render_layer_patch(metatile, layer)
                        )
                    for layer in range(3):
                        desired = edited.signature[layer]
                        if group.action == "modify-tiles":
                            # Only the cells the user actually edited must match.
                            # Other cells may change as a side effect when they share
                            # the edited tile, which is the point of a global tile edit.
                            side_effect = 0
                            for subtile in range(4):
                                final_sub = self._subtile_patch(
                                    final_patches[layer], subtile
                                )
                                desired_sub = self._subtile_patch(desired, subtile)
                                original_sub = self._subtile_patch(
                                    edited.original_layers[layer], subtile
                                )
                                if original_sub == desired_sub:
                                    side_effect += visualize_diff(
                                        final_sub, desired_sub
                                    )
                                    continue
                                if final_sub != desired_sub:
                                    diff = visualize_diff(final_sub, desired_sub)
                                    if (
                                        edited.fuzzy_tiles
                                        and diff
                                        <= self._tile_tolerance_for(edited)
                                    ):
                                        self.result.warnings.append(
                                            f"{edited.label} {LAYER_NAMES[layer]}: "
                                            f"{diff} pixel(s) were harmonized to a nearby "
                                            "existing 8x8 tile."
                                        )
                                        continue
                                    raise MapEditorError(
                                        f"Internal validation failed at {edited.label}, layer "
                                        f"{LAYER_NAMES[layer]} (edited 8x8 cell, {diff} pixels differ). "
                                        "Nothing was written."
                                    )
                            if side_effect:
                                self.result.warnings.append(
                                    f"{edited.label} {LAYER_NAMES[layer]}: {side_effect} pixel(s) "
                                    "outside the edited 8x8 cells changed because they share the "
                                    "same global tile."
                                )
                            continue
                        if final_patches[layer] != desired:
                            diff = visualize_diff(final_patches[layer], desired)
                            if edited.fuzzy_tiles and diff <= (
                                self._tile_tolerance_for(edited)
                                * max(1, edited.fuzzy_tiles)
                            ):
                                self.result.warnings.append(
                                    f"{edited.label} {LAYER_NAMES[layer]}: {diff} "
                                    "pixel(s) were harmonized to nearby existing "
                                    "8x8 tile(s)."
                                )
                                continue
                            if analysis.exact and not (
                                self.result.palette_changed
                                or self.tilesets.palette_rewrites
                            ):
                                raise MapEditorError(
                                    f"Internal validation failed at {edited.label}, layer "
                                    f"{LAYER_NAMES[layer]} ({diff} pixels differ). Nothing was written."
                                )
                            reason = (
                                "palette colours were rewritten"
                                if self.result.palette_changed
                                or self.tilesets.palette_rewrites
                                else "palette quantisation"
                            )
                            self.result.warnings.append(
                                f"{edited.label} {LAYER_NAMES[layer]} differs by {diff} pixel(s) "
                                f"because {reason}."
                            )
        finally:
            if preview_variants:
                self.tilesets.primary = saved_primary
                self.tilesets.secondary = saved_secondary
                self.tilesets.invalidate_signature_index()

        # 7. Persist.
        if self.resized:
            map_dirty = True
        if self.dry_run and needs_variants:
            if overflow_variants:
                self.result.warnings.append(
                    "A tileset exceeded the hardware tile limit. A real import would "
                    "create a per-map tileset variant."
                )
            else:
                self.result.warnings.append(
                    "A real import would create a per-map tileset variant because "
                    "--force-variant was requested."
                )
        if not self.dry_run:
            touched_paths: list[Path] = []
            if map_dirty:
                touched_paths.append(
                    self.project.layout_path(self.layout.blockdata_filepath)
                )
            if self.resized or needs_variants:
                touched_paths.append(self.project.paths["layouts_json"])
            if needs_variants:
                touched_paths.extend(
                    [
                        self.project.root / "include" / "tilesets.h",
                        self.project.root / "src" / "graphics.c",
                        self.project.root / "src" / "data" / "tilesets" / "graphics.h",
                        self.project.root / "src" / "data" / "tilesets" / "metatiles.h",
                        self.project.root / "src" / "data" / "tilesets" / "headers.h",
                    ]
                )
                if self.shift_events and (self.dx or self.dy):
                    for map_entry in self.project.maps_using_layout(self.layout.id):
                        touched_paths.append(map_entry.folder / "map.json")
                elif self.target.map is not None:
                    touched_paths.append(self.target.map.folder / "map.json")
            for tileset in (self.tilesets.primary, self.tilesets.secondary):
                # Removal/tile cleanup may touch these after the primary save,
                # so always include them in the backup set.
                touched_paths.extend(
                    [
                        tileset.metatiles_path,
                        tileset.attributes_path,
                        tileset.tiles_png_path,
                    ]
                )
                touched_paths.extend(
                    tileset.palette_path(index)
                    for index in range(len(tileset.palettes))
                )
            touched_paths.append(self.project.paths["graphics_h"])
            if self.backup:
                self.result.backup_dir = self._create_backup(touched_paths)

            if needs_variants:
                resized_width, resized_height = self.layout.width, self.layout.height
                variant_result = create_layout_variants(
                    self.project,
                    self.tilesets,
                    self.layout,
                    self.target,
                    working,
                    self.original_tile_counts,
                    force_kinds=force_variant_kinds,
                )
                if variant_result.changed:
                    if "primary" in variant_result.variants:
                        self.tilesets.primary = variant_result.variants["primary"]
                    if "secondary" in variant_result.variants:
                        self.tilesets.secondary = variant_result.variants["secondary"]
                    self.layout = replace(
                        self.project.layouts.get(self.layout.id, self.layout),
                        width=resized_width,
                        height=resized_height,
                    )
                    self.result.warnings.extend(variant_result.warnings)
                    self.result.written.extend(variant_result.written)

            for tileset in (self.tilesets.primary, self.tilesets.secondary):
                if self.project._variant_registry_path(tileset).is_file():
                    synced = sync_variants_from_base(self.project, tileset)
                    if synced:
                        self.result.written.extend(synced)
                        self.result.warnings.append(
                            f"Propagated {tileset.name} changes to its tileset variants."
                        )

            for tileset in (self.tilesets.primary, self.tilesets.secondary):
                self.result.written.extend(tileset.save())
                if (
                    tileset.dirty_metadata
                    or tileset.dirty_tiles
                    or tileset.dirty_palette_indices
                ):
                    self.result.touched_tilesets.append((tileset.name, tileset.kind))
                    if (tileset.directory / "porytiles_src").is_dir():
                        self.result.warnings.append(
                            f"{tileset.name} is Porytiles-managed. Its porytiles_src/ assets are now "
                            f"stale; run 'porytiles decompile-tileset {tileset.name}' "
                            "(or rerun this import with --sync-porytiles) to refresh them."
                        )
            if self.resized:
                self.layout = self.project.update_layout_size(
                    self.layout.id, self.layout.width, self.layout.height
                )
                self.result.written.append(self.project.paths["layouts_json"])
            if map_dirty:
                self.project.write_blocks(self.layout, working)
                self.result.written.append(
                    self.project.layout_path(self.layout.blockdata_filepath)
                )
            if self.resized and (self.dx or self.dy):
                if self.shift_events:
                    for map_entry in self.project.maps_using_layout(self.layout.id):
                        if self.project.shift_map_events(
                            map_entry.folder, self.dx, self.dy
                        ):
                            self.result.written.append(map_entry.folder / "map.json")
                else:
                    self.result.warnings.append(
                        "Layout origin moved but --no-shift-events was used; map event "
                        "coordinates were NOT adjusted. Review all maps using this layout."
                    )
            self.project._global_tile_usage_cache.clear()
            self.project.invalidate_metatile_usage()

            self._handle_removals(groups, remaps, pre_counts)
            self._handle_unused_tiles()
        else:
            self.result.written = []

        self.blocks = working

    # Removal / cleanup --------------------------------------------------

    def _clear_metatile(self, tileset: Tileset, local: int) -> None:
        tileset.metatiles[local] = [
            TileRef(0, 0, False, False) for _ in range(tileset.entries_per_metatile)
        ]
        tileset.attributes[local] = bytes(tileset.attribute_size)
        tileset.dirty_metadata = True
        tileset.invalidate_caches()
        if (tileset.directory / "porytiles_src").is_dir():
            self.result.warnings.append(
                f"{tileset.name} is Porytiles-managed. Its porytiles_src/ assets are now "
                f"stale; run 'porytiles decompile-tileset {tileset.name}' to refresh them."
            )

    def _metatile_is_visually_empty(self, metatile: list[TileRef]) -> bool:
        """True when every referenced 8x8 pattern is fully transparent.

        Palette index 0 is the GBA transparency colour, so a metatile whose
        visible pixels are all index 0 is already a free slot even if its
        entries do not literally point at tile 0.
        """
        for entry in metatile:
            if entry is None or entry.tile_index == 0:
                continue
            owner = self.tilesets.tileset_for_tile(entry.tile_index)
            local = entry.tile_index - owner.global_tile_base
            if not 0 <= local < len(owner.patterns):
                return False
            if any(owner.patterns[local]):
                return False
        return True

    def _reuse_unused_metatiles(self) -> None:
        """Clears globally unused metatile slots so new content can reuse them."""
        if not self.reuse_unused_metatiles:
            return
        critical = self._critical_metatile_ids()
        pairs: list[tuple[Tileset, set[int]]] = []
        all_ids: set[int] = set()
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            base = tileset.global_metatile_base
            ids = set(range(base, base + len(tileset.metatiles)))
            pairs.append((tileset, ids))
            all_ids.update(ids)
        if not all_ids:
            return

        counts, incomplete, incomplete_layouts = self._targeted_reference_counts(
            all_ids
        )
        if self.empty_metatiles_only:
            for tileset, ids in pairs:
                if ids & incomplete:
                    layouts = sorted(
                        {
                            layout
                            for metatile_id in ids & incomplete
                            for layout in incomplete_layouts.get(metatile_id, set())
                        }
                    )
                    self.result.warnings.append(
                        f"Cannot reuse empty metatiles in {tileset.name}: unreadable "
                        f"layout(s): {', '.join(layouts[:8])}; skipped."
                    )
                    continue
                kept: list[int] = []
                for local, metatile in enumerate(tileset.metatiles):
                    metatile_id = tileset.global_metatile_base + local
                    if counts.get(metatile_id, 0) != 0:
                        continue
                    if self._metatile_is_visually_empty(metatile):
                        continue
                    # Keep unused art available for other maps/tools; the
                    # allocator will only consider unreserved empty slots.
                    tileset._reserved_metatiles.add(local)
                    kept.append(local)
                if not kept:
                    continue
                if tileset.kind == "primary":
                    self.result.kept_unused_primary += len(kept)
                else:
                    self.result.kept_unused_secondary += len(kept)
                self.result.warnings.append(
                    f"Kept {len(kept)} zero-usage metatile slot(s) with content in "
                    f"{tileset.name}; only already-empty slots will be reused."
                )
            return
        plans: list[tuple[Tileset, list[int]]] = []
        for tileset, ids in pairs:
            if ids & incomplete:
                layouts = sorted(
                    {
                        layout
                        for metatile_id in ids & incomplete
                        for layout in incomplete_layouts.get(metatile_id, set())
                    }
                )
                self.result.warnings.append(
                    f"Cannot clear unused metatiles in {tileset.name}: unreadable "
                    f"layout(s): {', '.join(layouts[:8])}; skipped."
                )
                continue
            slots: list[int] = []
            for local, metatile in enumerate(tileset.metatiles):
                metatile_id = tileset.global_metatile_base + local
                if counts.get(metatile_id, 0) != 0:
                    continue
                if metatile_id in critical:
                    continue
                if self.project.is_protected_metatile_id(metatile_id, tileset.name):
                    continue
                if self._metatile_is_visually_empty(metatile):
                    # Already fully transparent: available without touching it.
                    continue
                slots.append(local)
            if slots:
                plans.append((tileset, slots))

        if not plans:
            return
        for tileset, slots in plans:
            for local in slots:
                tileset.metatiles[local] = [
                    TileRef(0, 0, False, False)
                    for _ in range(tileset.entries_per_metatile)
                ]
                tileset.attributes[local] = bytes(tileset.attribute_size)
            tileset.dirty_metadata = True
            if tileset.kind == "primary":
                self.result.cleared_unused_primary += len(slots)
            else:
                self.result.cleared_unused_secondary += len(slots)
            self.result.warnings.append(
                f"Cleared {len(slots)} unused metatile slot(s) in {tileset.name}; "
                "new content will reuse them first."
            )
        self._metatiles_cleared = True
        self.project.invalidate_metatile_usage()
        self.project.invalidate_tile_usage()
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            tileset.invalidate_caches()
        self.tilesets.invalidate_signature_index()

    def _handle_removals(
        self,
        groups: list[Group],
        remaps: list[Change],
        pre_counts: dict[int, int],
    ) -> None:
        if self.on_remove == "keep":
            return
        original_ids = {group.original_id for group in groups} | {
            change.original_id for change in remaps
        }
        if not original_ids:
            return

        self.project.invalidate_metatile_usage()
        post_counts, incomplete, incomplete_layouts = self._targeted_reference_counts(
            original_ids
        )

        cleared: set[tuple[str, int]] = set()
        for metatile_id in sorted(original_ids):
            owner = self.tilesets.metatile_owner(metatile_id)
            local = metatile_id - owner.global_metatile_base
            if not 0 <= local < len(owner.metatiles):
                continue

            before = pre_counts.get(metatile_id, 0)
            after = post_counts.get(metatile_id, 0)
            if after != 0:
                # Still used somewhere after the edit: never offer deletion.
                continue

            if metatile_id in incomplete:
                unreadable = sorted(incomplete_layouts.get(metatile_id, set()))
                examples = ", ".join(unreadable[:6])
                if len(unreadable) > 6:
                    examples += f", ... (+{len(unreadable) - 6})"
                self.result.warnings.append(
                    f"Metatile 0x{metatile_id:03X} now has 0 usages in readable maps, but "
                    f"not every layout using {owner.name} could be read. Refusing to clear it "
                    "to avoid deleting a metatile that may still be used. "
                    f"Unreadable layouts: {examples or '<unknown>'}."
                )
                continue

            if self.project.is_protected_metatile_id(metatile_id):
                self.result.warnings.append(
                    f"Metatile 0x{metatile_id:03X} is no longer used by any map, but it is "
                    "named in metatile_labels.h (door/script constant), so it was left intact."
                )
                continue

            if self.on_remove == "ask":
                print()
                print(
                    f"Metatile 0x{metatile_id:03X} in {owner.name}: "
                    f"usages before edit = {before}, usages after edit = {after}."
                )
                if not self.prompter.yes_no(
                    "  Clear this now-unused metatile slot from the tileset? [y/N]: ",
                    False,
                ):
                    continue
            self._clear_metatile(owner, local)
            cleared.add((owner.kind, local))
            self.result.touched_tilesets.append((owner.name, owner.kind))
            self.result.removed_metatiles += 1

        if cleared:
            for tileset in (self.tilesets.primary, self.tilesets.secondary):
                if tileset.dirty_metadata:
                    self.result.written.extend(tileset.save())
            self.project._global_tile_usage_cache.clear()
            self.project.invalidate_metatile_usage()

    def _handle_unused_tiles(self) -> None:
        if self.on_remove_tiles == "keep":
            return
        touched = [
            tileset
            for tileset in (self.tilesets.primary, self.tilesets.secondary)
            if tileset.dirty_tiles or tileset.dirty_metadata
        ]
        if not touched:
            return
        for tileset in touched:
            used = self.project.tile_usage_for(tileset)
            candidates = []
            protected = tileset._protected_tile_indices()
            for local, pattern in enumerate(tileset.patterns):
                if local == 0 or not any(pattern):
                    continue
                global_index = tileset.global_tile_base + local
                if global_index in used or global_index in protected:
                    continue
                candidates.append(local)
            if not candidates:
                continue
            if self.on_remove_tiles == "ask":
                print()
                print(
                    f"{len(candidates)} tile graphic(s) in {tileset.name} are no longer "
                    "referenced by any metatile."
                )
                if not self.prompter.yes_no(
                    "  Clear those tile slots (they can then be reused)? [y/N]: ", False
                ):
                    continue
            for local in candidates:
                tileset.patterns[local] = bytes(64)
            tileset.dirty_tiles = True
            self.result.cleared_tiles += len(candidates)
            self.result.touched_tilesets.append((tileset.name, tileset.kind))
            self.result.written.extend(tileset.save())
        self.project._global_tile_usage_cache.clear()

    # Backups ------------------------------------------------------------

    def _create_backup(self, paths: list[Path]) -> Path | None:
        existing = []
        for path in dict.fromkeys(paths):
            if path.is_file():
                existing.append(path)
        if not existing:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")  # noqa: UP017
        backup_root = self.project.root / ".poryeasy-backups" / stamp
        for path in existing:
            try:
                relative = path.relative_to(self.project.root)
            except ValueError:
                relative = Path(path.name)
            destination = backup_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        return backup_root

    # Entry point --------------------------------------------------------

    def _prompt_extra_animation_row(self, index: int, states: int) -> dict | None:
        if not self.prompter.interactive:
            return None
        catalog = scan_animation_catalog(self.project)
        defs = [
            definition
            for definition in catalog.tileset_defs
            if definition.tileset
            in (self.tilesets.primary.name, self.tilesets.secondary.name)
        ]
        print()
        print(
            f"Extra animated sheet row {index + 1} ({states} non-empty state(s)) "
            "has no manifest entry."
        )
        kind = self.prompter.choice(
            "  Is it [m]etatile, [d]oor, or [s]kip? [m]: ",
            {"m", "d", "s", "metatile", "door", "skip"},
            "m",
        )
        if kind in ("s", "skip"):
            return None
        if kind in ("d", "door"):
            doors = [
                door
                for door in catalog.door_defs
                if door.tileset
                in (self.tilesets.primary.name, self.tilesets.secondary.name)
            ]
            if not doors:
                print(
                    "  No existing door definitions are available for this tileset pair."
                )
                return {"kind": "door"}
            print("  Base door definitions:")
            for door_index, door in enumerate(doors, start=1):
                print(f"    [{door_index}] {door.key}")
            raw_door = self.prompter.choice(
                f"  Base door [1-{len(doors)}] [1]: ",
                {str(value) for value in range(1, len(doors) + 1)},
                "1",
            )
            base = doors[int(raw_door) - 1]
            name = self.prompter.text(
                f"  New door name [{index + 1}]: ", f"custom_door_{index + 1}"
            )
            return {
                "kind": "door",
                "base": base.key,
                "name": name,
                "parts": base.parts[0] * base.parts[1],
            }
        if not defs:
            print("  No existing animations are available for this tileset pair.")
            raw = self.prompter.choice(
                "  Create a new tileset animation? [n]: ",
                {"n", "new"},
                "n",
            )
        else:
            print("  Existing animations:")
            for def_index, definition in enumerate(defs, start=1):
                print(f"    [{def_index}] {definition.key}")
            print("    [n] New tileset animation")
            raw = self.prompter.choice(
                f"  Animation [1-{len(defs)}] or [n]ew [1]: ",
                {str(value) for value in range(1, len(defs) + 1)} | {"n", "new"},
                "1",
            )
        if raw in ("n", "new"):
            name = self.prompter.text(
                f"  New animation name [{index + 1}]: ", f"custom_anim_{index + 1}"
            )
            target = self.prompter.choice(
                "  Add animation to [1] secondary or [2] primary? [1]: ",
                {"1", "2", "secondary", "primary"},
                "1",
            )
            target_kind = "primary" if target in ("2", "primary") else "secondary"
            frames = self.prompter.text(f"  Frame count [{states}]: ", str(states))
            try:
                frame_count = min(256, max(1, int(frames)))
            except ValueError:
                print(f"  Invalid frame count '{frames}'; using {states}.")
                frame_count = max(1, states)
            divisor_text = self.prompter.text("  Timer divisor [16]: ", "16")
            try:
                divisor = min(256, max(1, int(divisor_text)))
            except ValueError:
                divisor = 16
            behavior = self.prompter.text(
                "  Behavior name or number (blank uses MB_NORMAL): ", ""
            )
            return {
                "kind": "new_animation",
                "name": name,
                "tileset": target_kind,
                "frames": frame_count,
                "divisor": divisor,
                "behavior": behavior,
            }
        definition = defs[int(raw) - 1]
        target = self.prompter.choice(
            "  Add metatile to [1] secondary or [2] primary? [1]: ",
            {"1", "2", "secondary", "primary"},
            "1",
        )
        target_kind = "primary" if target in ("2", "primary") else "secondary"
        if target_kind == "primary" and definition.kind != "primary":
            print(
                "  That animation belongs to the secondary tileset; a primary metatile cannot use it."
            )
            return None
        behavior = self.prompter.text(
            "  Behavior name or number (blank inherits the animation's behavior): ",
            "",
        )
        return {
            "kind": "metatile",
            "animation": definition.key,
            "tileset": target_kind,
            "behavior": behavior,
        }

    def _backup_animations(self, paths: list[Path]) -> Path | None:
        if not self.backup or self.dry_run:
            return None
        backup_root = self._create_backup(paths)
        if backup_root is not None:
            self.result.backup_dir = backup_root
        return backup_root

    def _palette_exact_match(
        self, colors: list[tuple[int, int, int]], allowed: range
    ) -> bool:
        banks = self.tilesets.palette_bank()
        for bank in allowed:
            palette = banks[bank]
            available = {palette[index] for index in range(1, 16)}
            if all(color in available for color in colors):
                return True
        return False

    def _choose_bank(self, colors: list[tuple[int, int, int]], allowed: range) -> int:
        banks = self.tilesets.palette_bank()

        def score(bank: int) -> tuple[int, int]:
            palette = banks[bank]
            available = {palette[index] for index in range(1, 16)}
            overlap = sum(1 for color in colors if color in available)
            return overlap, -bank

        return max(allowed, key=score)

    def _change_palette_patches(
        self, change: Change
    ) -> list[list[tuple[int, int, int]]]:
        patches: list[list[tuple[int, int, int]]] = []
        for layer in change.changed_layers:
            data = change.edited_layers.get(layer)
            if data is None:
                continue
            for subtile in range(4):
                sx = (subtile % 2) * TILE_SIZE
                sy = (subtile // 2) * TILE_SIZE
                patch = patch_from_rgba(data, METATILE_SIZE, sx, sy, TILE_SIZE)
                unique = list(
                    dict.fromkeys(
                        (patch[index], patch[index + 1], patch[index + 2])
                        for index in range(0, len(patch), 4)
                        if patch[index + 3]
                    )
                )
                if unique:
                    patches.append(unique)
        return patches

    def _palette_fit(self, change: Change, allowed: range) -> tuple[bool, int, int]:
        """Returns (exact, matched_colours, total_colours) for a cell."""
        patches = self._change_palette_patches(change)
        if not patches:
            return True, 0, 0
        banks = self.tilesets.palette_bank()
        available = {
            bank: {banks[bank][index] for index in range(1, 16)} for bank in allowed
        }
        exact = True
        matched = 0
        total = 0
        for unique in patches:
            total += len(unique)
            best = max(
                sum(1 for color in unique if color in available[bank])
                for bank in allowed
            )
            matched += best
            if best < len(unique):
                exact = False
        return exact, matched, total

    def _choose_target_kind(self, unit: ReplaceUnit) -> tuple[str, bool, bool]:
        change = unit.changes[0]
        settings = self.project.settings
        primary_allowed = range(settings.num_pals_in_primary)
        secondary_allowed = range(settings.num_pals_total)
        primary_exact, primary_matched, primary_total = self._palette_fit(
            change, primary_allowed
        )
        secondary_exact, secondary_matched, secondary_total = self._palette_fit(
            change, secondary_allowed
        )

        analysis_primary = self.analyze(change, "primary")
        primary_ok = (
            self._primary_scope_possible(change)
            and self.tilesets.primary.has_free_metatile_slot()
            and (
                analysis_primary.new_tile_count == 0
                or self.tilesets.primary.free_tile_slots()
                >= analysis_primary.new_tile_count
            )
        )
        analysis_secondary = self.analyze(change, "combined")
        secondary_ok = self.tilesets.secondary.has_free_metatile_slot() and (
            analysis_secondary.new_tile_count
            <= self.tilesets.secondary.free_tile_slots()
            + self.tilesets.primary.free_tile_slots()
        )

        if primary_ok and primary_exact:
            return "primary", primary_exact, secondary_exact
        if secondary_ok and secondary_exact:
            return "secondary", primary_exact, secondary_exact
        if primary_ok and (
            not secondary_ok
            or primary_matched * max(1, secondary_total)
            >= secondary_matched * max(1, primary_total)
        ):
            return "primary", primary_exact, secondary_exact
        return "secondary", primary_exact, secondary_exact

    def _prepare_replace_plan(self, groups: list[Group], remaps: list[Change]) -> None:
        units = self._build_replace_units(groups)
        if not units:
            return
        freed_ids = self._replace_freed_ids(groups, remaps)
        self._replace_plan_units = units
        self._replace_plan_freed = freed_ids
        self._assign_replace_slots(units, freed_ids)
        for unit in units:
            if unit.assigned_id is not None:
                owner = self.tilesets.metatile_owner(unit.assigned_id)
                unit.target_kind = owner.kind
            else:
                unit.target_kind = "secondary"
            if unit.changes:
                if unit.target_kind == "primary":
                    primary_exact, _, _ = self._palette_fit(
                        unit.changes[0],
                        range(self.project.settings.num_pals_in_primary),
                    )
                    secondary_exact = False
                else:
                    primary_exact = False
                    secondary_exact, _, _ = self._palette_fit(
                        unit.changes[0],
                        range(self.project.settings.num_pals_total),
                    )
            else:
                primary_exact, secondary_exact = False, False
            if unit.target_kind == "primary":
                self.result.target_primary += 1
            else:
                self.result.target_secondary += 1
            if primary_exact:
                self.result.palette_exact_primary += 1
            if secondary_exact:
                self.result.palette_exact_secondary += 1
            allowed = (
                range(self.project.settings.num_pals_in_primary)
                if unit.target_kind == "primary"
                else range(self.project.settings.num_pals_total)
            )
            for change in unit.changes:
                self._change_allowed_banks[change.index] = allowed

    def _bank_pixel_usage(self, bank: int) -> Counter[tuple[int, int, int]]:
        """Counts old tileset pixels per colour for one palette bank."""
        assigned_slots = {
            unit.assigned_id
            for unit in (self._replace_plan_units or [])
            if unit.assigned_id is not None
        }
        usage: Counter[tuple[int, int, int]] = Counter()
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            base = tileset.global_metatile_base
            for local, metatile in enumerate(tileset.metatiles):
                if base + local in assigned_slots:
                    continue
                for entry in metatile:
                    if entry is None or entry.palette != bank:
                        continue
                    owner = self.tilesets.tileset_for_tile(entry.tile_index)
                    pattern = owner.pattern_at(entry.tile_index)
                    palette = self.tilesets.palette_for(bank)
                    for value in pattern:
                        if value:
                            usage[palette[value]] += 1
        return usage

    def _remap_tile_indices_for_palettes(
        self,
        plans: list[
            tuple[
                int,
                list[tuple[int, int, int]],
                list[tuple[int, int, int]],
                dict[int, int],
            ]
        ],
    ) -> int:
        """Rewrites old tile indices to the closest colours in the new palettes."""
        if not plans:
            return 0
        plans_by_bank = {
            bank: (old_palette, new_palette, mapping)
            for bank, old_palette, new_palette, mapping in plans
        }
        animated = self.tilesets.animated_tile_indices()
        tile_banks: dict[int, Counter[int]] = {}
        for tileset in (self.tilesets.primary, self.tilesets.secondary):
            for metatile in tileset.metatiles:
                for entry in metatile:
                    if entry is None:
                        continue
                    if entry.palette in plans_by_bank:
                        tile_banks.setdefault(entry.tile_index, Counter())[
                            entry.palette
                        ] += 1

        remapped = 0
        compromises = 0
        for tile_index, bank_counts in tile_banks.items():
            if tile_index in animated or tile_index == 0:
                continue
            owner = self.tilesets.tileset_for_tile(tile_index)
            local = tile_index - owner.global_tile_base
            if not 0 <= local < len(owner.patterns):
                continue
            old_pattern = owner.patterns[local]
            if len(bank_counts) > 1:
                compromises += 1
            chosen_bank = min(bank_counts)
            if chosen_bank not in plans_by_bank:
                continue
            _old_palette, _new_palette, mapping = plans_by_bank[chosen_bank]
            new_pattern = bytes(mapping[value] if value else 0 for value in old_pattern)
            if new_pattern == old_pattern:
                continue
            owner.patterns[local] = new_pattern
            owner._tile_matcher = None
            owner.dirty_tiles = True
            remapped += 1
        if remapped:
            self.result.warnings.append(
                f"Adapted {remapped} old 8x8 tile(s) to the new palette colours "
                "using the closest colour index."
            )
        if compromises:
            self.result.warnings.append(
                f"{compromises} shared 8x8 tile(s) use several rewritten palette banks; "
                "the lowest bank mapping was chosen for each."
            )
        return remapped

    def _entry_color_counts(
        self, entry: TileRef, cache: dict[tuple[int, int], Counter]
    ) -> Counter[tuple[int, int, int]]:
        key = (entry.tile_index, entry.palette)
        counts = cache.get(key)
        if counts is None:
            palette = self.tilesets.palette_for(entry.palette)
            pattern = self.tilesets.tileset_for_tile(entry.tile_index).pattern_at(
                entry.tile_index
            )
            counts = Counter()
            for value in pattern:
                if value:
                    counts[palette[value]] += 1
            cache[key] = counts
        return counts

    def _add_entry_usage(
        self,
        entry: TileRef | None,
        target: dict[int, Counter],
        cache: dict[tuple[int, int], Counter],
    ) -> None:
        if entry is None or entry.tile_index == 0:
            return
        target.setdefault(entry.palette, Counter()).update(
            self._entry_color_counts(entry, cache)
        )

    def _metatile_color_usage(
        self,
        metatile_id: int,
        cache: dict[tuple[int, int], Counter],
        metatile_cache: dict[int, dict[int, Counter]],
    ) -> dict[int, Counter]:
        usage = metatile_cache.get(metatile_id)
        if usage is not None:
            return usage
        usage = {}
        try:
            metatile = self.tilesets.metatile(metatile_id)
        except MapEditorError:
            metatile_cache[metatile_id] = usage
            return usage
        for entry in metatile:
            self._add_entry_usage(entry, usage, cache)
        metatile_cache[metatile_id] = usage
        return usage

    def _add_metatile_usage(
        self,
        metatile_id: int,
        target: dict[int, Counter],
        cache: dict[tuple[int, int], Counter],
        metatile_cache: dict[int, dict[int, Counter]],
    ) -> None:
        for bank, counts in self._metatile_color_usage(
            metatile_id, cache, metatile_cache
        ).items():
            target.setdefault(bank, Counter()).update(counts)

    def _plan_edited_palettes(
        self, changes: list[Change], groups: list[Group]
    ) -> None:
        if self.palette_mode not in ("smart", "replace"):
            return
        change_by_index = {change.index: change for change in changes}
        action_by_index: dict[int, str] = {}
        for group in groups:
            for change in group.changes:
                action_by_index[change.index] = group.action
        remap_by_index = {
            change.index: change.remap_target_id
            for change in changes
            if change.remap_target_id is not None
        }

        old_usage: dict[int, Counter] = {}
        new_usage: dict[int, Counter] = {}
        entry_cache: dict[tuple[int, int], Counter] = {}
        metatile_cache: dict[int, dict[int, Counter]] = {}

        for index, word in enumerate(self.blocks):
            original_id = word & self.project.settings.metatile_id_mask
            remap_target = remap_by_index.get(index)
            if remap_target is not None:
                self._add_metatile_usage(
                    remap_target, old_usage, entry_cache, metatile_cache
                )
                continue
            change = change_by_index.get(index)
            action = action_by_index.get(index)
            if change is None or action in (None, "skip"):
                self._add_metatile_usage(
                    original_id, old_usage, entry_cache, metatile_cache
                )
                continue
            if self.tiny_edit_pixels and (
                change.changed_pixels <= self.tiny_edit_pixels
            ):
                # Tiny edits are harmonized: they never influence the palette.
                self._add_metatile_usage(
                    original_id, old_usage, entry_cache, metatile_cache
                )
                continue

            allowed = self._change_allowed_banks.get(index)
            if allowed is None:
                try:
                    owner = self.tilesets.metatile_owner(change.original_id)
                except MapEditorError:
                    owner = None
                allowed = (
                    range(self.project.settings.num_pals_in_primary)
                    if owner is not None and owner.kind == "primary"
                    else range(self.project.settings.num_pals_total)
                )
            try:
                metatile = self.tilesets.metatile(change.original_id)
                owner = self.tilesets.metatile_owner(change.original_id)
            except MapEditorError:
                metatile = None
                owner = None

            for layer in range(3):
                original_entries: list[TileRef | None] = (
                    owner.entries_for_layer(metatile, layer)
                    if owner is not None and metatile is not None
                    else [None] * 4
                )
                data = change.edited_layers.get(layer)
                if layer not in change.changed_layers or data is None:
                    for entry in original_entries:
                        self._add_entry_usage(entry, old_usage, entry_cache)
                    continue
                for subtile in range(4):
                    original_sub = normalize_patch(
                        self._subtile_patch(change.original_layers[layer], subtile)
                    )
                    edited_sub = normalize_patch(
                        self._subtile_patch(data, subtile)
                    )
                    entry = (
                        original_entries[subtile]
                        if subtile < len(original_entries)
                        else None
                    )
                    if original_sub == edited_sub:
                        self._add_entry_usage(entry, old_usage, entry_cache)
                        continue
                    patch = self._subtile_patch(data, subtile)
                    colors = [
                        (patch[i], patch[i + 1], patch[i + 2])
                        for i in range(0, len(patch), 4)
                        if patch[i + 3]
                    ]
                    if not colors:
                        continue
                    unique = list(dict.fromkeys(colors))
                    preferred = (
                        entry.palette
                        if entry is not None and entry.palette in allowed
                        else None
                    )
                    bank = (
                        preferred
                        if preferred is not None
                        else self._choose_bank(unique, allowed)
                    )
                    new_usage.setdefault(bank, Counter()).update(colors)

        map_total = sum(sum(counts.values()) for counts in old_usage.values())
        map_total += sum(sum(counts.values()) for counts in new_usage.values())
        if map_total <= 0:
            return

        palette_plans: list[tuple[int, object]] = []
        remap_plans: list[
            tuple[
                int,
                list[tuple[int, int, int]],
                list[tuple[int, int, int]],
                dict[int, int],
            ]
        ] = []
        for bank in sorted(set(old_usage) | set(new_usage)):
            old_palette = list(self.tilesets.palette_for(bank)[:16])
            existing = {old_palette[index] for index in range(1, 16)}
            desired: Counter[tuple[int, int, int]] = Counter()
            ignored = 0
            for color, count in new_usage.get(bank, Counter()).items():
                if color in existing:
                    continue
                if (
                    self.palette_min_share > 0
                    and count / map_total < self.palette_min_share
                ):
                    ignored += 1
                    continue
                desired[color] = count
            if ignored:
                self.result.ignored_palette_colors += ignored
                self.result.warnings.append(
                    f"Palette bank {bank}: ignored {ignored} new colour(s) below "
                    f"{self.palette_min_share:.4%} of the map; they will use the "
                    "nearest existing palette colour."
                )
            if not desired:
                continue
            plan = plan_bank_colors(
                self.tilesets,
                bank,
                desired,
                self.palette_mode,
                old_usage=old_usage.get(bank, Counter()),
            )
            if not plan.replaced_slots:
                continue
            palette_plans.append((bank, plan))
            remap_plans.append(
                (
                    bank,
                    old_palette,
                    plan.colors,
                    build_index_remap(old_palette, plan.colors),
                )
            )

        changed_palette = False
        for bank, plan in palette_plans:
            apply_palette_plan(self.tilesets, plan)
            changed_palette = True
            self.result.palette_changed = True
            self.result.warnings.append(
                f"Palette bank {bank}: replaced colour slot(s) "
                f"{', '.join(str(slot) for slot in plan.replaced_slots)}. "
                "Old tile indices are remapped to the closest new colours "
                f"({len(plan.substitutions)} colour merge(s))."
            )
            if plan.protected_slots:
                self.result.warnings.append(
                    f"Palette bank {bank}: protected high-use colour slot(s) "
                    f"{', '.join(str(slot) for slot in plan.protected_slots)} from "
                    "replacement."
                )
        if changed_palette:
            self.tilesets.invalidate_signature_index()
            self._remap_tile_indices_for_palettes(remap_plans)
            self.result.warnings.append(
                "Palette colours changed; re-export animated_metatiles.png if you want "
                "the sheet to show the new animation colours."
            )

    def run(self) -> ImportResult:
        if not self.input_dir.is_dir():
            raise MapEditorError(f"Input directory does not exist: {self.input_dir}")
        metadata_path = self.input_dir / "map.json"
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
            if metadata.get("layout_id") not in (None, self.layout.id):
                raise MapEditorError(
                    f"The layer images were exported for layout {metadata.get('layout_id')}, "
                    f"but '{self.target.query}' resolves to {self.layout.id}."
                )

        self._reuse_unused_metatiles()
        palette_result = apply_palette_sheet(
            self.project,
            self.tilesets,
            self.input_dir,
            dry_run=self.dry_run,
            backup_func=self._backup_animations,
            force=self.force_palette_edits,
        )
        self.result.palette_changed = palette_result.changed
        self.result.warnings.extend(palette_result.warnings)
        self.result.written.extend(palette_result.written)
        self.result.touched_tilesets.extend(palette_result.touched_tilesets)
        if palette_result.changed:
            self.tilesets.invalidate_signature_index()
        self.tilesets.palette_mode = self.palette_mode
        self._sheet_cell_index = sheet_cell_index(self.input_dir)
        animation_result = apply_animation_sheet(
            self.project,
            self.tilesets,
            self.input_dir,
            dry_run=self.dry_run,
            backup_func=self._backup_animations,
            prompt_extra_row=(
                self._prompt_extra_animation_row
                if self.on_extra_row == "ask" and self.prompter.interactive
                else None
            ),
        )
        self.result.animation_changed = animation_result.changed
        self.result.palette_changed = (
            self.result.palette_changed or animation_result.palette_changed
        )
        self.result.warnings.extend(animation_result.warnings)
        self.result.written.extend(animation_result.written)
        self.result.touched_tilesets.extend(animation_result.touched_tilesets)
        if animation_result.changed:
            self.tilesets.invalidate_signature_index()
        if not self.dry_run:
            self._animated_signature_index = animated_state_signature_index(
                self.project, self.tilesets, self.input_dir
            )
            for signature, ids in animation_result.extra_metatile_states.items():
                self._animated_signature_index.setdefault(signature, []).extend(ids)

        changes = self.load_changes()
        if not changes:
            if not self.dry_run and (
                animation_result.changed or self._metatiles_cleared
            ):
                for tileset in (self.tilesets.primary, self.tilesets.secondary):
                    self.result.written.extend(tileset.save())
            return self.result

        groups, remaps = self.build_groups(changes)
        for group in groups:
            self.decide_group(group)
        if self.tiny_edit_pixels:
            tiny_changes = [
                change
                for group in groups
                if group.action != "skip"
                for change in group.changes
                if 0 < change.changed_pixels <= self.tiny_edit_pixels
            ]
            if tiny_changes:
                self.result.tiny_edits = len(tiny_changes)
                labels = ", ".join(
                    f"{change.label}({change.changed_pixels}px)"
                    for change in tiny_changes[:12]
                )
                if len(tiny_changes) > 12:
                    labels += f", ... (+{len(tiny_changes) - 12})"
                message = (
                    f"{len(tiny_changes)} tiny edit(s) would create or modify "
                    f"metatile content: {labels}. If they were accidental, fix the "
                    "layer PNG(s) and re-import."
                )
                if self.tiny_edit_policy == "error":
                    raise MapEditorError(
                        message
                        + " The import was aborted to avoid creating unintended "
                        "metatiles; use --tiny-edit-policy allow to override."
                    )
                if self.tiny_edit_policy == "warn":
                    self.result.warnings.append(message)
        self._prepare_replace_plan(groups, remaps)
        self._plan_edited_palettes(changes, groups)
        self.apply(groups, remaps)
        return self.result
