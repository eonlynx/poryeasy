"""Choose palette colours when edited art uses colours outside a palette.

The planner only rewrites palette *colours*, never tile indices. Existing tiles
keep their indices, so a replaced slot recolours every tile that uses it, as
asked by the intelligent colour-selection policy.
"""

from __future__ import annotations

import colorsys
from collections import Counter
from dataclasses import dataclass

from .tileset import TilesetPair

RGB = tuple[int, int, int]
PALETTE_SLOTS = 15


@dataclass
class PalettePlan:
    bank: int
    colors: list[RGB]
    replaced_slots: list[int]
    substitutions: list[tuple[RGB, RGB]]
    protected_slots: list[int]


def _distance(a: RGB, b: RGB) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _hue_family(color: RGB) -> int:
    r, g, b = (value / 255.0 for value in color)
    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    if saturation < 0.12 or value < 0.06:
        return 12
    return min(11, int(hue * 12))


def _slot_usage(pair: TilesetPair, bank: int) -> list[int]:
    usage = [0] * 16
    for tileset in (pair.primary, pair.secondary):
        for metatile in tileset.metatiles:
            for entry in metatile:
                if entry.palette != bank:
                    continue
                pattern = tileset.pattern_at(entry.tile_index)
                for value in pattern:
                    usage[value & 0x0F] += 1
    return usage


def _representatives(desired: Counter[RGB], limit: int) -> list[RGB]:
    """Picks at most ``limit`` colours, keeping one per hue family first."""
    representatives: list[RGB] = []
    seen: set[RGB] = set()
    by_family: dict[int, list[tuple[RGB, int]]] = {}
    for color, count in desired.items():
        by_family.setdefault(_hue_family(color), []).append((color, count))
    families = sorted(
        by_family.items(),
        key=lambda item: -sum(count for _color, count in item[1]),
    )
    for _family, entries in families:
        color, _count = max(entries, key=lambda item: item[1])
        representatives.append(color)
        seen.add(color)
        if len(representatives) >= limit:
            return representatives
    for color, _count in desired.most_common():
        if color in seen:
            continue
        representatives.append(color)
        seen.add(color)
        if len(representatives) >= limit:
            break
    return representatives


def _representatives_by_usage(usage: Counter[RGB], limit: int) -> list[RGB]:
    """Keeps every hue family and spends the rest proportionally to pixel usage."""
    if not usage:
        return []
    by_family: dict[int, list[tuple[RGB, int]]] = {}
    family_usage: Counter[int] = Counter()
    for color, count in usage.items():
        family = _hue_family(color)
        by_family.setdefault(family, []).append((color, count))
        family_usage[family] += count
    families = sorted(
        family_usage,
        key=lambda family: (-family_usage[family], family),
    )[:limit]
    selected: list[RGB] = []
    family_selected: Counter[int] = Counter()
    for family in families:
        entries = sorted(by_family[family], key=lambda item: (-item[1], item[0]))
        selected.append(entries[0][0])
        family_selected[family] += 1

    remaining = limit - len(selected)
    if remaining > 0:
        total = sum(family_usage[family] for family in families) or 1
        quotas = {
            family: family_usage[family] * remaining / total for family in families
        }
        extras = {family: int(quotas[family]) for family in families}
        assigned = sum(extras.values())
        for family in sorted(
            families,
            key=lambda family: (-(quotas[family] - extras[family]), family),
        ):
            if assigned >= remaining:
                break
            extras[family] += 1
            assigned += 1
        for family in families:
            wanted = 1 + extras.get(family, 0)
            entries = sorted(by_family[family], key=lambda item: (-item[1], item[0]))
            for color, _count in entries:
                if family_selected[family] >= wanted:
                    break
                if color in selected:
                    continue
                selected.append(color)
                family_selected[family] += 1

    for color, _count in usage.most_common():
        if len(selected) >= limit:
            break
        if color not in selected:
            selected.append(color)
    return selected[:limit]


def build_index_remap(old_palette: list[RGB], new_palette: list[RGB]) -> dict[int, int]:
    """Maps each old index to the closest new colour, preferring its hue family."""
    mapping = {0: 0}
    for old_index in range(1, 16):
        old_color = old_palette[old_index]
        family = _hue_family(old_color)
        candidates = [
            new_index
            for new_index in range(1, 16)
            if _hue_family(new_palette[new_index]) == family
        ]
        if not candidates:
            candidates = list(range(1, 16))
        mapping[old_index] = min(
            candidates,
            key=lambda new_index: _distance(old_color, new_palette[new_index]),
        )
    return mapping


