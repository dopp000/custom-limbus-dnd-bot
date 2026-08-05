STATUS_COLORS = {
    "burn": 0xA0392A,
    "bleed": 0xBB511E,
    "tremor": 0xD98600,
    "rupture": 0x5F7D27,
    "sinking": 0x2C5F67,
    "poise": 0x17497D,
    "charge": 0x734687,
}

DEFAULT_COLOR = 0x99AAB5


def get_status_color(status_name: str | None) -> int:
    """Returns the embed color for a status name, or the default gray if
    there's no active status or the name isn't recognized.
    """
    if status_name is None:
        return DEFAULT_COLOR
    return STATUS_COLORS.get(status_name.lower(), DEFAULT_COLOR)