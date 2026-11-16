"""Create per-map tileset variants when a tileset hits the hardware tile limit."""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .errors import MapEditorError
from .project import Project, Target
from .tileset import TILE_SIZE, TileRef, Tileset, TilesetPair

VARIANT_FORMAT = "poryeasy-tileset-variants"
VARIANT_VERSION = 1


@dataclass
class VariantResult:
    variants: dict[str, Tileset] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    written: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    affected_paths: list[Path] = field(default_factory=list)
    registry_paths: list[Path] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.variants)


def _pattern_hash(pattern: bytes) -> str:
    return hashlib.sha256(pattern).hexdigest()


def _palette_hash(colors: list[tuple[int, int, int]]) -> str:
    return hashlib.sha256(
        bytes(value for color in colors for value in color)
    ).hexdigest()


def _camel(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or "Variant"


def _tile_owner_local(entry: TileRef, tileset: Tileset, total_count: int) -> int | None:
    local = entry.tile_index - tileset.global_tile_base
    if 0 <= local < total_count:
        return local
    return None


def _collect_usage(
    pair: TilesetPair, tileset: Tileset, exclude_layout: str | None = None
) -> dict[int, int]:
    try:
        counts = dict(tileset.project.tile_reference_counts(tileset, exclude_layout))
    except MapEditorError:
        counts = {}
    if counts:
        return counts
    for source in (pair.primary, pair.secondary):
        for metatile in source.metatiles:
            for entry in metatile:
                local = entry.tile_index - tileset.global_tile_base
                if 0 <= local < len(tileset.patterns):
                    counts[local] = counts.get(local, 0) + 1
    return counts


def _map_used_tiles(pair: TilesetPair, tileset: Tileset, blocks: list[int]) -> set[int]:
    used: set[int] = set()
    for word in blocks:
        metatile_id = word & pair.project.settings.metatile_id_mask
        try:
            owner = pair.metatile_owner(metatile_id)
            metatile = owner.metatile_at(metatile_id)
        except MapEditorError:
            continue
        entries = []
        layers = (0, 1, 2) if pair.entries_per_metatile == 12 else (0, 2)
        for layer in layers:
            entries.extend(owner.entries_for_layer(metatile, layer))
        for entry in entries:
            if entry is None:
                continue
            local = entry.tile_index - tileset.global_tile_base
            if 0 <= local < len(tileset.patterns):
                used.add(local)
    return used


def _nearest_kept(pattern: bytes, kept: list[tuple[int, bytes]]) -> int | None:
    if not kept:
        return None
    best_index, _best_pattern = min(
        kept,
        key=lambda item: sum(
            abs(left - right) for left, right in zip(pattern, item[1])
        ),
    )
    return best_index


def _build_variant_tileset(
    project: Project,
    base: Tileset,
    pair: TilesetPair,
    blocks: list[int],
    original_count: int,
    suffix: str,
    layout=None,
    *,
    persist: bool = True,
    force: bool = False,
) -> Tileset:
    max_tiles = base.max_local_tiles
    overflow = len(base.patterns) > max_tiles
    if not overflow and not force:
        raise MapEditorError(f"{base.name} does not exceed its tile limit.")

    if overflow:
        if layout is not None:
            try:
                blocks = list(blocks) + list(project.read_border(layout))
            except MapEditorError:
                pass
        map_used = _map_used_tiles(pair, base, blocks)
        usage = _collect_usage(pair, base, layout.id if layout is not None else None)
        new_used = {
            local
            for local in range(original_count, len(base.patterns))
            if usage.get(local, 0) > 0
        }
        must_keep = map_used | new_used
        if len(must_keep) > max_tiles:
            raise MapEditorError(
                f"{base.name} needs {len(must_keep)} tiles for this map, but the limit is "
                f"{max_tiles}. Reduce the new tile count or split the map further."
            )
        remaining = [local for local in range(original_count) if local not in must_keep]
        remaining.sort(key=lambda local: (-usage.get(local, 0), local))
        keep = set(must_keep)
        keep.update(remaining[: max_tiles - len(keep)])
        kept_originals = sorted(local for local in keep if local < original_count)
        free_slots = [
            local for local in range(max_tiles) if local not in set(kept_originals)
        ]
        old_to_new: dict[int, int] = {local: local for local in kept_originals}
        for local in sorted(local for local in keep if local >= original_count):
            if not free_slots:
                raise MapEditorError(
                    f"{base.name} has no room for new tile {local} in the per-map variant."
                )
            old_to_new[local] = free_slots.pop(0)
        kept_original_patterns = [
            (local, base.patterns[local]) for local in kept_originals
        ]
        for old in range(len(base.patterns)):
            if old in old_to_new:
                continue
            nearest = _nearest_kept(base.patterns[old], kept_original_patterns)
            if nearest is None:
                continue
            old_to_new[old] = nearest

        metatiles: list[list[TileRef]] = []
        for metatile in base.metatiles:
            remapped: list[TileRef] = []
            for entry in metatile:
                local = _tile_owner_local(entry, base, len(base.patterns))
                if local is None or local not in old_to_new:
                    remapped.append(entry)
                    continue
                remapped.append(
                    TileRef(
                        base.global_tile_base + old_to_new[local],
                        entry.palette,
                        entry.hflip,
                        entry.vflip,
                    )
                )
            metatiles.append(remapped)
    else:
        # Forced per-map copy: keep every tile and metatile reference intact.
        keep = set(range(len(base.patterns)))
        old_to_new = {local: local for local in keep}
        metatiles = [list(metatile) for metatile in base.metatiles]

    variant_name = f"{base.name}_{suffix}"
    root = project._tileset_root(base.kind)
    folder = project.tileset_folder_name(variant_name)
    variant_dir = root / folder
    extra = 2
    while variant_dir.exists():
        variant_name = f"{base.name}_{suffix}{extra}"
        folder = project.tileset_folder_name(variant_name)
        variant_dir = root / folder
        extra += 1

    if persist:
        variant_dir.mkdir(parents=True, exist_ok=True)
    palette_dir = base.directory / "palettes"
    if persist and palette_dir.is_dir():
        shutil.copytree(palette_dir, variant_dir / "palettes", dirs_exist_ok=True)

    patterns = [bytes(64)] * max_tiles
    for old, new in old_to_new.items():
        if old in keep:
            patterns[new] = base.patterns[old]
    rows = max(1, (len(patterns) + base.tiles_per_row - 1) // base.tiles_per_row)
    height = max(base.tiles_height, rows * TILE_SIZE)
    image = Image.new("P", (base.tiles_width, height), 0)
    variant = Tileset(
        project,
        variant_name,
        base.kind,
        variant_dir,
        image,
        metatiles,
        [bytes(attribute) for attribute in base.attributes],
        base.attribute_size,
        [list(colors) for colors in base.palettes],
    )
    variant.patterns = patterns
    variant.dirty_tiles = True
    variant.dirty_metadata = True
    variant.dirty_palette_indices.update(range(len(variant.palettes)))
    variant._reserved_tiles.update(
        range(variant.global_tile_base, variant.global_tile_base + len(patterns))
    )

    variants = project._variant_registry(base)
    if overflow:
        kept_base_to_variant = {
            old: new
            for old, new in old_to_new.items()
            if old < original_count and old in keep and old < base.max_local_tiles
        }
    else:
        kept_base_to_variant = {
            local: local for local in range(len(base.patterns))
        }
    entry = {
        "name": variant_name,
        "folder": folder,
        "variant_tile_to_base": {
            str(new): old for old, new in kept_base_to_variant.items()
        },
        "base_tile_to_variant": {
            str(old): new for old, new in kept_base_to_variant.items()
        },
        "base_pattern_sha256": {
            str(old): _pattern_hash(base.patterns[old])
            for old in kept_base_to_variant
            if old < len(base.patterns)
        },
        "base_palette_sha256": [_palette_hash(colors) for colors in base.palettes],
        "created": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
    }
    if persist:
        variants.setdefault("variants", []).append(entry)
        registry_path = project._write_variant_registry(base, variants)
        variant._variant_registry_path = registry_path  # type: ignore[attr-defined]
    return variant


def preview_layout_variants(
    project: Project,
    pair: TilesetPair,
    layout,
    target: Target,
    blocks: list[int],
    original_counts: dict[str, int],
    force_kinds: set[str] | None = None,
) -> dict[str, Tileset]:
    """Builds in-memory variants for validation without touching the project."""
    previews: dict[str, Tileset] = {}
    suffix = _camel(target.display_name)
    force_kinds = force_kinds or set()
    for tileset, kind in ((pair.primary, "primary"), (pair.secondary, "secondary")):
        forced = kind in force_kinds
        if not forced and len(tileset.patterns) <= tileset.max_local_tiles:
            continue
        previews[kind] = _build_variant_tileset(
            project,
            tileset,
            pair,
            blocks,
            original_counts.get(kind, len(tileset.patterns)),
            suffix,
            layout,
            persist=False,
            force=forced,
        )
    return previews


def sync_variants_from_base(project: Project, base: Tileset) -> list[Path]:
    registry = project._variant_registry(base)
    written: list[Path] = []
    changed_registry = False
    for entry in registry.get("variants", []):
        name = entry.get("name")
        if not name:
            continue
        variant = project.load_tileset(name, base.kind)
        variant_changed = False
        mapping = {
            int(variant_local): int(base_local)
            for variant_local, base_local in entry.get(
                "variant_tile_to_base", {}
            ).items()
        }
        hashes = entry.setdefault("base_pattern_sha256", {})
        for variant_local, base_local in mapping.items():
            if base_local >= len(base.patterns) or variant_local >= len(
                variant.patterns
            ):
                continue
            base_pattern = base.patterns[base_local]
            variant_pattern = variant.patterns[variant_local]
            baseline = hashes.get(str(base_local))
            base_hash = _pattern_hash(base_pattern)
            variant_hash = _pattern_hash(variant_pattern)
            if base_hash == baseline:
                continue
            if base_hash == variant_hash:
                hashes[str(base_local)] = base_hash
                changed_registry = True
                continue
            if variant_hash != baseline:
                continue
            variant.patterns[variant_local] = base_pattern
            hashes[str(base_local)] = base_hash
            variant_changed = True
            changed_registry = True
        palette_hashes = entry.setdefault("base_palette_sha256", [])
        if len(palette_hashes) == len(base.palettes):
            for index, colors in enumerate(base.palettes):
                baseline = palette_hashes[index]
                if _palette_hash(colors) == baseline:
                    continue
                if index >= len(variant.palettes):
                    continue
                variant_hash = _palette_hash(variant.palettes[index])
                if variant_hash == _palette_hash(colors):
                    palette_hashes[index] = _palette_hash(colors)
                    changed_registry = True
                    continue
                if variant_hash == baseline:
                    variant.palettes[index] = list(colors)
                    variant.dirty_palette_indices.add(index)
                    palette_hashes[index] = _palette_hash(colors)
                    variant_changed = True
                    changed_registry = True
        if variant_changed:
            variant.dirty_tiles = True
            variant.dirty_metadata = True
            written.extend(variant.save())
    if changed_registry:
        written.append(project._write_variant_registry(base, registry))
    return written


def _truncate_base_tileset(
    base: Tileset,
    usage: dict[int, int] | None = None,
    protected_base_tiles: set[int] | None = None,
) -> list[Path]:
    max_tiles = base.max_local_tiles
    if len(base.patterns) <= max_tiles:
        return []
    usage = usage or {}
    protected_tiles: set[int] = set(protected_base_tiles or ())
    try:
        protected_tiles.update(
            index - base.global_tile_base for index in base.animated_tile_indices()
        )
    except MapEditorError:
        pass
    candidates = list(range(min(max_tiles, len(base.patterns))))
    candidates.sort(
        key=lambda local: (
            0 if local in protected_tiles else 1,
            -usage.get(local, 0),
            local,
        )
    )
    keep = set(candidates[:max_tiles])
    patterns = [bytes(64)] * max_tiles
    for local in keep:
        patterns[local] = base.patterns[local]
    kept_patterns = [(local, base.patterns[local]) for local in sorted(keep)]
    mapping: dict[int, int] = {}
    for local in range(max_tiles, len(base.patterns)):
        nearest = _nearest_kept(base.patterns[local], kept_patterns)
        if nearest is not None:
            mapping[local] = nearest
    for local in range(max_tiles):
        if local in keep:
            continue
        nearest = _nearest_kept(base.patterns[local], kept_patterns)
        if nearest is not None:
            mapping[local] = nearest
    for metatile in base.metatiles:
        for index, entry in enumerate(metatile):
            local = entry.tile_index - base.global_tile_base
            if local in mapping:
                metatile[index] = TileRef(
                    base.global_tile_base + mapping[local],
                    entry.palette,
                    entry.hflip,
                    entry.vflip,
                )
    base.patterns = patterns
    base._reserved_tiles.update(base.global_tile_base + local for local in keep)
    base.dirty_tiles = True
    base.dirty_metadata = True
    return base.save()


def create_layout_variants(
    project: Project,
    pair: TilesetPair,
    layout,
    target: Target,
    blocks: list[int],
    original_counts: dict[str, int],
    force_kinds: set[str] | None = None,
) -> VariantResult:
    result = VariantResult()
    suffix = _camel(target.display_name)
    new_primary = layout.primary_tileset
    new_secondary = layout.secondary_tileset
    protected_by_kind: dict[str, set[int]] = {}
    force_kinds = force_kinds or set()
    for tileset, kind in ((pair.primary, "primary"), (pair.secondary, "secondary")):
        forced = kind in force_kinds
        if not forced and len(tileset.patterns) <= tileset.max_local_tiles:
            continue
        variant = _build_variant_tileset(
            project,
            tileset,
            pair,
            blocks,
            original_counts.get(kind, len(tileset.patterns)),
            suffix,
            layout,
            force=forced,
        )
        result.variants[kind] = variant
        result.names[kind] = variant.name
        registry = project._variant_registry(tileset)
        registry["base_maps"] = sorted(
            {
                map_entry.name
                for candidate in project.layouts.values()
                if candidate.id != layout.id
                and tileset.name
                in (candidate.primary_tileset, candidate.secondary_tileset)
                for map_entry in project.maps_using_layout(candidate.id)
            }
        )
        for entry in registry.get("variants", []):
            if entry.get("name") == variant.name:
                entry["maps"] = [
                    map_entry.name for map_entry in project.maps_using_layout(layout.id)
                ]
                entry["layouts"] = [layout.id]
                protected_by_kind[kind] = {
                    int(base_local)
                    for base_local in entry.get("base_tile_to_variant", {})
                }
                break
        project._write_variant_registry(tileset, registry)
        result.affected_paths.extend(
            [
                variant.directory / name
                for name in ("tiles.png", "metatiles.bin", "metatile_attributes.bin")
            ]
        )
        source_paths = project._register_variant_source(
            tileset, variant, len(tileset.palettes)
        )
        result.affected_paths.extend(source_paths)
        result.written.extend(source_paths)
        registry_path = getattr(variant, "_variant_registry_path", None)
        if registry_path is not None:
            result.affected_paths.append(registry_path)
            result.registry_paths.append(registry_path)
        if forced and len(tileset.patterns) <= tileset.max_local_tiles:
            result.warnings.append(
                f"Created per-map tileset variant {variant.name} for "
                f"{target.display_name} because --force-variant was requested. "
                "The base tileset was left unchanged."
            )
        else:
            result.warnings.append(
                f"{tileset.name} exceeded the tile limit; created {variant.name} for "
                f"{target.display_name}. The original tileset was truncated/remapped as "
                "far as possible. The variant reuses the base tileset's animation "
                "callback; verify that the animated tiles it needs were kept."
            )
        if kind == "primary":
            new_primary = variant.name
        else:
            new_secondary = variant.name

    if not result.variants:
        return result

    for kind, base in (("primary", pair.primary), ("secondary", pair.secondary)):
        if kind in result.variants:
            result.written.extend(
                _truncate_base_tileset(
                    base,
                    _collect_usage(pair, base, layout.id),
                    protected_by_kind.get(kind),
                )
            )

    layout = project.update_layout_tilesets(
        layout.id, primary_tileset=new_primary, secondary_tileset=new_secondary
    )
    result.written.append(project.paths["layouts_json"])
    for kind, variant in result.variants.items():
        registry_path = getattr(variant, "_variant_registry_path", None)
        if registry_path is not None:
            result.written.append(registry_path)
    return result
