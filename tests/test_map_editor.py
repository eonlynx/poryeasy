"""Integration tests for Poryeasy.

Run with::

    python -m unittest discover -s tests -v

The real-project tests need a pokeemerald-expansion checkout. Point
``PORYTILES_TEST_PROJECT`` at it, or keep it as a sibling of the Poryeasy
repository. Those tests are skipped when it cannot be found.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "porytiles_map.py"
sys.path.insert(0, str(SCRIPT.parent))

from ptmap.animations import (
    load_indexed_tile_patterns,
    scan_animation_catalog,
    scan_project_animations,
)
from ptmap.palette_planner import plan_bank_colors
from ptmap.project import Project
from ptmap.tileset import TileRef, TilesetPair
from ptmap.tileset_variants import create_layout_variants, sync_variants_from_base

EXPANSION = Path(
    os.environ.get("PORYTILES_TEST_PROJECT", REPO_ROOT.parent / "pokeemerald-expansion")
)


def copy_expansion(destination: Path) -> Path:
    project = destination / "project"
    for relative in ("data", "include", "src/data/tilesets"):
        source = EXPANSION / relative
        if not source.is_dir():
            raise unittest.SkipTest(f"Missing {source}")
        shutil.copytree(source, project / relative)
    return project


def run_tool(
    *args: str, project: Path, input_text: str | None = None
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(SCRIPT),
        *args,
        "--project-root",
        str(project),
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        input=input_text,
        timeout=300,
        check=False,
    )


def png_patch(
    image_path: Path, source_xy: tuple[int, int], destination_xy: tuple[int, int]
) -> None:
    with Image.open(image_path) as image:
        rgba = image.convert("RGBA")
        sx, sy = source_xy
        dx, dy = destination_xy
        patch = rgba.crop((sx, sy, sx + 16, sy + 16))
        rgba.paste(patch, (dx, dy))
        rgba.save(image_path, format="PNG")


def edit_subtile(
    image_path: Path, block_xy: tuple[int, int], subtile: int, offset: int = 1
) -> None:
    with Image.open(image_path) as image:
        rgba = image.convert("RGBA")
        x = block_xy[0] * 16 + (subtile % 2) * 8
        y = block_xy[1] * 16 + (subtile // 2) * 8
        patch = rgba.crop((x, y, x + 8, y + 8))
        canvas = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        canvas.paste(patch, (offset, 0))
        rgba.paste(canvas, (x, y))
        rgba.save(image_path, format="PNG")


def fill_8x8(image_path: Path, block_xy: tuple[int, int], subtile: int) -> None:
    with Image.open(image_path) as image:
        rgba = image.convert("RGBA")
        x = block_xy[0] * 16 + (subtile % 2) * 8
        y = block_xy[1] * 16 + (subtile // 2) * 8
        color = rgba.getpixel((x + 1, y + 1))
        for dy in range(8):
            for dx in range(8):
                rgba.putpixel((x + dx, y + dy), color)
        rgba.save(image_path, format="PNG")


def read_u16(path: Path) -> list[int]:
    data = path.read_bytes()
    return list(struct.unpack(f"<{len(data) // 2}H", data))


def write_indexed_png(path: Path, width: int, height: int, pixels: bytes) -> None:
    image = Image.frombytes("P", (width, height), pixels)
    palette = bytearray(768)
    for index in range(16):
        palette[index * 3 : index * 3 + 3] = bytes((index * 17,) * 3)
    image.putpalette(bytes(palette))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")


def write_jasc(path: Path, colors: list[tuple[int, int, int]]) -> None:
    lines = ["JASC-PAL", "0100", "16"]
    lines.extend(f"{r} {g} {b}" for r, g, b in colors)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def make_synthetic_triple_project(root: Path) -> None:
    (root / "include").mkdir(parents=True)
    (root / "include" / "fieldmap.h").write_text(
        "#define NUM_TILES_IN_PRIMARY 512\n"
        "#define NUM_TILES_TOTAL 1024\n"
        "#define NUM_METATILES_IN_PRIMARY 512\n"
        "#define NUM_METATILES_TOTAL 1024\n"
        "#define NUM_PALS_IN_PRIMARY 6\n"
        "#define NUM_PALS_TOTAL 13\n"
        "#define NUM_TILES_PER_METATILE 12\n"
        "#define MAPGRID_METATILE_ID_MASK 0x03FF\n"
        "#define MAPGRID_COLLISION_MASK 0x0C00\n"
        "#define MAPGRID_ELEVATION_MASK 0xF000\n",
        encoding="ascii",
    )
    behaviors = root / "include" / "constants" / "metatile_behaviors.h"
    behaviors.parent.mkdir(parents=True, exist_ok=True)
    behaviors.write_text(
        "#ifndef GUARD_METATILE_BEHAVIORS_H\n"
        "#define GUARD_METATILE_BEHAVIORS_H\n"
        "enum {\n"
        "    MB_NORMAL,\n"
        "    MB_TALL_GRASS,\n"
        "    MB_LONG_GRASS,\n"
        "};\n"
        "#endif\n",
        encoding="ascii",
    )
    (root / "data" / "layouts" / "Test").mkdir(parents=True)
    layouts = {
        "layouts_table_label": "gLayouts",
        "layouts": [
            {
                "id": "LAYOUT_TEST",
                "name": "Test_Layout",
                "width": 4,
                "height": 4,
                "primary_tileset": "gTileset_TestPrimary",
                "secondary_tileset": "gTileset_TestSecondary",
                "border_filepath": "data/layouts/Test/border.bin",
                "blockdata_filepath": "data/layouts/Test/map.bin",
                "layout_version": "emerald",
            }
        ],
    }
    (root / "data" / "layouts" / "layouts.json").write_text(
        json.dumps(layouts), encoding="utf-8"
    )
    blocks = [0] * 16
    blocks[1 * 4 + 1] = 1
    blocks[0 * 4 + 1] = 2
    (root / "data" / "layouts" / "Test" / "map.bin").write_bytes(
        struct.pack("<16H", *blocks)
    )
    (root / "data" / "layouts" / "Test" / "border.bin").write_bytes(
        struct.pack("<4H", 0, 0, 0, 0)
    )
    map_dir = root / "data" / "maps" / "TestMap"
    map_dir.mkdir(parents=True)
    (map_dir / "map.json").write_text(
        json.dumps({"id": "MAP_TEST", "name": "TestMap", "layout": "LAYOUT_TEST"}),
        encoding="utf-8",
    )
    (root / "src").mkdir(parents=True)
    anim_dir = (
        root / "data" / "tilesets" / "primary" / "test_primary" / "anim" / "water"
    )
    anim_dir.mkdir(parents=True, exist_ok=True)
    write_indexed_png(anim_dir / "0.png", 8, 8, bytes([1] * 64))
    write_indexed_png(anim_dir / "1.png", 8, 8, bytes([2] * 64))
    (root / "src" / "tileset_anims.c").write_text(
        """const u16 gTilesetAnims_TestPrimary_Water_Frame0[] =
    INCGFX_U16("data/tilesets/primary/test_primary/anim/water/0.png", ".4bpp");
const u16 gTilesetAnims_TestPrimary_Water_Frame1[] =
    INCGFX_U16("data/tilesets/primary/test_primary/anim/water/1.png", ".4bpp");
const u16 *const gTilesetAnims_TestPrimary_Water[] = {
    gTilesetAnims_TestPrimary_Water_Frame0,
    gTilesetAnims_TestPrimary_Water_Frame1,
};

static void QueueAnimTiles_TestPrimary_Water(u16 timer)
{
    AppendTilesetAnimToBuffer(gTilesetAnims_TestPrimary_Water[timer], (u16 *)(BG_VRAM + TILE_OFFSET_4BPP(1)), 1 * TILE_SIZE_4BPP);
}

void InitTilesetAnimations(void)
{
}

void InitTilesetAnim_TestPrimary(void)
{
    sPrimaryTilesetAnimCounter = 0;
    sPrimaryTilesetAnimCounterMax = 16;
    sPrimaryTilesetAnimCallback = TilesetAnim_TestPrimary;
}

