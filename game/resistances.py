DAMAGE_TYPES = ["slash", "blunt", "pierce"]

# Same 5 statuses as INFLICTABLE_STATUSES in game/statuses.py, listed here
# too so this module doesn't need to import statuses.py just for this list.
STATUS_RESISTANCE_TYPES = ["burn", "bleed", "tremor", "rupture", "sinking"]

ALL_RESISTANCE_TYPES = DAMAGE_TYPES + STATUS_RESISTANCE_TYPES

DEFAULT_RESISTANCES = {key: 0 for key in ALL_RESISTANCE_TYPES}


def apply_resistance(value: int, resistance_pct: int) -> int:
    """Reduces a value by a resistance percentage, using Limbus
    Company's own asymmetric formula instead of a flat linear
    reduction: a WEAKNESS (resistance_pct negative, meaning MORE damage
    taken) applies at full face value, but an actual RESISTANCE
    (resistance_pct positive) is only HALF as effective as its face
    value suggests. This matches the real game's philosophy that being
    weak to something hurts fully, but resisting something only helps
    partially -- resistance and weakness are not mirror images of each
    other.

    Concretely: -50 resistance_pct (50% weakness) still means 1.5x
    damage, same as a naive linear formula would give. But +50
    resistance_pct (nominally "50% resistance") only actually reduces
    damage by 25%, not 50% -- it now takes +200 resistance_pct to reach
    a full 100% reduction (0 damage), not +100. So once resistance_pct
    is positive, it's no longer literally "the percent damage reduced";
    it's better read as "how much resistance is being applied," with
    the real reduction being half that.

    resistance_pct still isn't clamped going in on either side. The
    result is still floored at 0, since resistance should never turn
    damage into healing.
    """
    if resistance_pct <= 0:
        multiplier = 1 - (resistance_pct / 100)
    else:
        multiplier = 1 - (resistance_pct / 200)
    return max(0, round(value * multiplier))