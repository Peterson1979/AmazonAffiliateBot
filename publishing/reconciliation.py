"""Evidence-backed historical publication reconciliation for the dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


RECONCILIATION_FILE = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "reconciliation"
    / "historical_publication_reconciliation.json"
)


def load_historical_reconciliation(path: Path = RECONCILIATION_FILE) -> Dict[str, Any]:
    """Load the auditable report; a missing report must never break the UI."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def get_historical_destination_report(asin: str, platform: str) -> Dict[str, Any]:
    """Return report metadata for one ASIN/destination, if available."""
    report = load_historical_reconciliation()
    products = report.get("products")
    if not isinstance(products, dict):
        return {}
    product = products.get(asin)
    if not isinstance(product, dict):
        return {}
    destinations = product.get("destinations")
    if not isinstance(destinations, dict):
        return {}
    result = destinations.get(platform)
    return result if isinstance(result, dict) else {}