static void TilesetAnim_TestPrimary(u16 timer)
{
    if (timer % 16 == 0)
        QueueAnimTiles_TestPrimary_Water(timer / 16);
}
""",
        encoding="ascii",
    )
    (root / "include" / "tileset_anims.h").write_text(
        "#ifndef GUARD_TILESET_ANIMS_H\n"
        "#define GUARD_TILESET_ANIMS_H\n"
        "void InitTilesetAnim_TestPrimary(void);\n"
        "#endif\n",
        encoding="ascii",
    )
    headers_dir = root / "src" / "data" / "tilesets"
    headers_dir.mkdir(parents=True, exist_ok=True)
    (headers_dir / "headers.h").write_text(
        "const struct Tileset gTileset_TestPrimary = {\n"
        "    .isCompressed = FALSE,\n"
        "    .isSecondary = FALSE,\n"
        "    .callback = InitTilesetAnim_TestPrimary,\n"
        "};\n\n"
        "const struct Tileset gTileset_TestSecondary = {\n"
        "    .isCompressed = FALSE,\n"
        "    .isSecondary = TRUE,\n"
        "};\n",
        encoding="ascii",
    )
    (headers_dir / "metatiles.h").write_text("", encoding="ascii")
    (root / "include" / "tilesets.h").write_text(
        "#ifndef GUARD_TILESETS_H\n#define GUARD_TILESETS_H\n#endif\n",
        encoding="ascii",
    )
    (root / "src" / "graphics.c").write_text('#include "global.h"\n', encoding="ascii")

    def make_tileset(
        kind: str,
        folder: str,
        patterns: list[bytes],
        metatiles: list[list[int]],
        attrs: list[int],
    ):
        directory = root / "data" / "tilesets" / kind / folder
        directory.mkdir(parents=True, exist_ok=True)
        width = 128
        tiles_per_row = width // 8
        rows = max(1, (len(patterns) + tiles_per_row - 1) // tiles_per_row)
        pixels = bytearray(width * rows * 8)
        for index, pattern in enumerate(patterns):
            tx = index % tiles_per_row
            ty = index // tiles_per_row
            for row in range(8):
                offset = (ty * 8 + row) * width + tx * 8
                pixels[offset : offset + 8] = pattern[row * 8 : (row + 1) * 8]
        write_indexed_png(directory / "tiles.png", width, rows * 8, bytes(pixels))
        words = [word for metatile in metatiles for word in metatile]
        (directory / "metatiles.bin").write_bytes(
            struct.pack(f"<{len(words)}H", *words)
        )
        (directory / "metatile_attributes.bin").write_bytes(
            struct.pack(f"<{len(attrs)}H", *attrs)
        )
        palette = [(0, 0, 0)] * 16
        palette[1] = (255, 0, 0)
        palette[2] = (0, 0, 255)
        palette[3] = (0, 255, 0)
        for index in range(16):
            write_jasc(directory / "palettes" / f"{index:02d}.pal", palette)

    transparent = bytes(64)
    red = bytes([1] * 64)
    blue = bytes(2 if (x + y) % 2 else 1 for y in range(8) for x in range(8))
    green = bytes([3] * 64)
    primary_metatiles = [
        [0] * 12,
        [1 | (1 << 12)] * 4 + [2 | (2 << 12)] * 4 + [0] * 4,
        [1 | (1 << 12)] * 4 + [0] * 8,
    ]
    secondary_metatiles = [[0] * 12]
    make_tileset(
        "primary",
        "test_primary",
        [transparent, red, blue, green],
        primary_metatiles,
        [0, 0, 0],
    )
    make_tileset(
        "secondary", "test_secondary", [transparent, green], secondary_metatiles, [0]
    )


def make_synthetic_door_project(root: Path) -> None:
    make_synthetic_triple_project(root)

    secondary = root / "data" / "tilesets" / "secondary" / "test_secondary"
    (secondary / "metatiles.bin").write_bytes(struct.pack("<24H", *([0] * 24)))
    (secondary / "metatile_attributes.bin").write_bytes(struct.pack("<2H", 0, 0))

    layout_bin = root / "data" / "layouts" / "Test" / "map.bin"
    blocks = read_u16(layout_bin)
    blocks[3 * 4 + 2] = 0x201
    layout_bin.write_bytes(struct.pack(f"<{len(blocks)}H", *blocks))

    labels = root / "include" / "constants" / "metatile_labels.h"
    labels.parent.mkdir(parents=True, exist_ok=True)
    labels.write_text(
        "#ifndef GUARD_METATILE_LABELS_H\n"
        "#define GUARD_METATILE_LABELS_H\n"
        "#define METATILE_TestSecondary_Door_Test 0x201\n"
        "#endif\n",
        encoding="ascii",
    )

    pixels = bytearray(32 * 96)
    for tile in range(48):
        tx = tile % 4
        ty = tile // 4
        for y in range(8):
            for x in range(8):
                pixels[(ty * 8 + y) * 32 + tx * 8 + x] = 1 + ((tile + x + y) % 3)
    write_indexed_png(
        root / "graphics" / "door_anims" / "test_door.png", 32, 96, bytes(pixels)
    )

    (root / "src" / "field_door.c").write_text(
        """
enum DoorSize
{
    DOOR_SIZE_1x1,
    DOOR_SIZE_1x2,
    DOOR_SIZE_2x2_LEFT,
    DOOR_SIZE_2x2_RIGHT,
    DOOR_SIZE_COUNT,
};

static const u8 sDoorAnimTiles_TestDoor[] =
    INCGFX_U8("graphics/door_anims/test_door.png", ".4bpp", "-mwidth 2 -mheight 4");
static const u8 sDoorAnimPalettes_TestDoor[] =
    {1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1};

static const struct DoorAnimFrame sDoorAnimFrames_Open2x2[] =
{
    {4, -1},
    {4, 0 * TILE_SIZE_4BPP},
    {4, 16 * TILE_SIZE_4BPP},
    {4, 32 * TILE_SIZE_4BPP},
    {},
};

static const struct DoorAnimFrame sDoorAnimFrames_Close2x2[] =
{
    {4, 32 * TILE_SIZE_4BPP},
    {4, 16 * TILE_SIZE_4BPP},
    {4, 0 * TILE_SIZE_4BPP},
    {4, -1},
    {},
};

static const struct DoorSizeInfo sDoorSizeInfo[DOOR_SIZE_COUNT] =
{
    [DOOR_SIZE_1x1] =
    {
        .openAnimFrames = sDoorAnimFrames_Open2x2,
        .closeAnimFrames = sDoorAnimFrames_Close2x2,
    },
    [DOOR_SIZE_1x2] =
    {
        .openAnimFrames = sDoorAnimFrames_Open2x2,
        .closeAnimFrames = sDoorAnimFrames_Close2x2,
    },
    [DOOR_SIZE_2x2_LEFT] =
    {
        .openAnimFrames = sDoorAnimFrames_Open2x2,
        .closeAnimFrames = sDoorAnimFrames_Close2x2,
    },
    [DOOR_SIZE_2x2_RIGHT] =
    {
        .openAnimFrames = sDoorAnimFrames_Open2x2,
        .closeAnimFrames = sDoorAnimFrames_Close2x2,
    },
};

static const struct DoorGraphics sDoorAnimGraphicsTable[] = {
    {
        .metatileNum = METATILE_TestSecondary_Door_Test,
        .tileset = &gTileset_TestSecondary,
        .sound = DOOR_SOUND_NORMAL,
        .size = DOOR_SIZE_2x2_LEFT,
        .tiles = sDoorAnimTiles_TestDoor,
        .palettes = sDoorAnimPalettes_TestDoor,
    },
    {},
};
""",
        encoding="ascii",
    )


def make_synthetic_1x2_door_project(root: Path) -> None:
    make_synthetic_triple_project(root)

    secondary = root / "data" / "tilesets" / "secondary" / "test_secondary"
    (secondary / "metatiles.bin").write_bytes(struct.pack("<24H", *([0] * 24)))
    (secondary / "metatile_attributes.bin").write_bytes(struct.pack("<2H", 0, 0))
    layout_bin = root / "data" / "layouts" / "Test" / "map.bin"
    blocks = read_u16(layout_bin)
    blocks[3 * 4 + 2] = 0x201
    layout_bin.write_bytes(struct.pack(f"<{len(blocks)}H", *blocks))

    labels = root / "include" / "constants" / "metatile_labels.h"
    labels.parent.mkdir(parents=True, exist_ok=True)
    labels.write_text(
        "#ifndef GUARD_METATILE_LABELS_H\n"
        "#define GUARD_METATILE_LABELS_H\n"
        "#define METATILE_TestSecondary_Door_Small 0x201\n"
        "#endif\n",
        encoding="ascii",
    )
    pixels = bytearray(16 * 96)
    for tile in range(24):
        tx = tile % 2
        ty = tile // 2
        for y in range(8):
            for x in range(8):
                pixels[(ty * 8 + y) * 16 + tx * 8 + x] = 1 + ((tile + x + y) % 3)
    write_indexed_png(
        root / "graphics" / "door_anims" / "small_door.png", 16, 96, bytes(pixels)
    )
    (root / "src" / "field_door.c").write_text(
        """
enum DoorSize
{
    DOOR_SIZE_1x1,
    DOOR_SIZE_1x2,
    DOOR_SIZE_2x2_LEFT,
    DOOR_SIZE_2x2_RIGHT,
    DOOR_SIZE_COUNT,
};

static const u8 sDoorAnimTiles_SmallDoor[] =
    INCGFX_U8("graphics/door_anims/small_door.png", ".4bpp");
static const u8 sDoorAnimPalettes_SmallDoor[] =
    {1, 1, 1, 1, 1, 1, 1, 1};

static const struct DoorAnimFrame sDoorAnimFrames_Open1x2[] =
{
    {4, -1},
    {4, 0 * TILE_SIZE_4BPP},
    {4, 8 * TILE_SIZE_4BPP},
    {4, 16 * TILE_SIZE_4BPP},
    {},
};

static const struct DoorAnimFrame sDoorAnimFrames_Close1x2[] =
{
    {4, 16 * TILE_SIZE_4BPP},
    {4, 8 * TILE_SIZE_4BPP},
    {4, 0 * TILE_SIZE_4BPP},
    {4, -1},
    {},
};

