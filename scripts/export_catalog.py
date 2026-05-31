#!/usr/bin/env python3
"""Export valid products to a public JSON snapshot for static hosting."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.library import export_library  # noqa: E402


def main() -> None:
    out = ROOT / "static" / "catalog.json"
    items = [item for item in export_library() if item.get("valid")]
    payload = {"products": len(items), "items": items}
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(items)} products to {out}")


if __name__ == "__main__":
    main()
