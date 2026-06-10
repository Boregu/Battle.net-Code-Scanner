"""Catalog product tags for browse filters and what's-included copy."""

from __future__ import annotations

import re
from typing import Any

CATALOG_TAGS_MARKER = "catalog_tags:"
CATALOG_TAGS_RE = re.compile(r"^catalog_tags:\s*(.+)$", re.I | re.M)

TAG_LABELS: dict[str, str] = {
    "coin_pack": "Coin pack",
    "hero_skin": "Hero skin",
    "bundle": "Bundle",
    "emote": "Emote",
    "spray": "Spray",
    "voice_line": "Voice line",
    "hero_offer": "Hero offer",
    "real_money": "Real money",
    "commander": "Commander",
    "war_chest": "War chest",
    "campaign": "Campaign",
    "sc2_cosmetic": "Cosmetic",
    "other": "Other",
}

OVERWATCH_TAG_OPTIONS = [
    "hero_skin",
    "bundle",
    "emote",
    "spray",
    "voice_line",
    "hero_offer",
    "coin_pack",
    "real_money",
    "other",
]

STARCRAFT_TAG_OPTIONS = [
    "commander",
    "war_chest",
    "bundle",
    "campaign",
    "sc2_cosmetic",
    "real_money",
    "other",
]

OVERWATCH_FILTER_TO_TAGS: dict[str, list[str]] = {
    "hero_skins": ["hero_skin"],
    "bundles": ["bundle"],
    "emotes": ["emote"],
    "sprays": ["spray"],
    "voice_lines": ["voice_line"],
    "hero_offers": ["hero_offer"],
    "coins": ["coin_pack"],
    "real_money": ["real_money"],
    "other": ["other"],
}

STARCRAFT_FILTER_TO_TAGS: dict[str, list[str]] = {
    "commanders": ["commander"],
    "war_chests": ["war_chest"],
    "bundles": ["bundle"],
    "campaigns": ["campaign"],
    "skins": ["sc2_cosmetic"],
    "real_money": ["real_money"],
    "other": ["other"],
}


def parse_catalog_tags(raw_notes: str | None) -> list[str]:
    if not raw_notes:
        return []
    match = CATALOG_TAGS_RE.search(raw_notes)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def write_catalog_tags(raw_notes: str | None, tags: list[str]) -> str | None:
    cleaned = CATALOG_TAGS_RE.sub("", raw_notes or "").strip()
    unique = []
    for tag in tags:
        tag = tag.strip()
        if tag and tag not in unique:
            unique.append(tag)
    if not unique:
        return cleaned or None
    line = f"{CATALOG_TAGS_MARKER} {', '.join(unique)}"
    return f"{line}\n{cleaned}".strip() if cleaned else line


def is_real_money_price(price: str | None) -> bool:
    if not price:
        return False
    trimmed = price.strip()
    if re.match(r"^[$€£¥₩]", trimmed):
        return True
    if re.match(r"^(EUR|USD|GBP|CHF|SEK|NOK|DKK|PLN|CZK|TWD|KRW)\s*-\s*", trimmed, re.I):
        return True
    return False


def is_overwatch_coin_price(price: str | None) -> bool:
    if not price:
        return False
    return bool(re.search(r"\boverwatch\s*coins\b", price, re.I))


def parse_coin_amount(price: str | None) -> int | None:
    if not price:
        return None
    match = re.match(r"^([\d,]+)\s+(?:Overwatch[\u00ae\u2122\s]*)?Coins\b", price.strip(), re.I)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _is_coin_pack_product(name: str) -> bool:
    lower = name.lower()
    if re.match(r"^\d[\d,]*\s+overwatch\s*coins\s*$", lower):
        return True
    if re.search(r"overwatch\s*:\s*\d[\d,]*\s*(overwatch\s*)?coins", lower):
        return True
    return False


def _item_context(item: dict[str, Any]) -> str:
    parts = [
        item.get("name") or "",
        item.get("price") or "",
        item.get("raw_notes") or "",
    ]
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def _classify_overwatch_coin_cosmetic(name: str, price: str | None, ctx: str) -> str:
    lower = name.lower()
    if re.search(r"\bvoice line\b", ctx):
        return "voice_line"
    if name.strip().startswith("..."):
        return "voice_line"
    if re.search(r"\bspray\b", lower) or re.search(r"\bspray\b", ctx):
        return "spray"
    if re.search(r"\bemote\b", lower) or re.search(r"\b(emote|highlight intro|victory pose)\b", ctx):
        return "emote"
    if re.search(r"\b(player icon|name card|charm|weapon charm)\b", lower + " " + ctx):
        return "spray"
    amount = parse_coin_amount(price)
    if amount is not None:
        if amount <= 25:
            return "spray"
        if amount <= 75:
            return "voice_line"
        if amount <= 250:
            return "spray"
        if amount <= 600:
            return "emote"
        return "hero_skin"
    return "other"