static const struct DoorSizeInfo sDoorSizeInfo[DOOR_SIZE_COUNT] =
{
    [DOOR_SIZE_1x1] =
    {
        .openAnimFrames = sDoorAnimFrames_Open1x2,
        .closeAnimFrames = sDoorAnimFrames_Close1x2,
    },
    [DOOR_SIZE_1x2] =
    {
        .openAnimFrames = sDoorAnimFrames_Open1x2,
        .closeAnimFrames = sDoorAnimFrames_Close1x2,
    },
    [DOOR_SIZE_2x2_LEFT] =
    {
        .openAnimFrames = sDoorAnimFrames_Open1x2,
        .closeAnimFrames = sDoorAnimFrames_Close1x2,
    },
    [DOOR_SIZE_2x2_RIGHT] =
    {
        .openAnimFrames = sDoorAnimFrames_Open1x2,
        .closeAnimFrames = sDoorAnimFrames_Close1x2,
    },
};

static const struct DoorGraphics sDoorAnimGraphicsTable[] = {
    {
        .metatileNum = METATILE_TestSecondary_Door_Small,
        .tileset = &gTileset_TestSecondary,
        .sound = DOOR_SOUND_NORMAL,
        .size = DOOR_SIZE_1x2,
        .tiles = sDoorAnimTiles_SmallDoor,
        .palettes = sDoorAnimPalettes_SmallDoor,
    },
    {},
};
""",
        encoding="ascii",
    )


class SyntheticTripleLayerTests(unittest.TestCase):
    def test_export_and_import_triple_layers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-triple-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            for name in ("bottom", "middle", "top", "preview"):
                self.assertTrue((export_dir / f"{name}.png").is_file())
            with Image.open(export_dir / "bottom.png") as bottom:
                self.assertEqual(bottom.size, (64, 64))
            with Image.open(export_dir / "middle.png") as middle:
                self.assertEqual(middle.getpixel((25, 24)), (0, 0, 255, 255))

            # Edit the middle layer and add a new secondary metatile.
            with Image.open(export_dir / "middle.png") as image:
                rgba = image.convert("RGBA")
                for y in range(16, 32):
                    for x in range(16, 24):
                        rgba.putpixel((x, y), (0, 255, 0, 255))
                rgba.save(export_dir / "middle.png", format="PNG")

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "add-secondary",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("added metatiles:      1", result.stdout)
            blocks = read_u16(project / "data/layouts/Test/map.bin")
            self.assertEqual(blocks[5] & 0x3FF, 513)
            metatiles = read_u16(
                project / "data/tilesets/secondary/test_secondary/metatiles.bin"
            )
            self.assertEqual(len(metatiles), 24)
            self.assertEqual(len(metatiles) // 12, 2)

    def test_moving_animated_block_preserves_animation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-anim-move-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            for name in ("bottom", "middle", "top"):
                png_patch(
                    export_dir / f"{name}.png",
                    (1 * 16, 1 * 16),
                    (2 * 16, 1 * 16),
                )
                png_patch(
                    export_dir / f"{name}.png",
                    (0, 0),
                    (1 * 16, 1 * 16),
                )
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("auto-remapped:        2", result.stdout)
            blocks = read_u16(project / "data/layouts/Test/map.bin")
            self.assertEqual(blocks[1 * 4 + 1] & 0x3FF, 0)
            self.assertEqual(blocks[1 * 4 + 2] & 0x3FF, 1)

            second = Path(temp) / "export2"
            result = run_tool(
                "export", "TestMap", "--output", str(second), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            metadata = json.loads((second / "map.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata.get("animation_rows"), 2)
            sheet_manifest = json.loads(
                (second / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                any(
                    row["kind"] == "metatile" and row["metatile_id"] == 1
                    for row in sheet_manifest["rows"]
                )
            )

    def test_edit_animation_sheet_updates_frames_and_static_tile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-anim-edit-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            row_index = next(
                index
                for index, row in enumerate(manifest["rows"])
                if row["kind"] == "metatile" and row["metatile_id"] == 1
            )
            with Image.open(export_dir / "animated_metatiles.png") as opened:
                sheet = opened.convert("RGBA")
            for state in range(2):
                for y in range(16):
                    for x in range(16):
                        sheet.putpixel(
                            (state * 16 + x, row_index * 16 + y), (0, 255, 0, 255)
                        )
            sheet.save(export_dir / "animated_metatiles.png", format="PNG")

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("animation sheet:      changed", result.stdout)
            anim_dir = project / "data/tilesets/primary/test_primary/anim/water"
            for frame in ("0.png", "1.png"):
                with Image.open(anim_dir / frame) as image:
                    self.assertEqual(image.convert("P").tobytes()[0], 3)
            with Image.open(
                project / "data/tilesets/primary/test_primary/tiles.png"
            ) as tiles:
                self.assertEqual(tiles.convert("P").tobytes()[8], 3)

            second = Path(temp) / "export2"
            result = run_tool(
                "export", "TestMap", "--output", str(second), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            with Image.open(second / "bottom.png") as bottom:
                self.assertEqual(
                    bottom.convert("RGBA").getpixel((16, 16)), (0, 255, 0, 255)
                )

    def test_add_animation_state_appends_frame_and_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-anim-state-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            row_index = next(
                index
                for index, row in enumerate(manifest["rows"])
                if row["kind"] == "metatile" and row["metatile_id"] == 1
            )
            with Image.open(export_dir / "animated_metatiles.png") as opened:
                sheet = opened.convert("RGBA")
            expanded = Image.new("RGBA", (sheet.width + 16, sheet.height), (0, 0, 0, 0))
            expanded.paste(sheet, (0, 0))
            for y in range(16):
                for x in range(16):
                    expanded.putpixel(
                        (2 * 16 + x, row_index * 16 + y), (0, 255, 0, 255)
                    )
            expanded.save(export_dir / "animated_metatiles.png", format="PNG")

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("added 1 new animation", result.stdout)
            new_frame = project / "data/tilesets/primary/test_primary/anim/water/2.png"
            self.assertTrue(new_frame.is_file())
            with Image.open(new_frame) as image:
                self.assertEqual(image.convert("P").tobytes()[0], 3)
            source = (project / "src/tileset_anims.c").read_text(encoding="utf-8")
            self.assertIn("gTilesetAnims_TestPrimary_Water_Frame2", source)

            second = Path(temp) / "export2"
            result = run_tool(
                "export", "TestMap", "--output", str(second), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            refreshed = json.loads(
                (second / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            row = next(
                row
                for row in refreshed["rows"]
                if row["kind"] == "metatile" and row["metatile_id"] == 1
            )
            self.assertEqual(row["states"], 3)

    def test_map_cell_matching_animation_state_is_identified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-anim-state-match-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            row_index = next(
                index
                for index, row in enumerate(manifest["rows"])
                if row["kind"] == "metatile" and row["metatile_id"] == 2
            )
            with Image.open(export_dir / "animated_metatiles.png") as opened:
                state = opened.convert("RGBA").crop(
                    (16, row_index * 16, 32, (row_index + 1) * 16)
                )
            for name in ("bottom", "middle", "top"):
                path = export_dir / f"{name}.png"
                with Image.open(path) as opened:
                    image = opened.convert("RGBA")
                image.paste((0, 0, 0, 0), (2 * 16, 2 * 16, 3 * 16, 3 * 16))
                if name == "bottom":
                    image.paste(state, (2 * 16, 2 * 16))
                image.save(path, format="PNG")

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            blocks = read_u16(project / "data/layouts/Test/map.bin")
            self.assertEqual(blocks[2 * 4 + 2] & 0x3FF, 2)

    def test_extra_animation_row_prompts_and_creates_metatile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-anim-extra-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            rows = len(manifest["rows"])
            columns = manifest["columns"]
            with Image.open(export_dir / "animated_metatiles.png") as opened:
                sheet = opened.convert("RGBA")
            expanded = Image.new(
                "RGBA",
                (max(columns, 2) * 16, (rows + 1) * 16),
                (0, 0, 0, 0),
            )
            expanded.paste(sheet, (0, 0))
            for y in range(16):
                for x in range(16):
                    expanded.putpixel((x, rows * 16 + y), (0, 255, 0, 255))
                    expanded.putpixel((16 + x, rows * 16 + y), (0, 0, 255, 255))
            expanded.save(export_dir / "animated_metatiles.png", format="PNG")
            before = len(
                read_u16(
                    project / "data/tilesets/secondary/test_secondary/metatiles.bin"
                )
            )
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                input_text="m\n1\n1\nMB_TALL_GRASS\n",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("Created new animated metatile", result.stdout)
            after = len(
                read_u16(
                    project / "data/tilesets/secondary/test_secondary/metatiles.bin"
                )
            )
            self.assertGreater(after, before)
            metatile_words = read_u16(
                project / "data/tilesets/secondary/test_secondary/metatiles.bin"
            )
            self.assertEqual(metatile_words[-12] & 0x3FF, 1)
            attributes = read_u16(
                project
                / "data/tilesets/secondary/test_secondary/metatile_attributes.bin"
            )
            self.assertEqual(attributes[-1] & 0xFF, 1)

    def test_replace_assigns_freed_slot_to_most_used_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-replace-usage-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            bottom = export_dir / "bottom.png"
            secondary_metatiles = (
                project
                / "data"
                / "tilesets"
                / "secondary"
                / "test_secondary"
                / "metatiles.bin"
            )
            secondary_before = read_u16(secondary_metatiles)[:12]
            with Image.open(bottom) as opened:
                image = opened.convert("RGBA")

            def paint(cell, pattern):
                for y in range(16):
                    for x in range(16):
                        image.putpixel(
                            (cell[0] * 16 + x, cell[1] * 16 + y),
                            pattern(x, y),
                        )

            paint(
                (1, 0),
                lambda x, y: (255, 0, 0, 255) if (x + y) % 2 else (0, 0, 255, 255),
            )
            paint(
                (2, 2),
                lambda x, y: (255, 0, 0, 255) if (x + y) % 2 else (0, 0, 255, 255),
            )
            paint(
                (3, 2),
                lambda x, y: (0, 255, 0, 255) if (x * 2 + y) % 3 else (255, 0, 0, 255),
            )
            image.save(bottom, format="PNG")
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "replace",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            blocks = read_u16(project / "data/layouts/Test/map.bin")
            a1 = blocks[0 * 4 + 1] & 0x3FF
            a2 = blocks[2 * 4 + 2] & 0x3FF
            b = blocks[2 * 4 + 3] & 0x3FF
            self.assertEqual(a1, a2)
            self.assertEqual(a1, 2)
            self.assertNotEqual(b, 2)
            self.assertEqual(read_u16(secondary_metatiles)[:12], secondary_before)

    def test_animated_tile_edit_remaps_to_animated_metatile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-anim-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue((export_dir / "animated_metatiles.png").is_file())
            sheet_manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                any(
                    row["kind"] == "metatile" and row["metatile_id"] == 1
                    for row in sheet_manifest["rows"]
                )
            )
            with Image.open(export_dir / "bottom.png") as opened:
                image = opened.convert("RGBA")
            for dy in range(8):
                for dx in range(8):
                    image.putpixel(
                        (16 + dx, 16 + dy),
                        (255, 0, 255, 255) if (dx + dy) % 2 else (0, 0, 0, 0),
                    )
            image.save(export_dir / "bottom.png", format="PNG")
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "modify-tiles",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("auto-remapped:        1", result.stdout)
            self.assertIn("modified metatiles:   0", result.stdout)
            self.assertIn("added tiles:          0", result.stdout)


@unittest.skipUnless(
    EXPANSION.is_dir(), f"pokeemerald-expansion not found at {EXPANSION}"
)
class ExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="ptmap-expansion-")
        self.addCleanup(self._temp.cleanup)
        self.project = copy_expansion(Path(self._temp.name))
        self.export_dir = Path(self._temp.name) / "export"
        result = run_tool(
            "export",
            "LittlerootTown",
            "--output",
            str(self.export_dir),
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_export_and_noop_import(self) -> None:
        with Image.open(self.export_dir / "bottom.png") as bottom:
            self.assertEqual(bottom.size, (320, 320))
        self.assertTrue((self.export_dir / "animated_metatiles.png").is_file())
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--on-new",
            "skip",
            "--on-remove",
            "keep",
            "--dry-run",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("No changes detected", result.stdout)

        usage = run_tool(
            "usages",
            "LittlerootTown",
            "--metatile",
            "529",
            project=self.project,
        )
        self.assertEqual(usage.returncode, 0, usage.stderr + usage.stdout)
        self.assertIn("usage(s)", usage.stdout)
        self.assertIn("scan complete:", usage.stdout)

    def test_auto_remap_to_existing_metatile(self) -> None:
        for name in ("bottom", "middle", "top"):
            png_patch(
                self.export_dir / f"{name}.png", (6 * 16, 5 * 16), (5 * 16, 5 * 16)
            )
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--on-new",
            "skip",
            "--on-remove",
            "keep",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("auto-remapped:        1", result.stdout)
        blocks = read_u16(self.project / "data/layouts/LittlerootTown/map.bin")
        self.assertEqual(blocks[5 * 20 + 5] & 0x3FF, 530)

    def test_modify_original_metatile(self) -> None:
        before_map = (self.project / "data/layouts/LittlerootTown/map.bin").read_bytes()
        edit_subtile(self.export_dir / "bottom.png", (5, 5), 0)
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--on-new",
            "modify-metatile",
            "--on-remove",
            "keep",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("modified metatiles:   1", result.stdout)
        self.assertEqual(
            (self.project / "data/layouts/LittlerootTown/map.bin").read_bytes(),
            before_map,
        )
        second = Path(self._temp.name) / "export2"
        result = run_tool(
            "export",
            "LittlerootTown",
            "--output",
            str(second),
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        with (
            Image.open(self.export_dir / "bottom.png") as edited,
            Image.open(second / "bottom.png") as rendered,
        ):
            box = (5 * 16, 5 * 16, 6 * 16, 6 * 16)
            self.assertEqual(
                edited.convert("RGBA").crop(box).tobytes(),
                rendered.convert("RGBA").crop(box).tobytes(),
            )

    def test_add_secondary_metatile_and_tile(self) -> None:
        before = (
            len(
                read_u16(
                    self.project / "data/tilesets/secondary/petalburg/metatiles.bin"
                )
            )
            // 8
        )
        fill_8x8(self.export_dir / "bottom.png", (10, 15), 0)
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--on-new",
            "add-secondary",
            "--on-remove",
            "keep",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("added metatiles:      1", result.stdout)
        after = (
            len(
                read_u16(
                    self.project / "data/tilesets/secondary/petalburg/metatiles.bin"
                )
            )
            // 8
        )
        self.assertEqual(after, before + 1)
        blocks = read_u16(self.project / "data/layouts/LittlerootTown/map.bin")
        self.assertEqual(blocks[15 * 20 + 10] & 0x3FF, 512 + before)
        graphics = (self.project / "src/data/tilesets/graphics.h").read_text(
            encoding="utf-8"
        )
        self.assertIn("-num_tiles 161", graphics)

    def test_modify_underlying_tiles(self) -> None:
        before_metatiles = (
            self.project / "data/tilesets/secondary/petalburg/metatiles.bin"
        ).read_bytes()
        before_map = (self.project / "data/layouts/LittlerootTown/map.bin").read_bytes()
        edit_subtile(self.export_dir / "bottom.png", (5, 5), 0)
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--on-new",
            "modify-tiles",
            "--on-remove",
            "keep",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("modified metatiles:   0", result.stdout)
        self.assertEqual(
            (
                self.project / "data/tilesets/secondary/petalburg/metatiles.bin"
            ).read_bytes(),
            before_metatiles,
        )
        self.assertEqual(
            (self.project / "data/layouts/LittlerootTown/map.bin").read_bytes(),
            before_map,
        )

    def test_clear_unused_metatile(self) -> None:
        # Make metatile 0x200 uniquely referenced by LittlerootTown (5,5).
        layouts = json.loads(
            (self.project / "data/layouts/layouts.json").read_text(encoding="utf-8")
        )["layouts"]
        for layout in layouts:
            path = self.project / layout["blockdata_filepath"]
            if not path.is_file():
                continue
            values = read_u16(path)
            for index, word in enumerate(values):
                if (word & 0x3FF) == 512:
                    values[index] = (word & ~0x3FF) | 1
            path.write_bytes(struct.pack(f"<{len(values)}H", *values))
        littleroot = self.project / "data/layouts/LittlerootTown/map.bin"
        values = read_u16(littleroot)
        values[5 * 20 + 5] = (values[5 * 20 + 5] & ~0x3FF) | 512
        littleroot.write_bytes(struct.pack(f"<{len(values)}H", *values))

        result = run_tool(
            "export",
            "LittlerootTown",
            "--output",
            str(self.export_dir),
            "--overwrite",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for name in ("bottom", "middle", "top"):
            png_patch(
                self.export_dir / f"{name}.png", (6 * 16, 5 * 16), (5 * 16, 5 * 16)
            )
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--on-new",
            "skip",
            "--on-remove",
            "clear",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("cleared metatiles:    1", result.stdout)
        metatiles = read_u16(
            self.project / "data/tilesets/secondary/petalburg/metatiles.bin"
        )
        self.assertEqual(metatiles[:8], [0] * 8)

    def test_do_not_clear_metatile_still_used(self) -> None:
        # Metatile 529 is used by many other maps. Replacing one occurrence must
        # not make it eligible for deletion.
        metatiles_path = (
            self.project / "data/tilesets/secondary/petalburg/metatiles.bin"
        )
        before_bytes = metatiles_path.read_bytes()
        for name in ("bottom", "middle", "top"):
            png_patch(
                self.export_dir / f"{name}.png", (6 * 16, 5 * 16), (5 * 16, 5 * 16)
            )
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--on-new",
            "skip",
            "--on-remove",
            "clear",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("cleared metatiles", result.stdout)
        self.assertEqual(metatiles_path.read_bytes(), before_bytes)

    def test_refuse_clear_when_usage_scan_is_incomplete(self) -> None:
        # OldaleTown uses gTileset_General + gTileset_Petalburg. If its block
        # data is missing, usage for those tilesets cannot be trusted.
        (self.project / "data/layouts/OldaleTown/map.bin").unlink()

        layouts = json.loads(
            (self.project / "data/layouts/layouts.json").read_text(encoding="utf-8")
        )["layouts"]
        for layout in layouts:
            path = self.project / layout["blockdata_filepath"]
            if not path.is_file():
                continue
            values = read_u16(path)
            for index, word in enumerate(values):
                if (word & 0x3FF) == 512:
                    values[index] = (word & ~0x3FF) | 1
            path.write_bytes(struct.pack(f"<{len(values)}H", *values))
        littleroot = self.project / "data/layouts/LittlerootTown/map.bin"
        values = read_u16(littleroot)
        values[5 * 20 + 5] = (values[5 * 20 + 5] & ~0x3FF) | 512
        littleroot.write_bytes(struct.pack(f"<{len(values)}H", *values))

        result = run_tool(
            "export",
            "LittlerootTown",
            "--output",
            str(self.export_dir),
            "--overwrite",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for name in ("bottom", "middle", "top"):
            png_patch(
                self.export_dir / f"{name}.png", (6 * 16, 5 * 16), (5 * 16, 5 * 16)
            )
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--on-new",
            "skip",
            "--on-remove",
            "clear",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("cleared metatiles", result.stdout)
        self.assertIn("Refusing to clear", result.stdout)
        self.assertIn("OLDALE_TOWN", result.stdout)

    def test_resize_with_offset_and_event_shift(self) -> None:
        old_event = None
        map_json = self.project / "data/maps/LittlerootTown/map.json"
        read_map = json.loads(map_json.read_text(encoding="utf-8"))
        old_event = (
            read_map["object_events"][0]["x"],
            read_map["object_events"][0]["y"],
        )

        for name in ("bottom", "middle", "top"):
            path = self.export_dir / f"{name}.png"
            with Image.open(path) as opened:
                image = opened.convert("RGBA")
            grass = image.crop((10 * 16, 15 * 16, 11 * 16, 16 * 16))
            resized = Image.new("RGBA", (21 * 16, 21 * 16), (0, 0, 0, 0))
            resized.paste(image, (16, 16))
            for y in range(0, 21 * 16, 16):
                for x in range(0, 21 * 16, 16):
                    if x < 16 or y < 16 or x >= 20 * 16 or y >= 20 * 16:
                        resized.paste(grass, (x, y))
            resized.save(path, format="PNG")

        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--offset-x",
            "1",
            "--offset-y",
            "1",
            "--on-new",
            "skip",
            "--on-remove",
            "keep",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("layout resized:       20x20 -> 21x21", result.stdout)

        layouts = json.loads(
            (self.project / "data/layouts/layouts.json").read_text(encoding="utf-8")
        )["layouts"]
        layout = next(
            item for item in layouts if item["id"] == "LAYOUT_LITTLEROOT_TOWN"
        )
        self.assertEqual((layout["width"], layout["height"]), (21, 21))
        self.assertEqual(
            (self.project / "data/layouts/LittlerootTown/map.bin").stat().st_size,
            21 * 21 * 2,
        )
        updated = json.loads(map_json.read_text(encoding="utf-8"))
        self.assertEqual(
            (updated["object_events"][0]["x"], updated["object_events"][0]["y"]),
            (old_event[0] + 1, old_event[1] + 1),
        )
        reexport = Path(self._temp.name) / "resized-export"
        result = run_tool(
            "export",
            "LittlerootTown",
            "--output",
            str(reexport),
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        with Image.open(reexport / "bottom.png") as rendered:
            self.assertEqual(rendered.size, (21 * 16, 21 * 16))

    def test_replace_mode_reuses_freed_metatile(self) -> None:
        metatiles_path = (
            self.project / "data/tilesets/secondary/petalburg/metatiles.bin"
        )
        before_size = metatiles_path.stat().st_size
        fill_8x8(self.export_dir / "bottom.png", (1, 18), 0)
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            "--on-new",
            "replace",
            "--on-remove",
            "keep",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("replaced metatiles:   1", result.stdout)
        self.assertIn("added metatiles:      0", result.stdout)
        self.assertEqual(metatiles_path.stat().st_size, before_size)
        blocks = read_u16(self.project / "data/layouts/LittlerootTown/map.bin")
        self.assertEqual(blocks[18 * 20 + 1] & 0x3FF, 471)
        second = Path(self._temp.name) / "replace-export"
        result = run_tool(
            "export",
            "LittlerootTown",
            "--output",
            str(second),
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        with (
            Image.open(self.export_dir / "bottom.png") as edited,
            Image.open(second / "bottom.png") as rendered,
        ):
            box = (16, 18 * 16, 32, 19 * 16)
            self.assertEqual(
                edited.convert("RGBA").crop(box).tobytes(),
                rendered.convert("RGBA").crop(box).tobytes(),
            )

    def test_animation_ranges_parsed(self) -> None:
        info = scan_project_animations(Project(EXPANSION))
        general = info.tile_indices.get(("primary", "gTileset_General"), set())
        self.assertIn(508, general)
        self.assertTrue(general)

    def test_interactive_prompt_add_secondary(self) -> None:
        fill_8x8(self.export_dir / "bottom.png", (10, 15), 0)
        result = run_tool(
            "import",
            "LittlerootTown",
            "--input",
            str(self.export_dir),
            input_text="3\nn\n",
            project=self.project,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Add a new metatile to gTileset_Petalburg", result.stdout)
        self.assertIn("added metatiles:      1", result.stdout)


class SyntheticDoorTests(unittest.TestCase):
    def test_door_1x2_edit_and_noop_reimport(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-door1x2-") as temp:
            project = Path(temp) / "project"
            make_synthetic_1x2_door_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            door_rows = [
                (index, row)
                for index, row in enumerate(manifest["rows"])
                if row["kind"] == "door"
            ]
            self.assertEqual(len(door_rows), 2)
            sheet_path = export_dir / "animated_metatiles.png"
            with Image.open(sheet_path) as opened:
                sheet = opened.convert("RGBA")
            part_row = next(index for index, row in door_rows if row["part"] == 1)
            state = 2
            for y in range(16):
                for x in range(16):
                    sheet.putpixel(
                        (state * 16 + x, part_row * 16 + y), (255, 0, 0, 255)
                    )
            sheet.save(sheet_path, format="PNG")
            door_png = project / "graphics" / "door_anims" / "small_door.png"
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            patterns = load_indexed_tile_patterns(str(door_png))
            for source_index in (20, 21, 22, 23):
                self.assertEqual(patterns[source_index], bytes([1] * 64), source_index)

            snapshot = door_png.read_bytes()
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(door_png.read_bytes(), snapshot)

    def test_door_2x2_edit_and_noop_reimport(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-door-") as temp:
            project = Path(temp) / "project"
            make_synthetic_door_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            door_rows = [
                (index, row)
                for index, row in enumerate(manifest["rows"])
                if row["kind"] == "door"
            ]
            self.assertEqual(len(door_rows), 4)
            self.assertEqual([row["part"] for _index, row in door_rows], [0, 1, 2, 3])
            self.assertEqual(
                {row["key"] for _index, row in door_rows},
                {"gTileset_TestSecondary/METATILE_TestSecondary_Door_Test"},
            )

            sheet_path = export_dir / "animated_metatiles.png"
            with Image.open(sheet_path) as opened:
                sheet = opened.convert("RGBA")
            part_row = door_rows[2][0]
            state = 1
            for y in range(16):
                for x in range(16):
                    sheet.putpixel(
                        (state * 16 + x, part_row * 16 + y), (255, 0, 0, 255)
                    )
            sheet.save(sheet_path, format="PNG")

            door_png = project / "graphics" / "door_anims" / "test_door.png"
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            patterns = load_indexed_tile_patterns(str(door_png))
            for source_index in (18, 19, 22, 23):
                self.assertEqual(patterns[source_index], bytes([1] * 64), source_index)

            snapshot = door_png.read_bytes()
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(door_png.read_bytes(), snapshot)

    def test_door_palette_bank_updates_from_edit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-doorpal-") as temp:
            project = Path(temp) / "project"
            make_synthetic_door_project(project)
            colors = [(0, 0, 0)] * 16
            colors[1] = (255, 255, 0)
            write_jasc(
                project
                / "data"
                / "tilesets"
                / "primary"
                / "test_primary"
                / "palettes"
                / "05.pal",
                colors,
            )
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            part_row = next(
                index
                for index, row in enumerate(manifest["rows"])
                if row["kind"] == "door" and row["part"] == 0
            )
            sheet_path = export_dir / "animated_metatiles.png"
            with Image.open(sheet_path) as opened:
                sheet = opened.convert("RGBA")
            for y in range(16):
                for x in range(16):
                    sheet.putpixel((x, part_row * 16 + y), (255, 255, 0, 255))
            sheet.save(sheet_path, format="PNG")

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("Updated door palette bank references", result.stdout)
            source = (project / "src" / "field_door.c").read_text(encoding="utf-8")
            match = re.search(
                r"sDoorAnimPalettes_TestDoor\[\]\s*=\s*\{([^}]*)\}", source
            )
            self.assertIsNotNone(match)
            values = [int(value) for value in match.group(1).split(",")]
            self.assertEqual(values[:2], [5, 5])
            patterns = load_indexed_tile_patterns(
                str(project / "graphics" / "door_anims" / "test_door.png")
            )
            self.assertEqual(patterns[0], bytes([1] * 64))
            self.assertEqual(patterns[1], bytes([1] * 64))

    def test_add_door_state_generates_dedicated_size(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-doorstate-") as temp:
            project = Path(temp) / "project"
            make_synthetic_door_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            door_rows = [
                (index, row)
                for index, row in enumerate(manifest["rows"])
                if row["kind"] == "door"
            ]
            self.assertEqual(len(door_rows), 4)
            columns = manifest["columns"]
            states = door_rows[0][1]["states"]
            sheet_path = export_dir / "animated_metatiles.png"
            with Image.open(sheet_path) as opened:
                original = opened.convert("RGBA")
            sheet = Image.new(
                "RGBA",
                ((columns + 1) * 16, original.height),
                (0, 0, 0, 0),
            )
            sheet.paste(original, (0, 0))
            colors = [
                (255, 0, 0, 255),
                (0, 255, 0, 255),
                (0, 0, 255, 255),
                (255, 0, 0, 255),
            ]
            for row_index, row in door_rows:
                color = colors[row["part"]]
                for y in range(16):
                    for x in range(16):
                        sheet.putpixel((states * 16 + x, row_index * 16 + y), color)
            sheet.save(sheet_path, format="PNG")

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("added 1 door state(s)", result.stdout)
            source = (project / "src" / "field_door.c").read_text(encoding="utf-8")
            self.assertIn("sDoorAnimFrames_OpenTestSecondaryDoorTest", source)
            self.assertIn("sDoorAnimFrames_CloseTestSecondaryDoorTest", source)
            self.assertIn("[DOOR_SIZE_2x2_LEFT_TestSecondaryDoorTest]", source)
            self.assertIn(".size = DOOR_SIZE_2x2_LEFT_TestSecondaryDoorTest", source)
            with Image.open(
                project / "graphics" / "door_anims" / "test_door.png"
            ) as png:
                self.assertEqual(png.size, (32, 128))

            result = run_tool(
                "export",
                "TestMap",
                "--output",
                str(export_dir),
                "--overwrite",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            states_by_part = {
                row["part"]: row["states"]
                for row in manifest["rows"]
                if row["kind"] == "door"
            }
            self.assertEqual(states_by_part, {0: 4, 1: 4, 2: 4, 3: 4})
            frame_offsets = next(
                row["frame_offsets"]
                for row in manifest["rows"]
                if row["kind"] == "door"
            )
            self.assertEqual(frame_offsets, [0, 16, 32, 48])

    def test_new_tileset_animation_from_extra_row(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-newanim-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            rows = len(manifest["rows"])
            columns = max(2, manifest["columns"])
            sheet_path = export_dir / "animated_metatiles.png"
            with Image.open(sheet_path) as opened:
                original = opened.convert("RGBA")
            sheet = Image.new(
                "RGBA",
                (columns * 16, (rows + 1) * 16),
                (0, 0, 0, 0),
            )
            sheet.paste(original, (0, 0))
            for y in range(16):
                for x in range(16):
                    sheet.putpixel((x, rows * 16 + y), (255, 0, 0, 255))
                    sheet.putpixel((16 + x, rows * 16 + y), (0, 0, 255, 255))
            sheet.save(sheet_path, format="PNG")

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                input_text="m\nn\nTorch\n1\n2\n16\nMB_LONG_GRASS\n",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            source = (project / "src" / "tileset_anims.c").read_text(encoding="utf-8")
            self.assertIn("QueueAnimTiles_TestSecondary_Torch", source)
            self.assertIn("gTilesetAnims_TestSecondary_Torch_Frame0", source)
            self.assertIn("gTilesetAnims_TestSecondary_Torch_Frame1", source)
            self.assertIn("InitTilesetAnim_TestSecondary", source)
            headers = (project / "src" / "data" / "tilesets" / "headers.h").read_text(
                encoding="utf-8"
            )
            self.assertIn(".callback = InitTilesetAnim_TestSecondary", headers)
            anim_dir = (
                project
                / "data"
                / "tilesets"
                / "secondary"
                / "test_secondary"
                / "anim"
                / "torch"
            )
            self.assertTrue((anim_dir / "0.png").is_file())
            self.assertTrue((anim_dir / "1.png").is_file())
            secondary = project / "data" / "tilesets" / "secondary" / "test_secondary"
            self.assertEqual(len(read_u16(secondary / "metatiles.bin")), 24)
            self.assertEqual(
                read_u16(secondary / "metatile_attributes.bin")[-1] & 0xFF, 2
            )
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["rows"]), rows + 1)
            new_row = manifest["rows"][-1]
            self.assertEqual(new_row["kind"], "metatile")
            self.assertEqual(
                new_row["animations"][0]["key"], "gTileset_TestSecondary/torch"
            )
            catalog = scan_animation_catalog(Project(project))
            definition = catalog.by_key("gTileset_TestSecondary/torch")
            self.assertIsNotNone(definition)
            self.assertEqual(len(definition.frame_paths), 2)
            self.assertEqual(definition.ranges[0][1], 4)
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertNotIn("Extra animated sheet row", result.stdout)

    def test_new_door_from_extra_rows_and_dry_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-newdoor-") as temp:
            project = Path(temp) / "project"
            make_synthetic_door_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            rows = len(manifest["rows"])
            columns = manifest["columns"]
            sheet_path = export_dir / "animated_metatiles.png"
            with Image.open(sheet_path) as opened:
                original = opened.convert("RGBA")
            sheet = Image.new("RGBA", (columns * 16, (rows + 4) * 16), (0, 0, 0, 0))
            sheet.paste(original, (0, 0))
            colors = [
                (255, 0, 0, 255),
                (0, 255, 0, 255),
                (0, 0, 255, 255),
                (255, 0, 0, 255),
            ]
            for part, color in enumerate(colors):
                for y in range(16):
                    for x in range(16):
                        sheet.putpixel((x, (rows + part) * 16 + y), color)
            sheet.save(sheet_path, format="PNG")

            source_path = project / "src" / "field_door.c"
            labels_path = project / "include" / "constants" / "metatile_labels.h"
            source_before = source_path.read_text(encoding="utf-8")
            labels_before = labels_path.read_text(encoding="utf-8")
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--dry-run",
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                input_text="d\n1\nTestNewDoor\n",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(source_path.read_text(encoding="utf-8"), source_before)
            self.assertEqual(labels_path.read_text(encoding="utf-8"), labels_before)
            self.assertFalse(
                (project / "graphics" / "door_anims" / "test_new_door.png").exists()
            )

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                input_text="d\n1\nTestNewDoor\n",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            source = source_path.read_text(encoding="utf-8")
            labels = labels_path.read_text(encoding="utf-8")
            self.assertIn("sDoorAnimTiles_TestNewDoor", source)
            self.assertIn(
                'INCGFX_U8("graphics/door_anims/test_new_door.png", ".4bpp", '
                '"-mwidth 2 -mheight 4")',
                source,
            )
            self.assertIn("METATILE_TestSecondary_Door_TestNewDoor", source)
            self.assertIn(".size = DOOR_SIZE_2x2_LEFT", source)
            self.assertEqual(source.count("sDoorAnimTiles_TestNewDoor"), 2)
            self.assertEqual(source.count("sDoorAnimPalettes_TestNewDoor"), 2)
            self.assertIn(
                "#define METATILE_TestSecondary_Door_TestNewDoor 0x202", labels
            )
            catalog = scan_animation_catalog(Project(project))
            new_door = next(
                (
                    door
                    for door in catalog.door_defs
                    if door.key.endswith("METATILE_TestSecondary_Door_TestNewDoor")
                ),
                None,
            )
            self.assertIsNotNone(new_door)
            self.assertEqual(new_door.parts, (2, 2))
            self.assertEqual(new_door.frame_offsets, [0, 16, 32])
            new_png = project / "graphics" / "door_anims" / "test_new_door.png"
            self.assertTrue(new_png.is_file())
            new_patterns = load_indexed_tile_patterns(str(new_png))
            for source_index, color_index in (
                (0, 1),
                (8, 3),
                (2, 2),
                (10, 1),
            ):
                self.assertEqual(
                    new_patterns[source_index],
                    bytes([color_index] * 64),
                    source_index,
                )
            secondary = project / "data" / "tilesets" / "secondary" / "test_secondary"
            metatiles = read_u16(secondary / "metatiles.bin")
            self.assertEqual(len(metatiles), 36)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["rows"]), rows + 4)
            self.assertEqual(
                [row["kind"] for row in manifest["rows"][-4:]],
                ["door", "door", "door", "door"],
            )
            self.assertTrue(
                all(
                    row["key"]
                    == "gTileset_TestSecondary/METATILE_TestSecondary_Door_TestNewDoor"
                    for row in manifest["rows"][-4:]
                )
            )
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertNotIn("Extra animated sheet row", result.stdout)


class PalettePolicyTests(unittest.TestCase):
    def test_secondary_palette_banks_follow_tileset_indexing(self) -> None:
        class FakeTileset:
            def __init__(self, kind: str, global_indices: bool = False) -> None:
                self.kind = kind
                self.global_indices = global_indices
                self.palettes = [[(index, 0, 0)] * 16 for index in range(16)]

            def palette(self, index: int):
                return self.palettes[index]

            def palette_file_index(self, bank: int) -> int:
                if self.kind == "primary" or self.global_indices:
                    return bank
                return bank - 6

        class FakeProject:
            class settings:
                num_pals_in_primary = 6

        local_pair = TilesetPair(
            FakeProject(), FakeTileset("primary"), FakeTileset("secondary")
        )
        self.assertEqual(local_pair.palette_for(10), local_pair.secondary.palette(4))
        self.assertEqual(local_pair.palette_for(6), local_pair.secondary.palette(0))
        self.assertEqual(local_pair.palette_for(5), local_pair.primary.palette(5))

        global_pair = TilesetPair(
            FakeProject(),
            FakeTileset("primary"),
            FakeTileset("secondary", global_indices=True),
        )
        self.assertEqual(global_pair.palette_for(10), global_pair.secondary.palette(10))
        self.assertEqual(global_pair.palette_for(6), global_pair.secondary.palette(6))

    def test_nearest_mode_keeps_palette(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-palette-nearest-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            bottom = export_dir / "bottom.png"
            with Image.open(bottom) as opened:
                image = opened.convert("RGBA")
            for y in range(48, 64):
                for x in range(48, 64):
                    image.putpixel((x, y), (255, 0, 255, 255))
            image.save(bottom, format="PNG")
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "add-secondary",
                "--on-remove",
                "keep",
                "--palette-mode",
                "nearest",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            for path in (project / "data" / "tilesets").rglob("*.pal"):
                self.assertNotIn("255 0 255", path.read_text(encoding="ascii"))

    def test_animated_sheet_outside_colour_rewrites_palette(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-anim-palette-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads(
                (export_dir / "animated_metatiles.json").read_text(encoding="utf-8")
            )
            row = next(
                index
                for index, entry in enumerate(manifest["rows"])
                if entry["kind"] == "metatile"
            )
            sheet_path = export_dir / "animated_metatiles.png"
            with Image.open(sheet_path) as opened:
                sheet = opened.convert("RGBA")
            for y in range(16):
                for x in range(16):
                    sheet.putpixel((x, row * 16 + y), (255, 0, 255, 255))
            sheet.save(sheet_path, format="PNG")
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            palettes = list(
                (
                    project
                    / "data"
                    / "tilesets"
                    / "primary"
                    / "test_primary"
                    / "palettes"
                ).glob("*.pal")
            )
            self.assertTrue(
                any(
                    "255 0 255" in path.read_text(encoding="ascii") for path in palettes
                )
            )

    def test_smart_palette_rewrites_least_used_slot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-palette-policy-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            bottom = export_dir / "bottom.png"
            with Image.open(bottom) as opened:
                image = opened.convert("RGBA")
            for y in range(48, 64):
                for x in range(48, 64):
                    image.putpixel((x, y), (255, 0, 255, 255))
            image.save(bottom, format="PNG")

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "add-secondary",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("Palette bank", result.stdout)
            palette_text = (
                project
                / "data"
                / "tilesets"
                / "primary"
                / "test_primary"
                / "palettes"
                / "00.pal"
            ).read_text(encoding="ascii")
            self.assertIn("255 0 255", palette_text)
            result = run_tool(
                "export",
                "TestMap",
                "--output",
                str(export_dir),
                "--overwrite",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            with Image.open(bottom) as opened:
                self.assertEqual(
                    opened.convert("RGBA").getpixel((48, 48)), (255, 0, 255, 255)
                )

    def test_smart_palette_protects_last_colour_family(self) -> None:
        blue = [(20 + index * 15, 60, 180) for index in range(10)]
        green = (20, 180, 40)
        palette = [(0, 0, 0)] + blue + [green] + [(0, 0, 0)] * 4

        class FakeTileset:
            def __init__(self) -> None:
                self.metatiles = [[TileRef(0, 1)]]
                self.patterns = [bytes([11] * 64)]

            def pattern_at(self, global_index: int) -> bytes:
                return self.patterns[global_index]

        class FakePair:
            def __init__(self) -> None:
                self.primary = FakeTileset()
                self.secondary = SimpleNamespace(metatiles=[])

            def palette_for(self, index: int):
                return palette

        desired = Counter({(220 - index, 30, 30): 100 - index for index in range(8)})
        plan = plan_bank_colors(FakePair(), 1, desired, "smart")
        self.assertIn(11, plan.protected_slots)
        self.assertEqual(plan.colors[11], green)
        self.assertTrue(plan.replaced_slots)

    def test_replace_palette_keeps_exact_colours_and_fits_new_ones(self) -> None:
        def rgb(value: str) -> tuple[int, int, int]:
            return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))

        palette = [(0, 0, 0)] + [
            rgb(value)
            for value in (
                "182952",
                "1D4826",
                "83C562",
                "398B31",
                "395200",
                "3B823B",
                "E38981",
                "82354F",
                "DA6769",
                "BE4154",
                "F5AE99",
                "80BF6D",
                "A4D5C5",
                "73C5A4",
                "296A2B",
                "6DAE5C",
            )
        ]

        class FakeTileset:
            def __init__(self) -> None:
                self.metatiles: list[list[TileRef]] = []
                self.patterns: list[bytes] = []

            def pattern_at(self, global_index: int) -> bytes:
                return self.patterns[global_index]

        class FakePair:
            def __init__(self) -> None:
                self.primary = FakeTileset()
                self.secondary = SimpleNamespace(metatiles=[])

            def palette_for(self, index: int):
                return palette

        desired = Counter(
            {
                rgb(value): 1
                for value in (
                    "80BF6D",
                    "BE4154",
                    "82354F",
                    "DA6769",
                    "E38981",
                    "F5AE99",
                    "1D4826",
                    "3B823B",
                    "296A2B",
                    "6AB55F",
                    "6DAE5C",
                )
            }
        )
        plan = plan_bank_colors(FakePair(), 1, desired, "replace")
        self.assertTrue(set(desired).issubset(set(plan.colors[1:16])))
        self.assertEqual(plan.colors[2], rgb("1D4826"))
        self.assertEqual(plan.colors[7], rgb("E38981"))


class TilesetVariantTests(unittest.TestCase):
    def test_variant_creation_keeps_map_tiles_and_updates_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-variant-") as temp:
            project_dir = Path(temp) / "project"
            make_synthetic_triple_project(project_dir)
            fieldmap = project_dir / "include" / "fieldmap.h"
            text = fieldmap.read_text(encoding="ascii")
            text = text.replace(
                "#define NUM_TILES_IN_PRIMARY 512",
                "#define NUM_TILES_IN_PRIMARY 16",
            ).replace("#define NUM_TILES_TOTAL 1024", "#define NUM_TILES_TOTAL 26")
            fieldmap.write_text(text, encoding="ascii")
            project = Project(project_dir)
            target = project.resolve("TestMap")
            pair = project.load_tileset_pair(target.layout)
            assert target.layout is not None
            original_counts = {
                "primary": len(pair.primary.patterns),
                "secondary": len(pair.secondary.patterns),
            }
            pair.secondary.patterns.extend(
                [bytes([3] * 64), bytes([2] * 64), bytes([1] * 64)]
            )
            new_metatile = pair.secondary.allocate_metatile(
                [
                    TileRef(pair.secondary.global_tile_base + index, 1)
                    for index in (2, 3, 4)
                ]
                + [TileRef(0, 0)] * 9
            )
            blocks = project.read_blocks(target.layout)
            blocks[10] = new_metatile
            result = create_layout_variants(
                project,
                pair,
                target.layout,
                target,
                blocks,
                original_counts,
            )
            self.assertTrue(result.changed)
            self.assertIn("secondary", result.variants)
            variant = result.variants["secondary"]
            variant.save()
            self.assertLessEqual(len(variant.patterns), variant.max_local_tiles)
            self.assertEqual(variant.name, "gTileset_TestSecondary_TestMap")
            self.assertTrue(variant.tiles_png_path.is_file())
            layouts = json.loads(
                (project_dir / "data" / "layouts" / "layouts.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(layouts["layouts"][0]["secondary_tileset"], variant.name)
            registry_path = (
                project_dir
                / "data"
                / "tilesets"
                / "secondary"
                / "test_secondary_variants.json"
            )
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["variants"][0]["name"], variant.name)
            extern = (project_dir / "include" / "tilesets.h").read_text(
                encoding="utf-8"
            )
            self.assertIn(f"extern const struct Tileset {variant.name};", extern)

            base = project.load_tileset("gTileset_TestSecondary", "secondary")
            mapping = registry["variants"][0]["variant_tile_to_base"]
            variant_local, base_local = next(
                (int(v), int(b)) for v, b in mapping.items()
            )
            new_pattern = bytes([2] * 64)
            base.patterns[base_local] = new_pattern
            base.dirty_tiles = True
            base.save()
            written = sync_variants_from_base(project, base)
            self.assertTrue(written)
            synced_variant = project.load_tileset(variant.name, "secondary")
            self.assertEqual(synced_variant.patterns[variant_local], new_pattern)

    def test_force_variant_without_overflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-force-variant-") as temp:
            project_dir = Path(temp) / "project"
            make_synthetic_triple_project(project_dir)
            project = Project(project_dir)
            target = project.resolve("TestMap")
            assert target.layout is not None
            pair = project.load_tileset_pair(target.layout)
            original_counts = {
                "primary": len(pair.primary.patterns),
                "secondary": len(pair.secondary.patterns),
            }
            base_patterns = list(pair.secondary.patterns)
            result = create_layout_variants(
                project,
                pair,
                target.layout,
                target,
                project.read_blocks(target.layout),
                original_counts,
                force_kinds={"secondary"},
            )
            self.assertTrue(result.changed)
            self.assertIn("secondary", result.variants)
            variant = result.variants["secondary"]
            variant.save()
            self.assertEqual(variant.name, "gTileset_TestSecondary_TestMap")
            self.assertEqual(
                variant.patterns[: len(base_patterns)], base_patterns
            )
            base_after = project.load_tileset("gTileset_TestSecondary", "secondary")
            self.assertEqual(base_after.patterns, base_patterns)
            layouts = json.loads(
                (project_dir / "data" / "layouts" / "layouts.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                layouts["layouts"][0]["secondary_tileset"], variant.name
            )
            registry_path = (
                project_dir
                / "data"
                / "tilesets"
                / "secondary"
                / "test_secondary_variants.json"
            )
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["variants"][0]["name"], variant.name)
            self.assertTrue(
                any(
                    "because --force-variant was requested" in warning
                    for warning in result.warnings
                )
            )

    def test_import_force_variant_cli(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-force-variant-import-") as temp:
            project_dir = Path(temp) / "project"
            make_synthetic_triple_project(project_dir)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            png_patch(export_dir / "bottom.png", (16, 16), (48, 16))
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "add-secondary",
                "--on-remove",
                "keep",
                "--force-variant",
                "secondary",
                project=project_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("because --force-variant was requested", result.stdout)
            layouts = json.loads(
                (project_dir / "data" / "layouts" / "layouts.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                layouts["layouts"][0]["secondary_tileset"],
                "gTileset_TestSecondary_TestMap",
            )

    def test_import_overflows_into_variant(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-variant-import-") as temp:
            project_dir = Path(temp) / "project"
            make_synthetic_triple_project(project_dir)
            headers_path = project_dir / "src" / "data" / "tilesets" / "headers.h"
            headers_text = headers_path.read_text(encoding="ascii")
            headers_text = headers_text.replace(
                "const struct Tileset gTileset_TestSecondary = {\n"
                "    .isCompressed = FALSE,\n"
                "    .isSecondary = TRUE,\n"
                "};",
                "const struct Tileset gTileset_TestSecondary = {\n"
                "    .isCompressed = FALSE,\n"
                "    .isSecondary = TRUE,\n"
                "    .callback = InitTilesetAnim_TestSecondary,\n"
                "};",
            )
            headers_path.write_text(headers_text, encoding="ascii")
            fieldmap = project_dir / "include" / "fieldmap.h"
            text = fieldmap.read_text(encoding="ascii")
            text = text.replace(
                "#define NUM_TILES_IN_PRIMARY 512",
                "#define NUM_TILES_IN_PRIMARY 16",
            ).replace("#define NUM_TILES_TOTAL 1024", "#define NUM_TILES_TOTAL 26")
            fieldmap.write_text(text, encoding="ascii")
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            bottom = export_dir / "bottom.png"
            with Image.open(bottom) as opened:
                image = opened.convert("RGBA")
            for cell, phase in (((2, 2), 1), ((3, 3), 2), ((1, 3), 3)):
                for y in range(16):
                    for x in range(16):
                        color = (
                            (255, 0, 0, 255)
                            if (x + y + phase) % 3 == 0
                            else (0, 0, 255, 255)
                            if (x * 2 + y + phase) % 5 == 0
                            else (0, 0, 0, 0)
                        )
                        image.putpixel((cell[0] * 16 + x, cell[1] * 16 + y), color)
            image.save(bottom, format="PNG")
            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "add-secondary",
                "--on-remove",
                "keep",
                project=project_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("exceeded the tile limit", result.stdout)
            fresh = Project(project_dir)
            base_after = fresh.load_tileset("gTileset_TestSecondary", "secondary")
            for metatile in base_after.metatiles:
                for entry in metatile:
                    local = entry.tile_index - base_after.global_tile_base
                    self.assertLess(local, base_after.max_local_tiles)
            layouts = json.loads(
                (project_dir / "data" / "layouts" / "layouts.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                layouts["layouts"][0]["secondary_tileset"],
                "gTileset_TestSecondary_TestMap",
            )
            headers = (
                project_dir / "src" / "data" / "tilesets" / "headers.h"
            ).read_text(encoding="utf-8")
            variant_block = re.search(
                r"const struct Tileset gTileset_TestSecondary_TestMap =\s*\{(.*?)\};",
                headers,
                re.DOTALL,
            )
            self.assertIsNotNone(variant_block)
            self.assertIn(
                ".callback = InitTilesetAnim_TestSecondary",
                variant_block.group(1),
            )
            second = Path(temp) / "second"
            result = run_tool(
                "export",
                "TestMap",
                "--output",
                str(second),
                project=project_dir,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


class LegacySourceParserTests(unittest.TestCase):
    def test_legacy_door_and_incbin_animation_formats(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-legacy-") as temp:
            project_dir = Path(temp) / "project"
            make_synthetic_1x2_door_project(project_dir)
            (project_dir / "src" / "field_door.c").write_text(
                """
