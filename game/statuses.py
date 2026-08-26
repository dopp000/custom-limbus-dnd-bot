from dataclasses import dataclass

# The 5 target-facing statuses that can be inflicted on an opponent and
# resisted. Poise and Charge are deliberately excluded: they're self-buff
# resources a fighter builds for themselves, not something dealt TO a
# target, so "resisting" them doesn't make sense.
INFLICTABLE_STATUSES = ["burn", "bleed", "tremor", "rupture", "sinking"]


@dataclass
class StatusInstance:
    """One status effect currently active on a fighter.

    Potency = damage/magnitude dealt each time it triggers.
    Count = how many triggers remain before it's fully gone.
    """
    name: str
    potency: int
    count: int


def decay_after_trigger(current: StatusInstance) -> StatusInstance:
    """Called when a status's trigger condition fires (e.g. Rupture
    triggers when its owner is hit). The caller is responsible for
    actually dealing current.potency as damage; this function only
    handles what happens to the stack afterward.

    Decrements Count by 1. If Count reaches 0, the stack is fully
    consumed: Potency resets to 0 too, rather than lingering at 0 count
    with stale Potency.
    """
    new_count = current.count - 1
    if new_count <= 0:
        return StatusInstance(name=current.name, potency=0, count=0)
    return StatusInstance(name=current.name, potency=current.potency, count=new_count)


def apply_status(current: StatusInstance | None, name: str, added_potency: int, added_count: int) -> StatusInstance:
    """Layers a new application on top of whatever is currently there.

    If this application coincides with a trigger (e.g. a hit that both
    triggers existing Rupture AND inflicts new Rupture at once), call
    decay_after_trigger() FIRST and pass its result in as `current`, so
    the new stack layers on top of the already-decayed one, not the
    stale pre-trigger one.

    A stack can never have Count > 0 with Potency <= 0 (a status with
    nothing to deal doesn't make sense), so if the combined Potency would
    be zero or less (e.g. a "no potency, +N count" effect applied to an
    empty stack), it floors to 1.
    """
    base_potency = current.potency if current else 0
    base_count = current.count if current else 0

    new_count = base_count + added_count
    new_potency = base_potency + added_potency
    if new_count > 0 and new_potency <= 0:
        new_potency = 1

    return StatusInstance(name=name, potency=new_potency, count=new_count)