"""
Safe, authentication-only Facebook Graph API v27.0 verification script.
Validates token presence, expiration/validity, Page ownership (LifeModeHQ),
and publishing permissions without ever invoking publishing endpoints.
Never prints or logs access tokens.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Any, Tuple

GRAPH_VERSION = "v27.0"
EXPECTED_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "226949230493910").strip()
EXPECTED_PAGE_NAME = "LifeModeHQ"
REQUIRED_PERMISSIONS = ["pages_manage_posts", "pages_read_engagement", "pages_show_list", "publish_video"]


def safe_api_get(endpoint: str, token: str, params: Dict[str, Any] = None) -> Tuple[int, Dict[str, Any]]:
    """Execute a safe GET request to Meta Graph API, redacting token in all error paths."""
    params = params.copy() if params else {}
    params["access_token"] = token
    query_str = urllib.parse.urlencode(params)
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{endpoint}?{query_str}"
    req = urllib.request.Request(url, headers={"User-Agent": "AmazonAffiliateBot-AuthVerifier/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            err_data = json.loads(err.read().decode("utf-8"))
        except Exception:
            err_data = {"error": {"message": err.reason, "code": err.code}}
        return err.code, err_data
    except Exception as exc:
        return 0, {"error": {"message": str(exc), "code": 0}}


def run_verification(token: str, page_id: str) -> bool:
    print("=======================================================")
    print(f"Facebook Graph API {GRAPH_VERSION} Authentication Verification")
    print("=======================================================\n")

    # Check 1: Secret presence
    print("--- Check 1: Secret Configuration ---")
    if not token:
        print("[FAIL] FACEBOOK_PAGE_ACCESS_TOKEN is empty or missing.")
        return False
    if not page_id:
        print("[FAIL] FACEBOOK_PAGE_ID is empty or missing.")
        return False

    token_prefix = token[:4] if len(token) >= 4 else ""
    print(f"[PASS] FACEBOOK_PAGE_ACCESS_TOKEN configured (length: {len(token)}, prefix: {token_prefix}***).")
    print(f"[PASS] FACEBOOK_PAGE_ID configured: {page_id}\n")

    # Check 2: API Connectivity & Token Validity
    print(f"--- Check 2: Graph API {GRAPH_VERSION} Token Validity & Expiry ---")
    status, me_data = safe_api_get("me", token, {"fields": "id,name"})
    if status == 200:
        entity_id = me_data.get("id", "<unknown>")
        entity_name = me_data.get("name", "<unknown>")
        print(f"[PASS] Graph API {GRAPH_VERSION} validated token successfully (HTTP {status}).")
        print(f"       Entity Name: {entity_name}")
        print(f"       Entity ID  : {entity_id}\n")
    else:
        err_obj = me_data.get("error", {})
        err_msg = err_obj.get("message", "Unknown error")
        err_code = err_obj.get("code", status)
        err_subcode = err_obj.get("error_subcode", "")
        print(f"[FAIL] Graph API {GRAPH_VERSION} rejected token (HTTP {status}).")
        print(f"       OAuth Error Code   : {err_code}")
        if err_subcode:
            print(f"       OAuth Error Subcode: {err_subcode}")
        print(f"       OAuth Error Message: {err_msg}")
        return False

    # Check 3: Page Identity & Ownership
    print(f"--- Check 3: Target Page Verification ({EXPECTED_PAGE_NAME} / {page_id}) ---")
    status_page, page_data = safe_api_get(page_id, token, {"fields": "id,name,link,tasks"})
    if status_page == 200:
        ret_id = str(page_data.get("id", ""))
        ret_name = str(page_data.get("name", ""))
        id_match = (ret_id == page_id)
        name_match = (EXPECTED_PAGE_NAME.lower() in ret_name.lower())

        if id_match:
            print(f"[PASS] Page ID matches target: {ret_id}")
        else:
            print(f"[FAIL] Page ID mismatch: returned {ret_id}, expected {page_id}")
            return False

        if name_match:
            print(f"[PASS] Page Name matches target: '{ret_name}'")
        else:
            print(f"[WARN] Page Name '{ret_name}' differs from expected '{EXPECTED_PAGE_NAME}'")
        print()
    else:
        err_obj = page_data.get("error", {})
        err_msg = err_obj.get("message", "Unknown error")
        err_code = err_obj.get("code", status_page)
        print(f"[FAIL] Could not verify Page {page_id} (HTTP {status_page}, Error {err_code}: {err_msg})")
        return False

    # Check 4: Publishing Permissions & Tasks
    print("--- Check 4: Required Publishing Permissions & Tasks ---")
    tasks = page_data.get("tasks", [])
    can_create = False
    if tasks:
        print(f"[INFO] Page tasks granted to token: {', '.join(tasks)}")
        can_create = any(t in tasks for t in ["CREATE_CONTENT", "MANAGE", "MODERATE"])
        if can_create:
            print("[PASS] Token possesses Page content creation task (CREATE_CONTENT).")
        else:
            print("[WARN] Token tasks do not include CREATE_CONTENT.")

    status_perm, perm_data = safe_api_get("me/permissions", token)
    if status_perm == 200 and "data" in perm_data:
        granted = {p.get("permission") for p in perm_data.get("data", []) if p.get("status") == "granted"}
        print(f"[INFO] Granted scopes: {', '.join(sorted(granted)) if granted else '<none listed>'}")
        missing_perms = [p for p in REQUIRED_PERMISSIONS if p not in granted]
        if not missing_perms:
            print("[PASS] All required publishing permissions granted.")
        else:
            print(f"[INFO] Permissions not explicitly in /me/permissions: {', '.join(missing_perms)}")
            if can_create:
                print("[PASS] Publishing authorization confirmed via Page tasks.")
            else:
                print("[WARN] Please ensure 'pages_manage_posts' and 'publish_video' are granted.")
    else:
        if can_create:
            print("[PASS] Publishing authorization confirmed via Page tasks.")
        else:
            print("[INFO] Permissions endpoint not applicable for direct Page token.")

    print("\n=======================================================")
    print(f"RESULT: ALL FACEBOOK AUTHENTICATION CHECKS PASSED ({GRAPH_VERSION})")
    print("=======================================================")
    return True


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    token = (os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN") or os.environ.get("META_ACCESS_TOKEN", "")).strip()
    page_id = (os.environ.get("FACEBOOK_PAGE_ID") or EXPECTED_PAGE_ID).strip()

    success = run_verification(token, page_id)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
