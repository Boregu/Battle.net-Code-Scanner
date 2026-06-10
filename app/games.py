import re

# Optional ® ™ © between words (Battle.net product names).
_BLIZZ = r"[\s®™©\u00ae\u2122]*"

# Checked against product name first — most reliable.
NAME_GAME_RULES: list[tuple[str, list[str]]] = [
    ("StarCraft II", [rf"starcraft{_BLIZZ}ii\b", rf"starcraft{_BLIZZ}2\b", r"commander:\s", r"war chest", r"announcer:", r"carbot"]),
    ("World of Warcraft", [r"world of warcraft", r"\bwow\b", r"game time", r"wow[\u00ae®]?\s*token"]),
    ("Diablo II", [rf"diablo{_BLIZZ}ii\b", r"lord of destruction", rf"diablo{_BLIZZ}2\b"]),
    ("Diablo III", [rf"diablo{_BLIZZ}iii\b", r"reaper of souls", rf"diablo{_BLIZZ}3\b"]),
    ("Diablo IV", [rf"diablo{_BLIZZ}iv\b", rf"diablo{_BLIZZ}4\b"]),
    ("Diablo Immortal", [r"diablo immortal"]),
    ("Overwatch", [r"overwatch", r"overwatch coins"]),
    ("Hearthstone", [r"hearthstone", r"\bp packs\b", r"card pack"]),
    ("Heroes of the Storm", [r"heroes of the storm"]),
    ("Call of Duty", [r"call of duty", r"cod points", r"modern warfare", r"black ops", r"warzone"]),
    ("Warcraft Rumble", [r"warcraft rumble"]),
]

# Fallback when name alone is ambiguous — page text / genre.
GAME_RULES: list[tuple[str, list[str]]] = [
    ("StarCraft II", [
        r"starcraft\s*(?:®|®\s*)?ii",
        r"starcraft ii",
        r"war chest",
        r"announcer:",
        r"\bzerg\b",
        r"\bprotoss\b",
        r"\bterran\b",
        r"real[- ]time strategy",
    ]),
    ("World of Warcraft", [
        r"world of warcraft",
        r"sprite darter",
        r"blossoming ancient",
        r"sylverian dreamer",
        r"iron skyreaver",
        r"steamscale incinerator",
        r"game time",
        r"\bwow\b",
        r"requires world of warcraft",
        r"massively multiplayer",
        r"\bmmorpg\b",
        r"\bmount\b",
        r"\bpet\b",
        r"blizzard gear store",
    ]),
    ("Diablo II", [r"diablo\s*ii\b", r"lord of destruction"]),
    ("Diablo III", [r"diablo\s*iii", r"reaper of souls"]),
    ("Diablo IV", [r"diablo\s*iv\b"]),
    ("Diablo Immortal", [r"diablo immortal"]),
    ("Overwatch", [
        r"overwatch\s*2?",
        r"overwatch coins",
        r"overwatch[\u00ae\u2122]?\s*coins",
        r"first[- ]person shooter",
        r"\b(?:ana|ashe|baptiste|bastion|brigitte|cassidy|d\.?\s*va|doomfist|echo|genji|hanzo|illari|junker queen|junkrat|kiriko|lifeweaver|l[úu]cio|mercy|mei|moira|orisa|pharah|ramattra|reaper|reinhardt|roadhog|sigma|sojourn|soldier:\s*76|sombra|symmetra|torbj[öo]rn|tracer|venture|widowmaker|winston|wrecking ball|zarya|zenyatta)\b",
    ]),
    ("Hearthstone", [r"hearthstone", r"card pack", r"card game"]),
    ("Heroes of the Storm", [r"heroes of the storm", r"moba"]),
    ("Call of Duty", [
        r"call of duty",
        r"\bcp\b",
        r"cod points",
        r"endowment",
        r"\bvanguard\b",
        r"modern warfare",
        r"black ops",
        r"warzone",
    ]),
    ("Warcraft Rumble", [r"warcraft rumble"]),
]


def _game_from_genre(genre: str | None, name: str | None = None) -> str | None:
    if not genre:
        return None
    gl = genre.lower()
    if "real-time strategy" in gl:
        return "StarCraft II"
    if "strategy card" in gl:
        return "Hearthstone"
    if "massively multiplayer" in gl or "mmorpg" in gl:
        return "World of Warcraft"
    if "first-person shooter" in gl or "first person shooter" in gl:
        return "Overwatch"
    if "action rpg" in gl and name:
        if re.search(rf"diablo{_BLIZZ}iv\b", name, re.I):
            return "Diablo IV"
        if re.search(rf"diablo{_BLIZZ}ii\b", name, re.I):
            return "Diablo II"
        if re.search(r"diablo", name, re.I):
            return "Diablo III"
    return None


def detect_game(name: str | None, page_text: str = "", genre: str | None = None) -> str | None:
    if name:
        for game, patterns in NAME_GAME_RULES:
            for pattern in patterns:
                if re.search(pattern, name, re.IGNORECASE):
                    return game

    from_genre = _game_from_genre(genre, name)
    if from_genre:
        return from_genre

    haystack = f"{name or ''} {page_text}"
    for game, patterns in GAME_RULES:
        for pattern in patterns:
            if re.search(pattern, haystack, re.IGNORECASE):
                return game
    return None
