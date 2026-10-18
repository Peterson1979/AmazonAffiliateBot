"""Build the persistent, evidence-preserving historical publication report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parent.parent
QUEUE_FILE = ROOT / "data" / "queue" / "publishing_queue.json"
OUTPUT_FILE = ROOT / "data" / "reconciliation" / "historical_publication_reconciliation.json"
DESTINATIONS = (
    "facebook",
    "pinterest",
    "pinterest_lifemodehq",
    "threads",
)


def _confirmed_live(result: Dict[str, Any]) -> bool:
    return (
        result.get("status") == "PUBLISHED"
        and result.get("mode") == "LIVE"
        and bool(
            result.get("post_id")
            or result.get("pin_id")
            or result.get("photo_id")
            or result.get("permalink")
            or result.get("permalink_url")
            or result.get("pin_url")
        )
    )


def _destination_record(item: Dict[str, Any], destination: str) -> Dict[str, Any]:
    all_results = item.get("publishing_results") or {}
    result = all_results.get(destination) if isinstance(all_results, dict) else {}
    result = result or {}
    if not isinstance(result, dict):
        result = {}
    if _confirmed_live(result):
        evidence_status = "platform_verified"
        state = "PUBLISHED"
    elif result.get("mode") == "DRY_RUN" or result.get("status") == "WOULD_PUBLISH":
        evidence_status = "dry_run_record"
        state = "SIMULATED"
    elif result.get("status") == "FAILED":
        evidence_status = "persisted_failure"
        state = "FAILED"
    else:
        evidence_status = "unresolved"
        state = "UNKNOWN"
    return {
        "state": state,
        "evidence_status": evidence_status,
        "source_queue_status": item.get("status"),
        "account": result.get("account"),
        "board": result.get("board"),
        "post_id": result.get("post_id") or result.get("pin_id") or result.get("photo_id"),
        "post_url": result.get("permalink_url") or result.get("pin_url") or result.get("permalink"),
        "published_at": result.get("published_at"),
        "errors": result.get("errors") or [],
        "message": result.get("message"),
    }


def build_report(queue_payload: Dict[str, Any]) -> Dict[str, Any]:
    products: Dict[str, Any] = {}
    for item in queue_payload.get("items", []):
        asin = item.get("asin")
        if not asin:
            continue
        products[asin] = {
            "queue_item_id": item.get("queue_item_id"),
            "product_name": item.get("product_name"),
            "original_url": item.get("original_url"),
            "source_published_at": item.get("published_at"),
            "destinations": {
                destination: _destination_record(item, destination)
                for destination in DESTINATIONS
            },
        }

    # This is a user report, not fabricated platform evidence. Keep it separate
    # so the UI can show the report without calling it a verified publication.
    b0d = products.get("B0DTHHK2S1")
    if b0d:
        b0d["destinations"]["facebook"].update({
            "state": "UNKNOWN",
            "reported_outcome": "not_visible",
            "evidence_status": "user_reported",
            "evidence_note": "User reports that the Facebook post was not visible; no platform evidence is stored.",
        })
        for destination in ("pinterest", "pinterest_lifemodehq", "threads"):
            b0d["destinations"][destination].update({
                "state": "UNKNOWN",
                "reported_outcome": "published",
                "evidence_status": "user_reported",
                "evidence_note": "User reports publication; no platform ID, permalink or read-only verification is stored.",
            })

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_policy": "Only LIVE results with a platform ID or permalink are platform_verified; dry-run records remain SIMULATED unless separately reconciled.",
        "destinations": list(DESTINATIONS),
        "products": products,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Write the report to the repository data directory")
    args = parser.parse_args()
    payload = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    report = build_report(payload)
    if args.write:
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