def detect_catalog_tags(item: dict[str, Any]) -> list[str]:
    name = (item.get("name") or "").strip()
    game = (item.get("game") or "").strip()
    price = item.get("price")
    lower = name.lower()
    ctx = _item_context(item)
    tags: list[str] = []

    if game == "Overwatch":
        if _is_coin_pack_product(name):
            return ["coin_pack"]
        if is_real_money_price(price):
            tags.append("real_money")
        if "bundle" in lower or "bundle" in ctx:
            tags.append("bundle")
        elif re.search(r"legendary offer|mythic weapon|hero offer", lower + " " + ctx):
            tags.append("hero_offer")
        elif re.search(r"\b(hero skin|skin for|weapon skin)\b", ctx):
            tags.append("hero_skin")
        elif " - " in name and is_overwatch_coin_price(price):
            tags.append("hero_skin")
        elif is_overwatch_coin_price(price):
            tags.append(_classify_overwatch_coin_cosmetic(name, price, ctx))
        elif re.search(r"battle pass|edition|upgrade|watchpoint", lower + " " + ctx):
            tags.append("other")
        else:
            tags.append("other")
        return tags

    if game == "StarCraft II":
        if is_real_money_price(price):
            tags.append("real_money")
        if re.search(r"\bcommander\b", lower) or re.search(r"co-?op commander|commander can only", ctx):
            tags.append("commander")
        elif re.search(r"\bwar chest\b", lower + " " + ctx):
            tags.append("war_chest")
        elif re.search(r"\bbundle\b", lower):
            tags.append("bundle")
        elif re.search(
            r"nova covert ops|mission pack|wings of liberty|heart of the swarm|legacy of the void|campaign|anniversary collection|digital deluxe|expansion pack",
            lower + " " + ctx,
        ):
            tags.append("campaign")
        elif re.search(
            r"\b(skin|announcer|portrait|emoticon|emoji|banner|decal|unit skin|skin pack|co-op.*skin)\b",
            lower + " " + ctx,
        ):
            tags.append("sc2_cosmetic")
        else:
            tags.append("other")
        return tags

    if is_real_money_price(price):
        tags.append("real_money")
    if tags:
        return tags
    return ["other"]


def extract_checkout_summary(page_text: str | None) -> str | None:
    if not page_text:
        return None
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if re.search(r"you are purchasing|product summary", line, re.I):
            chunk = lines[index : min(index + 6, len(lines))]
            return " ".join(chunk)[:400]
    for index, line in enumerate(lines):
        if line.lower() == "you are purchasing" and index + 1 < len(lines):
            return lines[index + 1][:200]
    return None


def apply_auto_catalog_tags(entry: dict[str, Any]) -> dict[str, Any]:
    if not entry.get("valid"):
        return entry
    game = (entry.get("game") or "").strip()
    if game not in ("Overwatch", "StarCraft II"):
        return entry
    if parse_catalog_tags(entry.get("raw_notes")):
        return entry
    tags = detect_catalog_tags(entry)
    if not tags:
        return entry
    out = dict(entry)
    out["raw_notes"] = write_catalog_tags(out.get("raw_notes"), tags)
    return out


def preserve_catalog_tags(existing_notes: str | None, new_notes: str | None) -> str | None:
    tags = parse_catalog_tags(existing_notes)
    if not tags:
        return new_notes
    return write_catalog_tags(new_notes, tags)


def needs_tag_review(item: dict[str, Any]) -> bool:
    if not item.get("valid"):
        return False
    game = (item.get("game") or "").strip()
    if game not in ("Overwatch", "StarCraft II"):
        return False
    tags = parse_catalog_tags(item.get("raw_notes"))
    if not tags:
        return True
    return tags == ["other"]


def resolve_catalog_tags(item: dict[str, Any]) -> list[str]:
    manual = parse_catalog_tags(item.get("raw_notes"))
    if manual:
        return manual
    if not item.get("valid"):
        return []
    return detect_catalog_tags(item)


def tag_labels(tags: list[str]) -> list[str]:
    return [TAG_LABELS.get(tag, tag.replace("_", " ").title()) for tag in tags]


def tag_options_for_game(game: str | None) -> list[dict[str, str]]:
    if game == "Overwatch":
        ids = OVERWATCH_TAG_OPTIONS
    elif game == "StarCraft II":
        ids = STARCRAFT_TAG_OPTIONS
    else:
        ids = sorted(TAG_LABELS.keys())
    return [{"id": tag, "label": TAG_LABELS[tag]} for tag in ids]


def item_matches_filter_category(item: dict[str, Any], category: str) -> bool:
    if not category:
        return True
    tags = resolve_catalog_tags(item)
    game = item.get("game") or ""
    mapping = OVERWATCH_FILTER_TO_TAGS if game == "Overwatch" else STARCRAFT_FILTER_TO_TAGS
    wanted = mapping.get(category, [category])
    return any(tag in tags for tag in wanted)