static const u8 sDoorAnimTiles_Legacy[] =
    INCBIN_U8("graphics/door_anims/small_door.4bpp");
static const u16 sDoorAnimPalettes_Legacy[] =
    {1, 1, 1, 1, 1, 1, 1, 1};

static const struct DoorAnimFrame sDoorOpenAnimFrames[] =
{
    {4, -1},
    {4, 0},
    {4, 0x100},
    {4, 0x200},
    {0, 0},
};

static const struct DoorAnimFrame sDoorCloseAnimFrames[] =
{
    {4, 0x200},
    {4, 0x100},
    {4, 0},
    {4, -1},
    {0, 0},
};

static const struct DoorGraphics sDoorAnimGraphicsTable[] =
{
    {METATILE_TestSecondary_Door_Small, DOOR_SOUND_NORMAL, 1,
     sDoorAnimTiles_Legacy, sDoorAnimPalettes_Legacy},
    {0, 0, 0, NULL, NULL},
};
""",
                encoding="ascii",
            )
            (project_dir / "src" / "tileset_anims.c").write_text(
                """
const u16 gTilesetAnims_General_Water_Frame0[] =
    INCBIN_U16("data/tilesets/primary/test_primary/anim/water/0.4bpp");
const u16 gTilesetAnims_General_Water_Frame1[] =
    INCBIN_U16("data/tilesets/primary/test_primary/anim/water/1.4bpp");
