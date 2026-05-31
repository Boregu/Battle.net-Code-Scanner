import re

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
    ("Diablo IV", [r"diablo\s*iv", r"action rpg"]),
    ("Diablo III", [r"diablo\s*iii"]),
    ("Diablo Immortal", [r"diablo immortal"]),
    ("Overwatch", [
        r"overwatch\s*2?",
        r"overwatch coins",
        r"overwatch[\u00ae\u2122]?\s*coins",
        r"first[- ]person shooter",
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


def detect_game(name: str | None, page_text: str = "", genre: str | None = None) -> str | None:
    haystack = f"{name or ''} {page_text} {genre or ''}"
    for game, patterns in GAME_RULES:
        for pattern in patterns:
            if re.search(pattern, haystack, re.IGNORECASE):
                return game
    return None
