"""Export a full map into editable bottom/middle/top PNG layers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .animation_sheet import (
    ANIMATION_MANIFEST_NAME,
    ANIMATION_SHEET_NAME,
    export_animation_sheet,
)
from .errors import MapEditorError
from .palette_sheet import (
    PALETTE_MANIFEST_NAME,
    PALETTE_SHEET_NAME,
    export_palette_sheet,
)
from .project import Project, Target
from .tileset import METATILE_SIZE


@dataclass(frozen=True)
class ExportPaths:
    directory: Path
    bottom: Path
    middle: Path
    top: Path
    preview: Path
    metadata: Path
    animation_sheet: Path
    animation_manifest: Path
    palette_sheet: Path
    palette_manifest: Path


def default_export_directory(project: Project, target: Target) -> Path:
    return project.root / "porytiles" / "maps" / target.display_name


def export_map(
    project: Project,
    target: Target,
    output_dir: Path | None = None,
    *,
    overwrite: bool = False,
) -> ExportPaths:
    layout = target.layout
    tilesets = project.load_tileset_pair(layout)
    blocks = project.read_blocks(layout)
    bottom_raw, middle_raw, top_raw = tilesets.render_map_layers(layout, blocks)

    width_px = layout.width * METATILE_SIZE
    height_px = layout.height * METATILE_SIZE
    bottom = Image.frombytes("RGBA", (width_px, height_px), bottom_raw)
    middle = Image.frombytes("RGBA", (width_px, height_px), middle_raw)
    top = Image.frombytes("RGBA", (width_px, height_px), top_raw)

    preview = bottom.copy()
    preview.alpha_composite(middle)
    preview.alpha_composite(top)

    directory = (output_dir or default_export_directory(project, target)).resolve()
    paths = ExportPaths(
        directory=directory,
        bottom=directory / "bottom.png",
        middle=directory / "middle.png",
        top=directory / "top.png",
        preview=directory / "preview.png",
        metadata=directory / "map.json",
        animation_sheet=directory / ANIMATION_SHEET_NAME,
        animation_manifest=directory / ANIMATION_MANIFEST_NAME,
        palette_sheet=directory / PALETTE_SHEET_NAME,
        palette_manifest=directory / PALETTE_MANIFEST_NAME,
    )

    if directory.exists() and any(directory.iterdir()) and not overwrite:
        raise MapEditorError(
            f"Output directory already exists and is not empty: {directory}."
            " Pass --overwrite to replace the exported layer images."
        )
    directory.mkdir(parents=True, exist_ok=True)
    legacy_overlay = directory / "animated_overlay.png"
    if legacy_overlay.is_file():
        legacy_overlay.unlink()

    bottom.save(paths.bottom, format="PNG")
    middle.save(paths.middle, format="PNG")
    top.save(paths.top, format="PNG")
    preview.save(paths.preview, format="PNG")

    animation_manifest, _ = export_animation_sheet(project, tilesets, blocks, directory)
    palette_manifest = export_palette_sheet(project, tilesets, directory)

    metadata = {
        "format": "porytiles-map-export",
        "version": 1,
        "map": {
            "id": target.map.id if target.map else None,
            "name": target.map.name if target.map else None,
            "folder": target.map.folder.name if target.map else None,
            "layout_id": layout.id,
            "layout_name": layout.name,
        },
        "width": layout.width,
        "height": layout.height,
        "pixel_width": width_px,
        "pixel_height": height_px,
        "primary_tileset": layout.primary_tileset,
        "secondary_tileset": layout.secondary_tileset,
        "layer_mode": "triple" if tilesets.entries_per_metatile == 12 else "dual",
        "metatiles": [word & project.settings.metatile_id_mask for word in blocks],
        "animation_sheet": ANIMATION_SHEET_NAME,
        "animation_manifest": ANIMATION_MANIFEST_NAME,
        "animation_rows": len(animation_manifest.get("rows", [])),
        "palette_sheet": PALETTE_SHEET_NAME,
        "palette_manifest": PALETTE_MANIFEST_NAME,
        "palette_rows": len(palette_manifest.get("rows", [])),
        "palette_animation_tilesets": tilesets.palette_animation_tilesets(),
    }
    paths.metadata.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return paths