def plan_bank_colors(
    pair: TilesetPair,
    bank: int,
    desired: Counter[RGB],
    mode: str,
    old_usage: Counter[RGB] | None = None,
) -> PalettePlan:
    palette = list(pair.palette_for(bank)[:16])
    usage = _slot_usage(pair, bank)
    existing = palette[1:16]
    exact = {
        color: index + 1 for index, color in enumerate(existing) if color in desired
    }
    missing = Counter(
        {color: count for color, count in desired.items() if color not in exact}
    )
    if not missing and old_usage is None:
        return PalettePlan(bank, palette, [], [], [])

    replaceable = list(range(1, 16))
    protected: list[int] = []
    if mode == "smart" and old_usage is not None:
        combined = Counter(old_usage)
        combined.update(desired)
        representatives = _representatives_by_usage(combined, PALETTE_SLOTS)
        selected: list[RGB] = []
        for color in representatives:
            if color not in selected:
                selected.append(color)
        for color, _count in combined.most_common():
            if len(selected) >= PALETTE_SLOTS:
                break
            if color not in selected:
                selected.append(color)
        while len(selected) < PALETTE_SLOTS:
            selected.append(selected[-1] if selected else palette[1])
        new_palette = [palette[0], *selected[:PALETTE_SLOTS]]
        replaced_slots = [
            slot for slot in range(1, 16) if new_palette[slot] != palette[slot]
        ]
        substitutions: list[tuple[RGB, RGB]] = []
        for color in desired:
            if color in selected:
                continue
            nearest = min(
                selected,
                key=lambda candidate: _distance(color, candidate),
            )
            substitutions.append((color, nearest))
        return PalettePlan(
            bank,
            new_palette,
            replaced_slots,
            substitutions,
            protected,
        )

    if mode == "smart":
        family_slots: dict[int, list[int]] = {}
        total_usage = sum(usage[1:16]) or 1
        for slot in range(1, 16):
            family = _hue_family(palette[slot])
            family_slots.setdefault(family, []).append(slot)
        for slots in family_slots.values():
            slots.sort(key=lambda slot: usage[slot], reverse=True)
            if slots and usage[slots[0]] / total_usage >= 0.005:
                protected.append(slots[0])
        replaceable = [slot for slot in replaceable if slot not in protected]
        replaceable.sort(key=lambda slot: (usage[slot], slot))

    limit = len(replaceable)
    representatives = _representatives(missing, limit)
    substitutions: list[tuple[RGB, RGB]] = []

    if mode == "replace":
        representatives = _representatives(desired, PALETTE_SLOTS)
        exact_slots = set(exact.values())
        replaceable = [slot for slot in range(1, 16) if slot not in exact_slots]
        protected = []

    assigned_slots: list[int] = []
    new_palette = list(palette)
    for color in representatives:
        if color in exact:
            continue
        if not replaceable:
            nearest_slot = min(
                range(1, 16),
                key=lambda slot: _distance(palette[slot], color),
            )
            substitutions.append((color, palette[nearest_slot]))
            continue
        slot = min(
            replaceable,
            key=lambda candidate: (
                usage[candidate],
                _distance(palette[candidate], color),
                candidate,
            ),
        )
        replaceable.remove(slot)
        new_palette[slot] = color
        assigned_slots.append(slot)

    for color, count in missing.most_common():
        if color in representatives:
            continue
        nearest = min(
            range(1, 16),
            key=lambda slot: _distance(new_palette[slot], color),
        )
        substitutions.append((color, new_palette[nearest]))

    return PalettePlan(
        bank=bank,
        colors=new_palette,
        replaced_slots=sorted(assigned_slots),
        substitutions=substitutions,
        protected_slots=protected,
    )


def apply_palette_plan(pair: TilesetPair, plan: PalettePlan) -> None:
    owner, local = pair.palette_owner(plan.bank)
    if owner is None or local >= len(owner.palettes):
        return
    owner.palettes[local] = plan.colors
    owner.dirty_palette_indices.add(local)
    pair.palette_rewrites.append(plan)