const u16 *const gTilesetAnims_General_Water[] = {
    gTilesetAnims_General_Water_Frame0,
    gTilesetAnims_General_Water_Frame1,
};

static void QueueAnimTiles_General_Water(u16 timer)
{
    u16 i = timer % ARRAY_COUNT(gTilesetAnims_General_Water);
    AppendTilesetAnimToBuffer(gTilesetAnims_General_Water[i],
        (u16 *)(BG_VRAM + TILE_OFFSET_4BPP(1)), 1 * TILE_SIZE_4BPP);
}
""",
                encoding="ascii",
            )
            catalog = scan_animation_catalog(Project(project_dir))
            door = next(
                (
                    entry
                    for entry in catalog.door_defs
                    if entry.metatile_label == "METATILE_TestSecondary_Door_Small"
                ),
                None,
            )
            self.assertIsNotNone(door)
            self.assertEqual(door.parts, (1, 2))
            self.assertEqual(door.frame_offsets, [0, 8, 16])
            self.assertEqual(door.frame_path, "graphics/door_anims/small_door.png")
            animation = catalog.by_key("gTileset_TestPrimary/general_water")
            self.assertIsNotNone(animation)
            self.assertEqual(
                animation.frame_paths[0],
                "data/tilesets/primary/test_primary/anim/water/0.png",
            )
            self.assertEqual(animation.ranges, [(1, 1)])


class SyntheticPaletteTests(unittest.TestCase):
    def test_palette_sheet_reports_usage_and_applies_colors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ptmap-palette-") as temp:
            project = Path(temp) / "project"
            make_synthetic_triple_project(project)
            export_dir = Path(temp) / "export"
            result = run_tool(
                "export", "TestMap", "--output", str(export_dir), project=project
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue((export_dir / "palettes.png").is_file())
            manifest = json.loads(
                (export_dir / "palettes.json").read_text(encoding="utf-8")
            )
            row_index, row = next(
                (index, entry)
                for index, entry in enumerate(manifest["rows"])
                if entry["kind"] == "primary" and entry["palette_index"] == 1
            )
            self.assertGreater(row["used_pixels"][1], 0)
            with Image.open(export_dir / "palettes.png") as opened:
                sheet = opened.convert("RGBA")
            for y in range(16):
                for x in range(16):
                    sheet.putpixel(
                        (12 * 16 + x, row_index * 16 + y), (250, 10, 20, 255)
                    )
            sheet.save(export_dir / "palettes.png", format="PNG")

            result = run_tool(
                "import",
                "TestMap",
                "--input",
                str(export_dir),
                "--on-new",
                "skip",
                "--on-remove",
                "keep",
                project=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("palette sheet:        changed", result.stdout)
            self.assertIn("Updated 1 palette file", result.stdout)
            palette_path = (
                project
                / "data"
                / "tilesets"
                / "primary"
                / "test_primary"
                / "palettes"
                / "01.pal"
            )
            lines = palette_path.read_text(encoding="ascii").splitlines()
            self.assertEqual(lines[3 + 12], "250 10 20")


if __name__ == "__main__":
    unittest.main()
