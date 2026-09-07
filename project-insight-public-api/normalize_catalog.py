#!/usr/bin/env python3
import json
import re
from pathlib import Path

CATALOG = Path(__file__).resolve().parent / "v1" / "listings"


def normalize_condition(item: dict) -> None:
    if item.get("condition") != "New":
        return
    text = (item.get("detail") or "").lower()
    explicit_new = re.search(r"\b(?:nwt|nwot|new with tags|new without tags|brand new|never worn|never used)\b", text)
    explicit_used = re.search(r"\b(?:pre[- ]?owned|preowned|used|worn|wear|good condition|excellent condition)\b", text)
    if explicit_used and not explicit_new:
        item["condition"] = "Pre-Owned"


def main() -> None:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    for item in payload.get("listings", []):
        normalize_condition(item)
    CATALOG.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
