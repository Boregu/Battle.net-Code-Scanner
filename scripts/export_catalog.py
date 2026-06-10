#!/usr/bin/env python3
"""Export valid products to JSON snapshots for static hosting."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.library import (  # noqa: E402
    count_confirmed_empty,
    count_incomplete_valid_codes,
    count_products,
    export_library,
)

DEFAULT_BORE_RIP = ROOT.parent / "bore.rip" / "public" / "data" / "battlenet-catalog.json"


def write_catalog(path: Path) -> int:
    items = [item for item in export_library() if item.get("valid")]
    payload = {
        "products": len(items),
        "valid_count": len(items),
        "library_count": count_products(),
        "incomplete_count": count_incomplete_valid_codes(),
        "empty_count": count_confirmed_empty(),
        "items": items,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return len(items)


def main() -> None:
    targets = [ROOT / "static" / "catalog.json"]

    env_target = os.environ.get("BORE_RIP_CATALOG", "").strip()
    if env_target:
        targets.append(Path(env_target))
    elif DEFAULT_BORE_RIP.exists() or DEFAULT_BORE_RIP.parent.exists():
        targets.append(DEFAULT_BORE_RIP)

    count = 0
    for out in targets:
        count = write_catalog(out)
        print(f"Wrote {count} products to {out}")


if __name__ == "__main__":
    main()