def get_product_includes(name: str, game: str | None, tags: list[str] | None = None) -> str:
    n = re.sub(r"[®™]", "", name or "").strip()
    g = game or ""
    lower = n.lower()
    tag_set = set(tags or [])

    pack_match = re.search(
        r"(\d+)\s+(Classic|Grand Tournament|Goblins vs Gnomes|Whispers of the Old Gods|Mean Streets of Gadgetzan|Journey to Un'Goro|The Boomsday Project|Rise of Shadows|Descent of Dragons|Ashes of Outland|Scholomance Academy|Madness at the Darkmoon Faire|Forged in the Barrens|United in Stormwind|Fractured in Alterac Valley|Voyage to the Sunken City|Murder at Castle Nathria|Legacy|Standard)\s+Packs?",
        n,
        re.I,
    )
    if pack_match:
        return f"Includes {pack_match.group(1)} Hearthstone {pack_match.group(2)} card packs for your account."

    if "coin_pack" in tag_set or _is_coin_pack_product(n):
        coins = re.match(r"^([\d,]+)", n)
        return (
            f"Includes {coins.group(1)} Overwatch Coins for the in-game shop."
            if coins
            else "Includes Overwatch Coins for the in-game shop."
        )

    if "commander" in tag_set or (re.search(r"\bcommander\b", lower) and "StarCraft" in g):
        cmd = re.search(r"Commander:?\s*(.+)$", n, re.I)
        return (
            f"Includes the {cmd.group(1).strip()} co-op commander for StarCraft II cooperative missions."
            if cmd
            else "Includes a playable co-op commander for StarCraft II."
        )

    if "war_chest" in tag_set or "war chest" in lower:
        return "Includes a StarCraft II War Chest with cosmetic and progression rewards."

    if "campaign" in tag_set:
        if "nova covert ops" in lower or "mission pack" in lower:
            num = re.search(r"pack\s*(\d+)", lower)
            return (
                f"Includes Nova Covert Ops Mission Pack {num.group(1)} — standalone story missions."
                if num
                else "Includes Nova Covert Ops story mission content for StarCraft II."
            )
        if "wings of liberty" in lower:
            return "Includes StarCraft II: Wings of Liberty — the base game campaign."
        if "heart of the swarm" in lower:
            return "Includes StarCraft II: Heart of the Swarm — the zerg campaign expansion."
        if "legacy of the void" in lower:
            return "Includes StarCraft II: Legacy of the Void — the protoss campaign expansion."
        return "Includes StarCraft II campaign or mission content."

    if "sc2_cosmetic" in tag_set:
        if "announcer" in lower:
            who = re.search(r"Announcer:?\s*(.+)$", n, re.I)
            return (
                f"Includes the {who.group(1).strip()} announcer pack for StarCraft II."
                if who
                else "Includes an announcer pack for StarCraft II."
            )
        if "skin" in lower:
            return f"Includes the {n} cosmetic for StarCraft II."
        return f"Includes the {n} cosmetic for StarCraft II."

    if "voice_line" in tag_set:
        return f"Includes the {n} voice line for Overwatch."

    if "emote" in tag_set:
        return f"Includes the {n} emote for Overwatch."

    if "spray" in tag_set:
        return f"Includes the {n} spray for Overwatch."

    if "hero_skin" in tag_set:
        return f"Includes the {n} cosmetic for Overwatch."

    if "hero_offer" in tag_set:
        return f"Includes the {n} hero offer for Overwatch."

    if "bundle" in tag_set:
        if "StarCraft" in g:
            return "Includes multiple StarCraft II campaigns and content bundled together."
        if "Overwatch" in g:
            return f"Includes the {n} bundle for Overwatch."
        return f"Includes the {n} bundle."

    if re.search(r"\bplatinum\b", lower) and "Diablo" in g:
        amt = re.match(r"^([\d,]+)", n)
        return (
            f"Includes {amt.group(1)} Platinum for Diablo in-game purchases."
            if amt
            else "Includes Platinum for Diablo in-game purchases."
        )

    if "subscription" in lower:
        months = re.search(r"(\d+)\s*Month", n, re.I)
        return (
            f"Includes {months.group(1)} month{'s' if months.group(1) != '1' else ''} of World of Warcraft game time."
            if months
            else "Includes World of Warcraft subscription game time."
        )

    if re.search(r"\bwow\s*token\b", lower):
        return "Includes one WoW Token — redeem for 30 days of game time or sell on the Auction House."

    if re.search(r"\bbattletag change\b", lower):
        return "Includes a one-time BattleTag name change on your Battle.net account."

    if re.search(r"\bbattle chest\b", lower):
        return "Includes the complete game bundle (base game and expansion content)."

    if re.search(r"\breaper of souls\b", lower):
        return "Includes Diablo III: Reaper of Souls expansion content."

    if re.search(r"\bdiablo\s*iii\b", lower) and not re.search(r"reaper|platinum|upgrade", lower):
        return "Includes Diablo III — the full action RPG base game."

    if g and n:
        return f"Battle.net checkout product for {g} — open on Battle.net for live purchase details."

    return "Battle.net checkout product — open on Battle.net for live details and purchase."
