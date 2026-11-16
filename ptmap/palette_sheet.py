"""Export and import editable palette sheets with usage counts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .errors import MapEditorError
from .project import Project, write_bytes_atomic, write_text_atomic
from .tileset import TilesetPair

PALETTE_SHEET_NAME = "palettes.png"
PALETTE_MANIFEST_NAME = "palettes.json"
PALETTE_FORMAT = "poryeasy-palettes"
PALETTE_VERSION = 1
SWATCH = 16


@dataclass
class PaletteApplyResult:
    changed: bool = False
    written: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    touched_tilesets: set[tuple[str, str]] = field(default_factory=set)


def _visible_palettes(pair: TilesetPair):
    settings = pair.project.settings
    entries: list[tuple[int, object, int]] = []
    primary_count = min(settings.num_pals_in_primary, len(pair.primary.palettes), 16)
    for index in range(primary_count):
        entries.append((index, pair.primary, index))
    for local in range(
        min(
            len(pair.secondary.palettes),
            max(0, settings.num_pals_total - settings.num_pals_in_primary),
        )
    ):
        bank = settings.num_pals_in_primary + local
        if bank >= 16:
            break
        entries.append((bank, pair.secondary, pair.secondary.palette_file_index(bank)))
    return entries


def _palette_usage(
    project: Project,
    tileset,
    bank: int,
    cache: dict[tuple[str, str], dict[int, tuple[list[int], int]]],
) -> tuple[list[int], int]:
    key = (tileset.kind, str(tileset.directory))
    if key not in cache:
        counts: dict[int, list[int]] = {}
        metatiles: dict[int, set[int]] = {}
        usage = project.global_metatile_usage().get(key, set())
        for local in usage:
            if not 0 <= local < len(tileset.metatiles):
                continue
            for entry in tileset.metatiles[local]:
                bank_counts = counts.setdefault(entry.palette, [0] * 16)
                pattern = tileset.pattern_at(entry.tile_index)
                for value in pattern:
                    bank_counts[value & 0x0F] += 1
                metatiles.setdefault(entry.palette, set()).add(local)
        cache[key] = {
            palette_bank: (
                bank_counts,
                len(metatiles.get(palette_bank, set())),
            )
            for palette_bank, bank_counts in counts.items()
        }
    return cache[key].get(bank, ([0] * 16, 0))


def export_palette_sheet(project: Project, pair: TilesetPair, directory: Path) -> dict:
    rows: list[dict] = []
    entries = _visible_palettes(pair)
    usage_cache: dict[tuple[str, str], dict[int, tuple[list[int], int]]] = {}
    sheet = Image.new(
        "RGBA", (16 * SWATCH, max(1, len(entries)) * SWATCH), (0, 0, 0, 0)
    )
    for row_index, (bank, tileset, local) in enumerate(entries):
        palette = pair.palette_for(bank)
        counts, used_metatiles = _palette_usage(project, tileset, bank, usage_cache)
        path = tileset.directory / "palettes" / f"{local:02d}.pal"
        rows.append(
            {
                "bank": bank,
                "kind": tileset.kind,
                "tileset": tileset.name,
                "palette_index": local,
                "path": path.relative_to(project.root).as_posix(),
                "colors": [list(color) for color in palette[:16]],
                "used_metatiles": used_metatiles,
                "used_pixels": counts,
                "complete": project.metatile_usage_complete(tileset),
            }
        )
        for slot, color in enumerate(palette[:16]):
            for y in range(SWATCH):
                for x in range(SWATCH):
                    if slot == 0:
                        checker = ((x // 4) + (y // 4)) % 2
                        base = 32 + checker * 16
                        pixel = (base, base, base, 255)
                    else:
                        pixel = (color[0], color[1], color[2], 255)
                    sheet.putpixel((slot * SWATCH + x, row_index * SWATCH + y), pixel)
    sheet_path = directory / PALETTE_SHEET_NAME
    manifest_path = directory / PALETTE_MANIFEST_NAME
    sheet.save(sheet_path, format="PNG")
    manifest = {
        "format": PALETTE_FORMAT,
        "version": PALETTE_VERSION,
        "swatch_size": SWATCH,
        "sheet_sha256": hashlib.sha256(sheet_path.read_bytes()).hexdigest(),
        "rows": rows,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _write_palette(path: Path, colors: list[tuple[int, int, int]]) -> None:
    padded = list(colors[:16])
    padded.extend([(0, 0, 0)] * (16 - len(padded)))
    data = path.read_bytes() if path.is_file() else b""
    if data.startswith(b"JASC-PAL"):
        lines = ["JASC-PAL", "0100", "16"]
        lines.extend(f"{r} {g} {b}" for r, g, b in padded)
        write_text_atomic(path, "\n".join(lines) + "\n")
    else:
        write_bytes_atomic(path, bytes(value for color in padded for value in color))


def apply_palette_sheet(
    project: Project,
    pair: TilesetPair,
    directory: Path,
    *,
    dry_run: bool = False,
    backup_func=None,
    force: bool = False,
) -> PaletteApplyResult:
    result = PaletteApplyResult()
    sheet_path = directory / PALETTE_SHEET_NAME
    manifest_path = directory / PALETTE_MANIFEST_NAME
    if not sheet_path.is_file() or not manifest_path.is_file():
        return result
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MapEditorError(f"Could not read {manifest_path}: {exc}") from exc
    if manifest.get("format") != PALETTE_FORMAT:
        raise MapEditorError(f"{manifest_path} is not a Poryeasy palette sheet.")
    expected = manifest.get("sheet_sha256")
    if expected and hashlib.sha256(sheet_path.read_bytes()).hexdigest() == expected:
        return result
    with Image.open(sheet_path) as opened:
        sheet = opened.convert("RGBA")
    rows = manifest.get("rows", [])
    if sheet.width < 16 * SWATCH or sheet.height < len(rows) * SWATCH:
        raise MapEditorError(f"{sheet_path} is smaller than its manifest expects.")

    updates: dict[str, list[tuple[int, int, int]]] = {}
    for row_index, row in enumerate(rows):
        colors = [tuple(color[:3]) for color in row.get("colors", [])]
        if len(colors) < 16:
            colors.extend([(0, 0, 0)] * (16 - len(colors)))
        changed: list[tuple[int, int, int]] = []
        for slot in range(1, 16):
            pixel = sheet.getpixel(
                (slot * SWATCH + SWATCH // 2, row_index * SWATCH + SWATCH // 2)
            )
            color = (pixel[0], pixel[1], pixel[2])
            if color == colors[slot]:
                continue
            used_counts = list(row.get("used_pixels", []))
            used_counts.extend([0] * (16 - len(used_counts)))
            used = int(used_counts[slot])
            if not row.get("complete", True) and not force:
                raise MapEditorError(
                    f"{row.get('path')}: cannot safely edit palette colours because "
                    "metatile usage could not be fully scanned. Fix the missing layouts "
                    "or pass --force-palette-edits."
                )
            if used:
                result.warnings.append(
                    f"{row.get('path')}: colour slot {slot} is used by {used} tile "
                    "pixel(s) across readable maps."
                )
            colors[slot] = color
            changed.append(color)
        if changed:
            updates[str(row["path"])] = colors

    if not updates:
        return result
    paths = [project.root / path for path in updates]
    if backup_func is not None and not dry_run:
        backup_func(paths)
    for path_str, colors in updates.items():
        path = project.root / path_str
        result.written.append(path)
        if not dry_run:
            _write_palette(path, colors)
    for bank, tileset, local in _visible_palettes(pair):
        for path_str, colors in updates.items():
            expected = (
                (tileset.directory / "palettes" / f"{local:02d}.pal")
                .relative_to(project.root)
                .as_posix()
            )
            if path_str == expected:
                if local < len(tileset.palettes):
                    tileset.palettes[local] = colors
                result.touched_tilesets.add((tileset.name, tileset.kind))
    result.changed = True
    result.warnings.append(
        f"Updated {len(updates)} palette file(s) from {sheet_path.name}."
    )
    return result
