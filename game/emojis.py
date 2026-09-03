# Discord custom emoji IDs, keyed by name. Fill any remaining None values
# in once you've uploaded that icon as a custom emoji in your server.
#
# HOW TO GET AN EMOJI'S ID:
# In any channel where the emoji is available, type a backslash right
# before it, e.g.  \:Burn:  then send the message. Discord expands it
# to the full tag, like <:Burn:1234567890123456789>, copy the number
# from there into the dict below.
#
# Anything left as None falls back to plain ":Name:" text instead of a
# real icon, so the bot keeps working while you fill these in gradually.

STATUS_EMOJI_IDS: dict[str, int | None] = {
    "burn": 1538062892094460024,
    "rupture": 1538063110001401906,
    "bleed": 1538062845793533992,
    "tremor": 1538063202930393179,
    "charge": 1538062873241059338,
    "tremor_burst": 1538063181967130624,  # not wired to a mechanic yet, ready for when Tremor Burst is built
    "sinking": 1538063130729512962,
    "poise": 1538062820300816384,
    "evasion": 1538065517896671252,  # uploaded as "Evade", not "Evasion" -- see _STATUS_DISPLAY_NAMES below
    "counter": 1543225884310245406,
    # Same "reserved, not wired to a mechanic yet" pattern as tremor_burst
    # above -- upload the icon and fill in the ID whenever the matching
    # system actually gets built (see README's Roadmap section).
    "shield": 1544679323192266904,   # for Guard's Shield HP, once Guard exists
    "guard": None,                    # the Guard skill type itself, once it exists
    "stagger": None,                  # the Stagger status icon itself, once uploaded
    # Panic and Low Morale intentionally share one icon -- same as how
    # they share design (see README) -- rather than needing two uploads.
    "panic": 1544676724862623834,
    "low_morale": 1544676724862623834,
}

# Internal keys stay semantic (base/tails/heads) so the rest of the
# codebase never has to know or care what the emoji is actually named on
# Discord's side. Only this file needs to track that mapping.
COIN_EMOJI_IDS: dict[str, int | None] = {
    "base": 1538062791339147264,   # displayed on Discord as "Coin"
    "tails": 1538062765980131370,  # displayed on Discord as "CoinIcon"
    "heads": 1538062747609206845,  # displayed on Discord as "CoinIconGlow"
}
_COIN_DISPLAY_NAMES = {"base": "Coin", "tails": "CoinIcon", "heads": "CoinIconGlow"}

DAMAGE_TYPE_EMOJI_IDS: dict[str, int | None] = {
    "slash": 1538065502210097204,
    "blunt": 1538065584602751019,
    "pierce": 1538065614739079198,
}

STAT_EMOJI_IDS: dict[str, int | None] = {
    "speed": 1538062414665486457,
    "hp": 1538062502896607232,
    "sanity": 1544677030245703771,
}
_STAT_DISPLAY_NAMES = {"speed": "SPEED", "hp": "HP", "sanity": "Sanity"}

# Severity tiers for the small "-# hint" line under a fighter's name on the
# battle-view embed. 1 = mild, 2 = dangerous, 3 = highly dangerous.
# Which specific statuses/passives map to which tier still needs a real
# classification pass. These are just the icons themselves for now.
HINT_EMOJI_IDS: dict[int, int | None] = {
    1: 1538077024319442964,
    2: 1538077039666659388,
    3: 1538077057425215569,
}

SKILL_SLOT_EMOJI_IDS: dict[int, int | None] = {
    1: 1538062724368437308,
    2: 1538062706542903306,
    3: 1538062687362097282,
    4: 1538062570856906842,
    5: 1538062524963098655,
}

# Animated emoji use a different tag format: <a:Name:ID> instead of <:Name:ID>.
COIN_ROLL_EMOJI_ID: int | None = 1538091919995965471


def emoji_tag(display_name: str, emoji_id: int | None, animated: bool = False) -> str:
    """Builds a real Discord custom emoji tag if an ID is known, otherwise
    falls back to plain ":Name:" text (visible as literal characters,
    not an icon, until the ID gets filled in above).

    Animated emoji need the "a" prefix: <a:Name:ID> instead of <:Name:ID>.
    """
    if emoji_id is None:
        return f":{display_name}:"
    prefix = "a" if animated else ""
    return f"<{prefix}:{display_name}:{emoji_id}>"


def status_emoji(status_name: str) -> str:
    key = status_name.lower()
    # Internal status keys don't always match the emoji's actual name on
    # Discord -- the engine calls this status "evasion" everywhere (it
    # reads better next to poise/charge/counter), but the uploaded emoji
    # itself is named "Evade". Only entries that DON'T just match
    # key.capitalize() need to be listed here.
    display_names = {"evasion": "Evade"}
    display_name = display_names.get(key, status_name.capitalize())
    return emoji_tag(display_name, STATUS_EMOJI_IDS.get(key))


def coin_emoji(face: str) -> str:
    """face is 'base', 'heads', or 'tails'."""
    key = face.lower()
    return emoji_tag(_COIN_DISPLAY_NAMES[key], COIN_EMOJI_IDS.get(key))


def coin_roll_emoji() -> str:
    """The animated "currently rolling" coin, for the coin-by-coin reveal."""
    return emoji_tag("CoinRoll", COIN_ROLL_EMOJI_ID, animated=True)


def damage_type_emoji(damage_type: str) -> str:
    """damage_type is 'slash', 'blunt', or 'pierce'."""
    key = damage_type.lower()
    return emoji_tag(damage_type.capitalize(), DAMAGE_TYPE_EMOJI_IDS.get(key))


def stat_emoji(stat_name: str) -> str:
    """stat_name is 'speed', 'hp', or 'sanity'."""
    key = stat_name.lower()
    return emoji_tag(_STAT_DISPLAY_NAMES[key], STAT_EMOJI_IDS.get(key))


def hint_emoji(tier: int) -> str:
    """tier is 1 (mild), 2 (dangerous), or 3 (highly dangerous)."""
    return emoji_tag(f"Hint_{tier}", HINT_EMOJI_IDS.get(tier))


def skill_slot_emoji(slot_number: int) -> str:
    """slot_number is 1 through 5."""
    return emoji_tag(f"SkillSlot{slot_number}", SKILL_SLOT_EMOJI_IDS.get(slot_number))