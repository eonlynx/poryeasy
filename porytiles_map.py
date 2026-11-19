#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10.0"]
# ///
"""Poryeasy - export and import full map layer PNGs.

Examples (run from a pokeemerald / pokeemerald-expansion project root):

    uv run <porytiles>/porytiles/tools/map_editor/porytiles_map.py export LittlerootTown
    # edit poryeasy/maps/LittlerootTown/{bottom,middle,top}.png in GIMP
    uv run .../porytiles_map.py import LittlerootTown

If Pillow is installed globally, plain ``python porytiles_map.py`` works too.
Windows users can also use ``porytiles-map.cmd`` from this directory.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Make ``ptmap`` importable when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ptmap.errors import MapEditorError
from ptmap.export import default_export_directory, export_map
from ptmap.import_map import ImportMap
from ptmap.project import Project


def find_project_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "data" / "layouts" / "layouts.json").is_file():
            return candidate
    return None


def make_project(args: argparse.Namespace) -> Project:
    root = (
        Path(args.project_root).resolve()
        if args.project_root
        else find_project_root(Path.cwd())
    )
    if root is None:
        raise MapEditorError(
            "Could not find a decomp project (data/layouts/layouts.json) above the current "
            "directory. Run from the project root or pass --project-root."
        )
    return Project(root, layer_mode=args.layer_mode)


def cmd_export(args: argparse.Namespace) -> int:
    project = make_project(args)
    target = project.resolve(args.map)
    output = Path(args.output).resolve() if args.output else None
    paths = export_map(project, target, output, overwrite=args.overwrite)
    layout = target.layout
    print(f"Exported {target.display_name} ({layout.id}) to {paths.directory}")
    print(
        f"  {layout.width}x{layout.height} metatiles ({layout.width * 16}x{layout.height * 16} px)"
    )
    print(f"  primary:   {layout.primary_tileset}")
    print(f"  secondary: {layout.secondary_tileset}")
    print(f"  layers:    {paths.bottom.name}, {paths.middle.name}, {paths.top.name}")
    print(f"  preview:   {paths.preview.name}")
    print(
        f"  animated:  {paths.animation_sheet.name} ({paths.animation_manifest.name})"
    )
    print(f"  palettes:  {paths.palette_sheet.name} ({paths.palette_manifest.name})")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    project = make_project(args)
    target = project.resolve(args.map)
    input_dir = (
        Path(args.input).resolve()
        if args.input
        else default_export_directory(project, target)
    )
    importer = ImportMap(
        project,
        target,
        input_dir,
        on_new=args.on_new,
        on_remove=args.on_remove,
        on_remove_tiles=args.on_remove_tiles,
        quantize=args.quantize,
        backup=not args.no_backup,
        dry_run=args.dry_run,
        interactive=args.interactive,
        anchor=args.anchor,
        offset_x=args.offset_x,
        offset_y=args.offset_y,
        allow_shrink=args.allow_shrink,
        fill_metatile=args.fill_metatile,
        shift_events=args.shift_events,
        allow_animated_edits=args.allow_animated_edits,
        on_extra_row=args.on_extra_animation_row,
        force_palette_edits=args.force_palette_edits,
        palette_mode=args.palette_mode,
        force_variant=args.force_variant,
        reuse_unused_metatiles=args.reuse_unused_metatiles,
        empty_metatiles_only=args.empty_metatiles_only,
        palette_min_share=args.palette_min_share / 100.0,
        tiny_edit_pixels=args.tiny_edit_pixels,
        tiny_edit_policy=args.tiny_edit_policy,
        tile_match_tolerance=args.tile_match_tolerance,
        dedupe_metatiles=args.dedupe_metatiles,
    )
    result = importer.run()

    if (
        result.changes == 0
        and not result.animation_changed
        and not result.palette_changed
        and not result.cleared_unused_primary
        and not result.cleared_unused_secondary
        and not result.deduped_metatiles
        and not result.dedupe_rewrites
    ):
        print(f"No changes detected in {input_dir}.")
        for warning in result.warnings:
            print(f"  warning: {warning}")
        return 0

    print()
    print("Import summary")
    print(f"  changed blocks:       {result.changes}")
    print(
        f"  animation sheet:      {'changed' if result.animation_changed else 'unchanged'}"
    )
    print(
        f"  palette sheet:        {'changed' if result.palette_changed else 'unchanged'}"
    )
    if result.resized_from and result.resized_to:
        print(
            f"  layout resized:       {result.resized_from[0]}x{result.resized_from[1]}"
            f" -> {result.resized_to[0]}x{result.resized_to[1]}"
            f" (offset {result.resized_offset[0]},{result.resized_offset[1]})"
        )
    print(f"  auto-remapped:        {result.remapped}")
    print(f"  edit groups:          {result.groups}")
    print(f"  modified metatiles:   {result.modified_metatiles}")
    print(f"  replaced metatiles:   {result.replaced_metatiles}")
    print(f"  added metatiles:      {result.added_metatiles}")
    print(f"  added tiles:          {result.added_tiles}")
    if result.reused_tiles:
        print(
            f"  reused near tiles:    {result.reused_tiles} "
            f"(up to {result.max_reused_difference} px difference)"
        )
    if result.tiny_edits:
        print(f"  tiny edits:           {result.tiny_edits}")
    if result.ignored_palette_colors:
        print(
            f"  ignored new colours:  {result.ignored_palette_colors} "
            "(below --palette-min-share)"
        )
    if result.replace_units:
        print(
            f"  replace plan:         {result.replace_assigned}/{result.replace_units} "
            "units assigned to freed slots"
        )
        print(
            f"  freed candidates:     primary {result.freed_primary}, "
            f"secondary {result.freed_secondary}, protected {result.freed_protected}"
        )
        print(
            f"  replace targets:      primary {result.target_primary}, "
            f"secondary {result.target_secondary}"
        )
        print(
            f"  palette fit:          exact primary {result.palette_exact_primary}, "
            f"exact secondary {result.palette_exact_secondary}"
        )
        if result.preferred_secondary_overflow:
            print(
                f"  overflow preference:  secondary for "
                f"{result.preferred_secondary_overflow} unit(s)"
            )
        print(
            f"  free tiles before:    primary {result.primary_free_tiles}, "
            f"secondary {result.secondary_free_tiles}"
        )
    print(f"  skipped blocks:       {result.skipped}")
    if result.cleared_unused_primary or result.cleared_unused_secondary:
        print(
            f"  cleared unused:       primary {result.cleared_unused_primary}, "
            f"secondary {result.cleared_unused_secondary}"
        )
    if result.kept_unused_primary or result.kept_unused_secondary:
        print(
            f"  kept unused:          primary {result.kept_unused_primary}, "
            f"secondary {result.kept_unused_secondary} (content preserved)"
        )
    if result.left_empty_replace_slots:
        print(f"  empty freed slots:    {result.left_empty_replace_slots}")
    if (
        result.deduped_metatiles
        or result.dedupe_rewrites
        or result.dedupe_skipped
    ):
        print(
            f"  deduped metatiles:    {result.deduped_metatiles} "
            f"in {result.dedupe_layouts} layout(s)"
        )
        print(f"  dedupe rewrites:      {result.dedupe_rewrites} cell(s)")
        if result.dedupe_skipped:
            print(f"  dedupe skipped:       {result.dedupe_skipped} group(s)")
    if result.dedupe_backup_dir is not None:
        print(f"  dedupe backup:        {result.dedupe_backup_dir}")
    if result.removed_metatiles:
        print(f"  cleared metatiles:    {result.removed_metatiles}")
    if result.cleared_tiles:
        print(f"  cleared tile slots:   {result.cleared_tiles}")
    for warning in result.warnings:
        print(f"  warning: {warning}")
    if result.backup_dir is not None:
        print(f"  backup:               {result.backup_dir}")
    if args.sync_porytiles and not args.dry_run:
        exe = shutil.which("porytiles")
        if exe is None:
            print(
                "  warning: --sync-porytiles was requested, but 'porytiles' is not on PATH."
            )
        else:
            for name, kind in dict.fromkeys(result.touched_tilesets):
                directory = project.tileset_dir(name, kind)
                if not (directory / "porytiles_src").is_dir():
                    continue
                print(f"  syncing Porytiles source assets for {name}...")
                completed = subprocess.run(
                    [exe, "decompile-tileset", name, "--no-verify-checksums"],
                    cwd=project.root,
                    check=False,
                )
                if completed.returncode != 0:
                    print(f"  warning: porytiles decompile-tileset failed for {name}.")
    if args.dry_run:
        print("  dry run:              no files were written")
    else:
        print(f"  files written:        {len(dict.fromkeys(result.written))}")
        print()
        print("Reload the tileset/map in Porymap to see the changes.")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    project = make_project(args)
    target = project.resolve(args.map)
    layout = target.layout
    tilesets = project.load_tileset_pair(layout)
    blocks = project.read_blocks(layout)
    ids = [word & project.settings.metatile_id_mask for word in blocks]
    unique = len(set(ids))
    print(f"{target.display_name} ({target.map.id if target.map else '<layout only>'})")
    print(f"  layout:     {layout.id} ({layout.width}x{layout.height} metatiles)")
    print(f"  blockdata:  {layout.blockdata_filepath}")
    print(f"  border:     {layout.border_filepath or '<none>'}")
    print(f"  primary:    {layout.primary_tileset}")
    print(f"  secondary:  {layout.secondary_tileset}")
    print(
        f"  layer mode: {'triple' if tilesets.entries_per_metatile == 12 else 'dual'}"
    )
    print(
        f"  metatiles:  {len(tilesets.primary.metatiles)} primary, "
        f"{len(tilesets.secondary.metatiles)} secondary"
    )
    print(
        f"  tiles:      {tilesets.primary.local_tile_count} primary, "
        f"{tilesets.secondary.local_tile_count} secondary"
    )
    print(f"  used ids:   {unique} distinct metatile ids on this map")
    return 0


def parse_metatile_id(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid metatile ID (try 0x200 or 512)"
        ) from None


def cmd_usages(args: argparse.Namespace) -> int:
    project = make_project(args)
    target = project.resolve(args.map)
    layout = target.layout
    tilesets = project.load_tileset_pair(layout)
    counts = project.metatile_reference_counts()

    if args.metatile is not None:
        metatile_id = args.metatile
        owner = tilesets.metatile_owner(metatile_id)
        references = project.find_metatile_references(metatile_id)
        print(
            f"Metatile 0x{metatile_id:03X} ({metatile_id}) in {owner.name}: "
            f"{counts.get(metatile_id, 0)} map/border usage(s)."
        )
        print(
            f"  protected label: "
            f"{'yes (metatile_labels.h)' if project.is_protected_metatile_id(metatile_id) else 'no'}"
        )
        print(
            f"  scan complete:   "
            f"{'yes' if project.metatile_usage_complete(owner) else 'no'}"
        )
        if not project.metatile_usage_complete(owner):
            missing = project.metatile_usage_incomplete_layouts(owner)
            print(f"  unreadable:      {', '.join(missing[:10])}")
        if references:
            print("  references:")
            for layout_id, x, y, source in references[:80]:
                print(f"    {layout_id} {source} ({x},{y})")
            if len(references) > 80:
                print(f"    ... and {len(references) - 80} more")
        else:
            print("  references:      <none>")
        return 0

    blocks = project.read_blocks(layout)
    ids = sorted({word & project.settings.metatile_id_mask for word in blocks})
    print(f"Metatile usages for {target.display_name} ({layout.id})")
    print(f"  {'ID':>6}  {'local':>5}  {'global':>6}  {'protected':>9}  tileset")
    for metatile_id in ids:
        owner = tilesets.metatile_owner(metatile_id)
        local = metatile_id - owner.global_metatile_base
        print(
            f"  0x{metatile_id:03X}  {local:>5}  {counts.get(metatile_id, 0):>6}  "
            f"{'yes' if project.is_protected_metatile_id(metatile_id) else 'no':>9}  "
            f"{owner.name}"
        )
    incomplete = {
        owner.name
        for owner in (tilesets.primary, tilesets.secondary)
        if not project.metatile_usage_complete(owner)
    }
    if incomplete:
        print(
            "  warning: usage scan incomplete for "
            + ", ".join(sorted(incomplete))
            + " (missing/unreadable layouts); deletion is disabled for those tilesets."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poryeasy",
        description="Poryeasy: export/import full Pokémon GBA maps as editable PNG layers.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "map", help="Map symbol/name, map folder, layout ID, or layout name"
        )
        p.add_argument(
            "--project-root", help="Decomp project root (default: search upwards)"
        )
        p.add_argument(
            "--layer-mode",
            choices=("dual", "triple"),
            help="Override NUM_TILES_PER_METATILE from the project headers",
        )

    export_parser = sub.add_parser(
        "export", help="Export a map to bottom/middle/top PNGs"
    )
    add_common(export_parser)
    export_parser.add_argument("-o", "--output", help="Output directory")
    export_parser.add_argument(
        "--overwrite",
        action="store_true",
        default=True,
        help="Replace existing layer images (default)",
    )
    export_parser.add_argument(
        "--no-overwrite",
        dest="overwrite",
        action="store_false",
        help="Refuse to replace existing layer images",
    )
    export_parser.set_defaults(func=cmd_export)

    import_parser = sub.add_parser(
        "import",
        help="Import edited layer PNGs back into the map",
        epilog=(
            "Common cases:\n"
            "  poryeasy import LittlerootTown\n"
            "  poryeasy import LittlerootTown --input <export-dir>\n"
            "  poryeasy import LittlerootTown --offset-x 2 --offset-y 1\n"
            "Images with a different size are accepted automatically; top-left is "
            "the default anchor."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common(import_parser)
    import_parser.add_argument(
        "-i",
        "--input",
        help="Directory containing bottom/middle/top PNGs (default: the map's export folder)",
    )
    import_parser.add_argument(
        "--on-new",
        choices=(
            "ask",
            "modify-metatile",
            "modify-tiles",
            "add-secondary",
            "add-primary",
            "replace",
            "skip",
        ),
        default="replace",
        help="What to do when an edited block does not match an existing metatile (default: replace)",
    )
    import_parser.add_argument(
        "--force-variant",
        nargs="?",
        const="both",
        choices=("primary", "secondary", "both"),
        default=None,
        help=(
            "Create a per-map tileset variant even when the tileset is under the tile "
            "limit. Choose primary, secondary or both (default when omitted: both)"
        ),
    )
    import_parser.add_argument(
        "--anchor",
        choices=("top-left", "top-right", "bottom-left", "bottom-right", "center"),
        default="top-left",
        help="Where the old map sits when the layer images change size",
    )
    import_parser.add_argument(
        "--offset-x",
        type=int,
        help="Explicit old-map X offset inside the new canvas (overrides --anchor)",
    )
    import_parser.add_argument(
        "--offset-y",
        type=int,
        help="Explicit old-map Y offset inside the new canvas (overrides --anchor)",
    )
    import_parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="Allow layer images smaller than the layout (cropping)",
    )
    import_parser.add_argument(
        "--fill-metatile",
        type=parse_metatile_id,
        default=0,
        help="Metatile ID used for new canvas cells (default: 0)",
    )
    import_parser.add_argument(
        "--no-shift-events",
        dest="shift_events",
        action="store_false",
        help="Do not shift map.json event coordinates when the layout origin moves",
    )
    import_parser.add_argument(
        "--allow-animated-edits",
        action="store_true",
        help="Allow modify-tiles to overwrite tile slots used by tileset animations",
    )
    import_parser.add_argument(
        "--on-extra-animation-row",
        choices=("ask", "skip"),
        default="skip",
        help="How to handle animated sheet rows without a manifest entry (default: skip)",
    )
    import_parser.add_argument(
        "--force-palette-edits",
        action="store_true",
        help="Allow palette colour edits even when metatile usage is incomplete",
    )
    import_parser.add_argument(
        "--palette-mode",
        choices=("smart", "replace", "nearest", "error"),
        default="smart",
        help=(
            "How to handle edited colours outside the active palettes: smart rewrites "
            "least-used slots (default), replace rewrites the whole affected palette, "
            "nearest maps to existing colours, error refuses"
        ),
    )
    import_parser.add_argument(
        "--palette-min-share",
        type=float,
        default=0.5,
        metavar="PERCENT",
        help=(
            "Ignore new colours that appear in less than this percentage of the "
            "resulting map's pixels (default: 0.5; use 0 to disable)"
        ),
    )
    import_parser.add_argument(
        "--tiny-edit-pixels",
        type=int,
        default=4,
        metavar="N",
        help=(
            "Treat changes of at most N pixels as suspicious tiny edits "
            "(default: 4; use 0 to disable)"
        ),
    )
    import_parser.add_argument(
        "--tiny-edit-policy",
        choices=("warn", "error", "allow"),
        default="warn",
        help=(
            "What to do with tiny edits that would create or modify metatiles: "
            "warn and continue (default), abort the import, or allow silently"
        ),
    )
    import_parser.add_argument(
        "--tile-match-tolerance",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Reuse an existing 8x8 tile when the edited tile differs by at most N "
            "pixels (default: 1; tiny edits use --tiny-edit-pixels; use 0 to "
            "disable fuzzy tile matching)"
        ),
    )
    reuse_group = import_parser.add_mutually_exclusive_group()
    reuse_group.add_argument(
        "--no-reuse-unused-metatiles",
        dest="reuse_unused_metatiles",
        action="store_false",
        help=(
            "Do not clear or reuse metatile slots with zero usages across every map "
            "before importing (default: clear them and reuse them for new content)"
        ),
    )
    reuse_group.add_argument(
        "--reuse-empty-metatiles-only",
        dest="empty_metatiles_only",
        action="store_true",
        help=(
            "Reuse only already-empty metatile slots; keep unused metatiles that "
            "still contain art intact"
        ),
    )
    import_parser.add_argument(
        "--no-dedupe-metatiles",
        dest="dedupe_metatiles",
        action="store_false",
        help=(
            "Do not merge duplicate metatiles in the imported map's primary and "
            "secondary tilesets before importing (default: merge exact duplicates, "
            "rewrite map/border references, and empty the duplicate slots)"
        ),
    )
    import_parser.add_argument(
        "--on-remove",
        choices=("ask", "keep", "clear"),
        default="keep",
        help="What to do with metatiles that became unused (default: keep)",
    )
    import_parser.add_argument(
        "--on-remove-tiles",
        choices=("ask", "keep", "clear"),
        default="keep",
        help="What to do with tile graphics that are no longer referenced",
    )
    import_parser.add_argument(
        "--quantize",
        choices=("nearest", "error"),
        default="nearest",
        help="How to handle edited colours outside every allowed palette",
    )
    import_parser.add_argument(
        "--sync-porytiles",
        action="store_true",
        help="Run 'porytiles decompile-tileset' for Porytiles-managed tilesets after import",
    )
    import_parser.add_argument(
        "--no-backup", action="store_true", help="Skip file backups"
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report without writing files",
    )
    import_parser.add_argument(
        "--no-interactive",
        dest="interactive",
        action="store_false",
        help="Never prompt; --on-new must not be 'ask'",
    )
    import_parser.set_defaults(func=cmd_import)

    info_parser = sub.add_parser(
        "info", help="Show resolved layout/tileset information"
    )
    add_common(info_parser)
    info_parser.set_defaults(func=cmd_info)

    usages_parser = sub.add_parser(
        "usages", help="Show global metatile usage counts (like Porymap's usages view)"
    )
    add_common(usages_parser)
    usages_parser.add_argument(
        "--metatile",
        type=parse_metatile_id,
        help="Specific metatile ID to inspect (for example 0x200 or 512)",
    )
    usages_parser.set_defaults(func=cmd_usages)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MapEditorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
