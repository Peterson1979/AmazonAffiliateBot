"""
publishing/blacklist.py - Permanent ASIN Blacklist Enforcement

Enforces permanent ASIN blacklisting across the entire lifecycle:
- Product import / scraping
- Content generation
- Queue creation & maintenance
- Scheduling & slot assignment
- Publishing dispatch & adapter execution
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Set, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BLACKLIST_FILE = PROJECT_ROOT / "data" / "blacklist.json"

# Hardcoded permanent immutable blacklist - cannot be bypassed or removed by config changes
PERMANENT_BLACKLISTED_ASINS = frozenset({
    "B09D7DWTVB",
})


def load_blacklist_file(path: Path = BLACKLIST_FILE) -> Set[str]:
    """Load configured blacklisted ASINs from data/blacklist.json."""
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            raw_list = data.get("blacklisted_asins", [])
            if isinstance(raw_list, list):
                return {str(a).strip().upper() for a in raw_list if a}
    except Exception:
        pass
    return set()


def get_blacklisted_asins() -> Set[str]:
    """Return the combined immutable and persisted set of blacklisted ASINs."""
    asins = set(PERMANENT_BLACKLISTED_ASINS)
    asins.update(load_blacklist_file())
    return asins


def is_asin_blacklisted(asin: Optional[str]) -> bool:
    """Return True if the ASIN is permanently blacklisted."""
    if not asin:
        return False
    clean = str(asin).strip().upper()
    if clean in PERMANENT_BLACKLISTED_ASINS:
        return True
    return clean in load_blacklist_file()


def assert_not_blacklisted(asin: Optional[str], context: str = "") -> None:
    """Raise ValueError if the ASIN is blacklisted."""
    if is_asin_blacklisted(asin):
        ctx_msg = f" in {context}" if context else ""
        raise ValueError(f"ASIN '{asin}' is permanently blacklisted{ctx_msg}. Operation prohibited.")

