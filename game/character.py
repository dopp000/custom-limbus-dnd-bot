import json
import os
from dataclasses import dataclass

CHARACTERS_DIR = "data/characters"


@dataclass
class Character:
    """A persistent, player-built character. Survives bot restarts, unlike
    a Battle's Fighter objects, which only exist for one fight.
    """

    owner_id: int  # Discord user ID of whoever created this character
    name: str
    avatar_url: str | None = None
    hp: int = 100
    max_hp: int = 100
    sp: int = 15
    speed: int = 10
    power: int = 6

    def to_dict(self) -> dict:
        return {
            "owner_id": self.owner_id,
            "name": self.name,
            "avatar_url": self.avatar_url,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "sp": self.sp,
            "speed": self.speed,
            "power": self.power,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Character":
        return cls(**data)


def _character_path(name: str) -> str:
    safe_name = name.lower().replace(" ", "_")
    return os.path.join(CHARACTERS_DIR, f"{safe_name}.json")


def save_character(character: Character):
    os.makedirs(CHARACTERS_DIR, exist_ok=True)
    with open(_character_path(character.name), "w") as f:
        json.dump(character.to_dict(), f, indent=2)


def load_character(name: str) -> Character | None:
    path = _character_path(name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return Character.from_dict(data)


def character_exists(name: str) -> bool:
    return os.path.exists(_character_path(name))


def delete_character(name: str) -> bool:
    path = _character_path(name)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def list_characters_by_owner(owner_id: int) -> list[Character]:
    """Scans every saved character file and returns the ones this user owns.

    Fine at small scale, this reads every character file in the folder on
    each call. If this ever needs to scale to hundreds of characters, an
    index file (or a real database) would replace this linear scan.
    """
    if not os.path.isdir(CHARACTERS_DIR):
        return []
    owned = []
    for filename in os.listdir(CHARACTERS_DIR):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(CHARACTERS_DIR, filename)) as f:
            data = json.load(f)
        if data.get("owner_id") == owner_id:
            owned.append(Character.from_dict(data))
    return owned