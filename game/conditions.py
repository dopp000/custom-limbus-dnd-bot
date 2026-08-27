from dataclasses import dataclass

# Recognized condition types for a Trigger's gate. Each type only reads
# the Condition fields it actually needs, the rest stay at their default.
CONDITION_TYPES = [
    "target_status", "target_hp_pct", "caster_sanity", "caster_status", "first_hit_of_round",
]

EFFECT_TYPES = ["bonus_power", "bonus_coin_power", "inflict_status", "sanity_gain"]

COMPARISONS = {
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}


@dataclass
class Condition:
    """One check against battle state. type is one of CONDITION_TYPES.

    target_status uses status_name/min_potency/min_count.
    target_hp_pct and caster_sanity use comparison/value.
    caster_status uses status_name only (presence check, no potency/count,
    this is meant for Poise/Charge, the self-buff resources).
    first_hit_of_round uses nothing, it just reads is_first_hit_of_round
    off the TriggerContext directly.
    """
    type: str
    status_name: str | None = None
    min_potency: int = 0
    min_count: int = 0
    comparison: str = "gte"
    value: int = 0


@dataclass
class TriggerContext:
    """Everything a Condition might need. target can be None for
    conditions that don't reference one (caster_sanity, caster_status,
    first_hit_of_round) -- any target-dependent condition just evaluates
    False rather than raising if target is missing.

    is_first_hit_of_round is set by the caller (cogs/battle.py's combat())
    based on resolution order, this module has no concept of round state
    on its own.
    """
    caster: "Fighter"
    target: "Fighter | None"
    battle: "Battle"
    is_first_hit_of_round: bool = False


def evaluate_condition(condition: Condition, context: TriggerContext) -> bool:
    """Pure evaluation, no side effects. Safe to call speculatively (e.g.
    for Hint display) against a target the caster hasn't actually
    declared on yet.
    """
    if condition.type == "target_status":
        if context.target is None:
            return False
        status = context.target.get_status(condition.status_name)
        if status is None:
            return False
        return status.potency >= condition.min_potency and status.count >= condition.min_count

    if condition.type == "target_hp_pct":
        if context.target is None or context.target.max_hp <= 0:
            return False
        pct = (context.target.hp / context.target.max_hp) * 100
        return COMPARISONS[condition.comparison](pct, condition.value)

    if condition.type == "caster_sanity":
        return COMPARISONS[condition.comparison](context.caster.sanity, condition.value)

    if condition.type == "caster_status":
        return context.caster.get_status(condition.status_name) is not None

    if condition.type == "first_hit_of_round":
        return context.is_first_hit_of_round

    return False


@dataclass
class Trigger:
    """A Condition plus what happens if it's true, attached to a Skill.

    hint_tier (1-3) is hand-set by whoever built the skill, the same
    spirit as manually tagging per-coin statuses in /battle addskill.
    There's no auto-classification, the skill's author decides how
    dangerous this trigger is meant to read as on the Hint line.
    """
    condition: Condition
    effect_type: str
    effect_value: int = 0
    status_name: str | None = None  # only used when effect_type == "inflict_status"
    status_count: int = 0  # only used when effect_type == "inflict_status"
    hint_tier: int = 1


def _parse_condition(token: str) -> Condition:
    parts = token.split(":")
    ctype = parts[0]
    if ctype == "target_status":
        # target_status:<name>:<min_potency>:<min_count>
        return Condition(type=ctype, status_name=parts[1], min_potency=int(parts[2]), min_count=int(parts[3]))
    if ctype in ("target_hp_pct", "caster_sanity"):
        # target_hp_pct:<gte|lte|eq>:<value>
        return Condition(type=ctype, comparison=parts[1], value=int(parts[2]))
    if ctype == "caster_status":
        # caster_status:<name>
        return Condition(type=ctype, status_name=parts[1])
    if ctype == "first_hit_of_round":
        return Condition(type=ctype)
    raise ValueError(f"Unknown condition type '{ctype}'")


def _parse_effect(token: str) -> tuple[str, int, str | None, int]:
    parts = token.split(":")
    etype = parts[0]
    if etype in ("bonus_power", "bonus_coin_power", "sanity_gain"):
        return etype, int(parts[1]), None, 0
    if etype == "inflict_status":
        # inflict_status:<name>:<potency>:<count>
        return etype, int(parts[2]), parts[1], int(parts[3])
    raise ValueError(f"Unknown effect type '{etype}'")


def parse_trigger_input(trigger_input: str) -> list["Trigger"]:
    """Parses the /battle addskill trigger_input mini-format.

    One trigger is 'condition|effect|hint:N', multiple triggers on the
    same skill are separated by ';'. 'none' means no triggers.

    Examples:
      'target_status:burn:1:0|bonus_power:8|hint:2'
      'caster_sanity:gte:20|sanity_gain:3|hint:1;first_hit_of_round|bonus_coin_power:2|hint:2'

    Raises ValueError with a descriptive message on malformed input, the
    caller is expected to catch this and show it back to the user
    ephemerally rather than letting it crash the command.
    """
    cleaned = trigger_input.strip()
    if not cleaned or cleaned.lower() == "none":
        return []

    triggers = []
    for entry in cleaned.split(";"):
        segments = entry.split("|")
        if len(segments) != 3:
            raise ValueError(
                f"Trigger '{entry}' needs exactly 3 parts separated by '|': "
                "condition|effect|hint:N"
            )
        condition_token, effect_token, hint_token = segments
        condition = _parse_condition(condition_token.strip())
        effect_type, effect_value, status_name, status_count = _parse_effect(effect_token.strip())

        hint_parts = hint_token.strip().split(":")
        if len(hint_parts) != 2 or hint_parts[0] != "hint":
            raise ValueError(f"Trigger '{entry}' needs a 'hint:N' tier at the end (N is 1-3).")
        hint_tier = int(hint_parts[1])
        if not (1 <= hint_tier <= 3):
            raise ValueError(f"Trigger '{entry}' hint tier must be 1, 2, or 3.")

        triggers.append(Trigger(
            condition=condition,
            effect_type=effect_type,
            effect_value=effect_value,
            status_name=status_name,
            status_count=status_count,
            hint_tier=hint_tier,
        ))
    return triggers