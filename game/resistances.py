DAMAGE_TYPES = ["slash", "blunt", "pierce"]

# Same 5 statuses as INFLICTABLE_STATUSES in game/statuses.py, listed here
# too so this module doesn't need to import statuses.py just for this list.
STATUS_RESISTANCE_TYPES = ["burn", "bleed", "tremor", "rupture", "sinking"]

ALL_RESISTANCE_TYPES = DAMAGE_TYPES + STATUS_RESISTANCE_TYPES

DEFAULT_RESISTANCES = {key: 0 for key in ALL_RESISTANCE_TYPES}


def apply_resistance(value: int, resistance_pct: int) -> int:
    """Reduces a value by a resistance percentage.

    resistance_pct isn't clamped going in, so values over 100 (fully
    negates and then some) and negative values (a weakness, taking MORE
    damage than normal) both work. The result itself is floored at 0,
    since resistance should never turn damage into healing.
    """
    reduced = value * (100 - resistance_pct) / 100
    return max(0, round(reduced))