#!/usr/bin/env python3
"""Safely reconcile local queue metadata without changing publication state.

This one-off maintenance tool repairs two metadata-only problems that can occur
when the dashboard's local queue has diverged from the committed repository:

1. Persisted ``slot_claims`` that exist in the committed queue are merged back
   into the local queue (COMPLETED wins; otherwise the newer claim wins).
2. Absolute, machine-specific ``package_reference`` values that point inside
   the repository are rewritten to portable POSIX paths
   (``data/generated/asin_<ASIN>_content_package.json``).

It never changes queue item IDs, ASINs, statuses, schedules, publication
history, errors or the import batch ledger. The default mode is read-only;
pass ``--apply`` to write the reconciled queue (a backup is created first).
"""

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.queue import portable_package_reference  # noqa: E402
from publishing.sync import merge_slot_claims  # noqa: E402

DEFAULT_QUEUE_FILE = PROJECT_ROOT / "data" / "queue" / "publishing_queue.json"
DEFAULT_CLAIMS_SOURCE = "HEAD:data/queue/publishing_queue.json"


def load_queue_source(spec: str, repo_dir: Path = PROJECT_ROOT) -> Dict[str, Any]:
    """Load queue JSON from a file path or a ``<git-rev>:<path>`` spec."""
    candidate = Path(spec)
    if candidate.exists():
        with open(candidate, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    elif ":" in spec:
        revision, _, repo_path = spec.partition(":")
        result = subprocess.run(
            ["git", "show", f"{revision}:{repo_path}"],
            cwd=str(repo_dir),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Could not read {spec} from git: {detail}")
        data = json.loads(result.stdout.decode("utf-8"))
    else:
        raise FileNotFoundError(f"Queue source not found: {spec}")

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(f"Invalid queue source {spec!r}: expected a dict with an items list.")
    return data


def reconcile_queue_state(
    local_data: Dict[str, Any],
    claims_data: Optional[Dict[str, Any]],
    repo_dir: Path = PROJECT_ROOT,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a reconciled copy of ``local_data`` plus a change report."""
    reconciled = copy.deepcopy(local_data)
    report: Dict[str, Any] = {
        "claims_before": len(reconciled.get("slot_claims") or {}),
        "claims_after": len(reconciled.get("slot_claims") or {}),
        "claims_added": [],
        "claims_changed": [],
        "package_references_changed": [],
    }

    merged_claims = merge_slot_claims(
        reconciled.get("slot_claims"),
        (claims_data or {}).get("slot_claims"),
    )
    before_claims = reconciled.get("slot_claims") or {}
    for key in sorted(merged_claims):
        if key not in before_claims:
            report["claims_added"].append(key)
        elif before_claims.get(key) != merged_claims[key]:
            report["claims_changed"].append(key)
    reconciled["slot_claims"] = merged_claims
    report["claims_after"] = len(merged_claims)

    for item in reconciled.get("items", []):
        reference = item.get("package_reference")
        if not reference:
            continue
        normalized = portable_package_reference(
            reference,
            asin=str(item.get("asin") or ""),
            repo_dir=repo_dir,
        )
        if normalized and normalized != reference:
            item["package_reference"] = normalized
            report["package_references_changed"].append(
                {
                    "queue_item_id": item.get("queue_item_id"),
                    "asin": item.get("asin"),
                    "before": reference,
                    "after": normalized,
                }
            )

    return reconciled, report


def _write_atomically(path: Path, data: Dict[str, Any]) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge committed slot claims and normalize package references safely."
    )
    parser.add_argument("--queue-file", default=str(DEFAULT_QUEUE_FILE),
                        help="Local publishing queue JSON file")
    parser.add_argument("--claims-source", default=DEFAULT_CLAIMS_SOURCE,
                        help="Committed queue source: file path or <git-rev>:<path>")
    parser.add_argument("--apply", action="store_true",
                        help="Write the reconciled queue (a .reconcile_bak backup is created)")
    args = parser.parse_args()

    queue_file = Path(args.queue_file)
    if not queue_file.exists():
        print(f"[ERROR] Local queue not found: {queue_file}", file=sys.stderr)
        return 1

    try:
        local_data = load_queue_source(str(queue_file))
        claims_data = load_queue_source(args.claims_source)
        reconciled, report = reconcile_queue_state(local_data, claims_data, PROJECT_ROOT)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"[ERROR] Reconciliation could not run: {exc}", file=sys.stderr)
        return 1

    print(f"Claims before/after      : {report['claims_before']} -> {report['claims_after']}")
    print(f"Claims added             : {len(report['claims_added'])}")
    print(f"Claims updated           : {len(report['claims_changed'])}")
    print(f"Package refs normalized  : {len(report['package_references_changed'])}")
    for change in report["package_references_changed"]:
        print(
            f"  - {change['queue_item_id']} (ASIN {change['asin']}):\n"
            f"      {change['before']}\n"
            f"   -> {change['after']}"
        )

    if not args.apply:
        print("\n[DRY RUN] No file was changed. Re-run with --apply to persist this metadata.")
        return 0

    if not report["claims_added"] and not report["claims_changed"] \
            and not report["package_references_changed"]:
        print("\n[OK] Nothing to change; queue already reconciled.")
        return 0

    backup_file = queue_file.with_name(queue_file.name + ".reconcile_bak")
    shutil.copyfile(queue_file, backup_file)
    _write_atomically(queue_file, reconciled)
    print(f"\n[OK] Reconciled queue written: {queue_file}")
    print(f"[OK] Backup kept at: {backup_file}")
    print("[OK] Import batch ledger was not touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
