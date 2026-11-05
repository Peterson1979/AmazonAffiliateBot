"""
AmazonAffiliateBot Management Dashboard - Flask Application.
Primary human-facing control surface for the local automated affiliate pipeline.
Strictly binds to 127.0.0.1 (localhost only).
"""

from typing import Dict, Any, Optional
from datetime import datetime
import mimetypes
import os
import sys
from pathlib import Path
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    send_from_directory,
    send_file,
    abort
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.services import (
    load_application_config,
    save_application_config,
    get_dashboard_summary,
    get_queue_item_presentation,
    load_import_batches,
    get_import_batch,
    build_import_batch_view,
    get_all_products,
    get_product_detail,
    add_products_from_urls,
    attach_product_video_service,
    detach_product_video_service,
    enqueue_existing_product_service,
    revalidate_content,
    regenerate_content,
    recover_failed_product,
    get_product_creatives,
    generate_product_creatives_service,
    upload_creative_image_service,
    select_creative_service,
    get_reference_image_service,
    execute_discovery_run,
    execute_publisher_run,
    execute_live_publish_item,
    get_log_content,
    check_windows_tasks_status,
    get_platform_statuses,
    execute_sync_with_github,
    format_batch_label,
    DEFAULT_CONFIG_QUERIES,
    DEFAULT_TRACKING_TAG,
    DEFAULT_TARGET_FUTURE_CAPACITY,
    DEFAULT_MIN_FUTURE_CAPACITY
)
from publishing.queue import (
    PublishingQueue,
    DEFAULT_QUEUE_FILE,
    STATUS_PUBLISHED,
    STATUS_READY,
    STATUS_READY_FOR_REVIEW,
    STATUS_FAILED,
    find_overdue_held_items,
    get_budapest_timezone,
)
from amazon.creators_api import environment_status as creators_api_environment_status

# Products page pagination defaults. Server-side only: the route slices the
# filtered product list before rendering.
PRODUCTS_PER_PAGE_OPTIONS = (25, 50, 100)
DEFAULT_PRODUCTS_PER_PAGE = PRODUCTS_PER_PAGE_OPTIONS[0]


def _parse_positive_int(raw: Any, default: int) -> int:
    """Parse an untrusted query-string integer, falling back to ``default``."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def auto_sync_after_import_enabled() -> bool:
    """Automatic GitHub synchronization after import: on unless opted out.

    Opt out with AUTO_SYNC_AFTER_IMPORT=0 (or false/no/off).
    """
    flag = os.environ.get("AUTO_SYNC_AFTER_IMPORT", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def create_app(test_config: Optional[Dict[str, Any]] = None) -> Flask:
    """Application factory for AmazonAffiliateBot Management Dashboard."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent / "static")
    )
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "amazon-affiliate-bot-local-secret-key")
    if test_config:
        app.config.update(test_config)

    @app.context_processor
    def inject_global_context():
        """Inject safety notice and timezone into all templates."""
        cfg = load_application_config()
        dry_run = cfg.get("publishing", {}).get("dry_run", False)
        workflow_configured = (PROJECT_ROOT / ".github" / "workflows" / "scheduled_publishing.yml").exists()
        return {
            "dry_run": dry_run,
            "execution_mode": "DEFAULT DRY-RUN · explicit LIVE action available" if dry_run else "LIVE publisher configured",
            "cloud_scheduling_configured": workflow_configured,
            "timezone": "Europe/Budapest",
            "app_name": "AmazonAffiliateBot Control Center"
        }

    # -------------------------------------------------------------
    # 1. Dashboard Overview
    # -------------------------------------------------------------
    @app.route("/healthz")
    def healthz():
        """Fast local readiness endpoint used by the desktop launcher."""
        return jsonify({
            "status": "ok",
            "service": "amazon-affiliate-dashboard",
            "creators_api": creators_api_environment_status(),
        })

    @app.route("/")
    def index():
        summary = get_dashboard_summary()
        return render_template("dashboard.html", summary=summary)

    # -------------------------------------------------------------
    # 2. Products Page
    # -------------------------------------------------------------
    @app.route("/products")
    def products():
        q = request.args.get("q", "").strip()
        status_filter = request.args.get("status", "all").strip().lower()

        products_list = get_all_products(query=q, status_filter=status_filter)

        if status_filter == "all":
            published_products = [p for p in products_list if p.get("publishing_status") == "PUBLISHED"]
            active_products = [p for p in products_list if p.get("publishing_status") != "PUBLISHED"]
            failed_products = [
                p for p in products_list
                if p.get("publishing_status") == "FAILED" or p.get("validation_status") in ("FAILED_VALIDATION", "FAILED")
            ]
            display_list = active_products
            active_count = len(active_products)
            published_count = len(published_products)
            failed_count = len(failed_products)
        elif status_filter == "published":
            display_list = products_list
            published_products = products_list
            published_count = len(published_products)
            active_count = 0
            failed_count = 0
        else:
            display_list = products_list
            published_products = [p for p in products_list if p.get("publishing_status") == "PUBLISHED"]
            active_count = len(display_list)
            published_count = len(published_products)
            failed_count = len([
                p for p in display_list
                if p.get("publishing_status") == "FAILED" or p.get("validation_status") in ("FAILED_VALIDATION", "FAILED")
            ])

        per_page = _parse_positive_int(request.args.get("per_page"), default=PRODUCTS_PER_PAGE_OPTIONS[0])
        if per_page not in PRODUCTS_PER_PAGE_OPTIONS:
            per_page = PRODUCTS_PER_PAGE_OPTIONS[0]

        total_display = len(display_list)
        total_pages = max(1, (total_display + per_page - 1) // per_page)
        page = min(max(_parse_positive_int(request.args.get("page"), default=1), 1), total_pages)

        start_index = (page - 1) * per_page
        page_products = display_list[start_index:start_index + per_page]

        pages: list = []
        window_start = max(1, page - 2)
        window_end = min(total_pages, page + 2)
        if window_start > 1:
            pages.append(1)
            if window_start > 2:
                pages.append("…")
        pages.extend(range(window_start, window_end + 1))
        if window_end < total_pages:
            if window_end < total_pages - 1:
                pages.append("…")
            pages.append(total_pages)

        return render_template(
            "products.html",
            products=page_products,
            published_products=published_products,
            active_count=active_count,
            published_count=published_count,
            failed_count=failed_count,
            search_query=q,
            current_filter=status_filter,
            pagination={
                "page": page,
                "per_page": per_page,
                "total": total_display,
                "total_pages": total_pages,
                "pages": pages,
                "has_prev": page > 1,
                "has_next": page < total_pages,
                "start_index": start_index + 1 if total_display else 0,
                "end_index": min(start_index + per_page, total_display),
            }
        )

    # -------------------------------------------------------------
    # 3. Product Detail
    # -------------------------------------------------------------
    @app.route("/products/<asin>")
    def product_detail(asin: str):
        detail = get_product_detail(asin)
        if not detail:
            flash(f"Product with ASIN '{asin}' was not found.", "warning")
            return redirect(url_for("products"))
        failed_recovery_batches = []
        for batch in load_import_batches():
            for entry in batch.get("entries", []):
                if (
                    str(entry.get("asin", "")).upper() == asin.upper()
                    and entry.get("status") == "CONTENT_GENERATION_FAILED"
                ):
                    failed_recovery_batches.append({
                        "batch_id": batch.get("batch_id"),
                        "url": entry.get("url"),
                    })
        return render_template(
            "product_detail.html",
            product=detail,
            failed_recovery_batches=failed_recovery_batches,
        )

    # -------------------------------------------------------------
    # 4. Add Amazon Products
    # -------------------------------------------------------------
    @app.route("/add-product", methods=["GET", "POST"])
    def add_product():
        if request.method == "POST":
            raw_urls = request.form.get("urls", "").strip()
            single_url = request.form.get("url", "").strip()
            combined = "\n".join(filter(None, [single_url, raw_urls]))
            uploaded_video = request.files.get("video")
            has_video = bool(uploaded_video and str(getattr(uploaded_video, "filename", "") or "").strip())

            if not combined.strip():
                flash("Please enter at least one Amazon product or affiliate URL.", "error")
                return render_template("add_product.html", last_input="", existing_products=get_all_products())

            if has_video and single_url and raw_urls:
                flash("Video attachment applies to a single product URL only. Please leave Batch Affiliate URLs empty when attaching a video.", "error")
                return render_template("add_product.html", last_input=combined, existing_products=get_all_products())

            if has_video:
                res = add_products_from_urls(combined, video_file=uploaded_video)
            else:
                res = add_products_from_urls(combined)

            # Normalize ASINs: replace empty/missing ASINs with safe display values
            # and record display URL so the template can show the original URL
            # without relying on the product_detail route when ASIN is unavailable.
            for r in res.get("results", []):
                asin_val = r.get("asin")
                if not asin_val or asin_val == "UNKNOWN":
                    r["asin"] = ""
                r["_display_url"] = r.get("url", "")

            if res.get("success"):
                acc = res.get("accepted", 0)
                rej = res.get("rejected", 0)
                if acc > 0:
                    flash(f"Successfully processed {acc} product(s) into publishing queue!", "success")

                    # Route-level Git operation: only runs when at least one
                    # product was accepted, never under test runs, and can be
                    # disabled with AUTO_SYNC_AFTER_IMPORT=0.  It reuses the
                    # existing synchronization lock and error handling inside
                    # execute_sync_with_github() and never publishes anything.
                    if auto_sync_after_import_enabled() and not app.config.get("TESTING"):
                        sync_res = execute_sync_with_github()
                        if sync_res.get("success"):
                            flash(
                                f"The {acc} accepted product(s) were synchronized to GitHub: "
                                f"{sync_res.get('message')}",
                                "success"
                            )
                        else:
                            flash(
                                f"Automatic GitHub synchronization failed: "
                                f"{sync_res.get('error', 'unknown error')} "
                                f"All local data was preserved, but the newly accepted product(s) may not "
                                f"yet be available to scheduled GitHub Actions publishing.",
                                "error"
                            )
                if rej > 0:
                    flash(f"{rej} URL(s) were rejected, duplicate, or failed validation. See details below.", "warning")
            else:
                flash(res.get("message", "Processing failed."), "error")

            return render_template("add_product.html", result=res, last_input=combined, existing_products=get_all_products())

        # GET: blank form.
        existing_asin = request.args.get("existing_asin", "").strip().upper()
        if existing_asin:
            if get_product_detail(existing_asin):
                return redirect(url_for("product_detail", asin=existing_asin))
            flash(f"Product with ASIN '{existing_asin}' was not found.", "warning")
        return render_template("add_product.html", existing_products=get_all_products())

    # -------------------------------------------------------------
    # 5. Discovery Control
    # -------------------------------------------------------------
    @app.route("/discovery")
    def discovery():
        cfg = load_application_config()
        disc_cfg = cfg.get("discovery", {})
        queries = disc_cfg.get("queries", list(DEFAULT_CONFIG_QUERIES))
        tracking_tag = disc_cfg.get("tracking_tag", DEFAULT_TRACKING_TAG)
        target_cap = disc_cfg.get("target_future_capacity", DEFAULT_TARGET_FUTURE_CAPACITY)
        min_cap = disc_cfg.get("min_future_capacity", DEFAULT_MIN_FUTURE_CAPACITY)
        return render_template(
            "discovery.html",
            queries=queries,
            tracking_tag=tracking_tag,
            target_capacity=target_cap,
            min_capacity=min_cap
        )

    @app.route("/discovery/save", methods=["POST"])
    def discovery_save():
        cfg = load_application_config()
        raw_queries = request.form.get("queries", "")
        queries_list = [q.strip() for q in raw_queries.splitlines() if q.strip()]
        
        tracking_tag = request.form.get("tracking_tag", "").strip()
        try:
            target_cap = int(request.form.get("target_capacity", 28))
            min_cap = int(request.form.get("min_capacity", 14))
        except ValueError:
            target_cap = 28
            min_cap = 14

        cfg.setdefault("discovery", {})
        cfg["discovery"]["queries"] = queries_list
        if tracking_tag:
            cfg["discovery"]["tracking_tag"] = tracking_tag
        cfg["discovery"]["target_future_capacity"] = target_cap
        cfg["discovery"]["min_future_capacity"] = min_cap

        save_application_config(cfg)
        flash("Discovery settings saved successfully.", "success")
        return redirect(url_for("discovery"))

    @app.route("/discovery/run", methods=["POST"])
    def discovery_run():
        res = execute_discovery_run(force_discovery=True)
        if res.get("success"):
            rep = res.get("report", {})
            added = rep.get("items_added_count", 0)
            reason = rep.get("reason", "")
            if reason == "CAPACITY_HEALTHY":
                flash(f"Queue capacity is already healthy ({rep.get('initial_future_capacity')}/{rep.get('target_capacity')} items). No new discovery needed.", "info")
            else:
                flash(f"Discovery completed (DRY RUN): Added {added} product(s) to future slots.", "success")
            return render_template("discovery_result.html", report=rep)
        else:
            flash(f"Discovery failed: {res.get('error')}", "error")
            return redirect(url_for("discovery"))

    # -------------------------------------------------------------
    # 6. Queue Management
    # -------------------------------------------------------------
    @app.route("/queue")
    def queue():
        status_filter = request.args.get("status", "all").strip().upper()
        selected_batch_id = request.args.get("batch_id", "").strip() or None
        asin_filter = request.args.get("asin", "").strip().upper()
        queue_obj = PublishingQueue.load(DEFAULT_QUEUE_FILE) if DEFAULT_QUEUE_FILE.exists() else None

        batches = load_import_batches()
        for b in batches:
            b["display_label"] = format_batch_label(b)

        selected_batch = None
        if selected_batch_id and selected_batch_id != "ALL":
            selected_batch = next((b for b in batches if b.get("batch_id") == selected_batch_id), None)

        if selected_batch:
            batch_view = build_import_batch_view(selected_batch, queue_obj)
            items = list(batch_view["items"])
            batch_entries = batch_view["entries"]
        else:
            batch_entries = []
            items = []
            if queue_obj:
                for it in queue_obj.items:
                    presentation = get_queue_item_presentation(queue_obj, it)
                    it.ui_state = presentation["state"]
                    it.ui_label = presentation.get("ui_label", presentation["state"])
                    it.ui_retryable = presentation["retryable"]
                    it.ui_platforms = presentation["platforms"]
                    it.ui_published_at = presentation["published_at"]
                    it.ui_media_type = presentation["media_type"]
                    it.ui_video_url = presentation["video_url"]
                    it.ui_image_url = presentation["image_url"]
                    items.append(it)

        if status_filter != "ALL":
            if status_filter in ("SCHEDULED", "PUBLISHING", "SIMULATED", "PUBLISHED", "PARTIAL", "FAILED", "UNKNOWN"):
                items = [it for it in items if it.ui_state == status_filter]
            elif status_filter == "RETRYABLE":
                items = [it for it in items if it.ui_retryable]
            elif status_filter == "READY":
                items = [it for it in items if it.status == "READY"]

        # Sort chronologically by scheduled date and daily slot
        if asin_filter:
            items = [it for it in items if (it.asin or "").upper() == asin_filter]
        items = sorted(items, key=lambda x: (x.scheduled_date or "9999-99-99", x.daily_slot or 0, x.queue_item_id))

        overdue_held_items = []
        if queue_obj:
            for held_item in find_overdue_held_items(queue_obj, datetime.now(get_budapest_timezone())):
                presentation = get_queue_item_presentation(queue_obj, held_item)
                held_item.ui_state = presentation["state"]
                held_item.ui_label = presentation.get("ui_label", presentation["state"])
                held_item.ui_retryable = presentation["retryable"]
                held_item.ui_platforms = presentation["platforms"]
                held_item.ui_published_at = presentation["published_at"]
                held_item.ui_media_type = presentation["media_type"]
                held_item.ui_video_url = presentation["video_url"]
                held_item.ui_image_url = presentation["image_url"]
                overdue_held_items.append(held_item)

        per_page = _parse_positive_int(request.args.get("per_page"), default=25)
        total_items = len(items)
        total_pages = max(1, (total_items + per_page - 1) // per_page)
        page = min(max(_parse_positive_int(request.args.get("page"), default=1), 1), total_pages)

        start_index = (page - 1) * per_page
        page_items = items[start_index:start_index + per_page]

        pages: list = []
        window_start = max(1, page - 2)
        window_end = min(total_pages, page + 2)
        if window_start > 1:
            pages.append(1)
            if window_start > 2:
                pages.append("…")
        pages.extend(range(window_start, window_end + 1))
        if window_end < total_pages:
            if window_end < total_pages - 1:
                pages.append("…")
            pages.append(total_pages)

        pagination = {
            "page": page,
            "per_page": per_page,
            "total": total_items,
            "total_pages": total_pages,
            "pages": pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "start_index": start_index + 1 if total_items else 0,
            "end_index": min(start_index + per_page, total_items),
        }

        summary = get_dashboard_summary()
        return render_template(
            "queue.html",
            queue=queue_obj,
            items=page_items,
            overdue_held_items=overdue_held_items,
            current_filter=status_filter,
            summary=summary,
            batches=list(reversed(batches)),
            selected_batch=selected_batch,
            selected_batch_id=selected_batch_id or "ALL",
            batch_entries=batch_entries,
            pagination=pagination,
            asin_filter=asin_filter
        )

    @app.route("/queue/<queue_item_id>/remove", methods=["POST"])
    def queue_remove_item(queue_item_id: str):
        if not DEFAULT_QUEUE_FILE.exists():
            flash("Queue file not found.", "error")
            return redirect(url_for("queue"))
        queue_obj = PublishingQueue.load(DEFAULT_QUEUE_FILE)
        removed = queue_obj.remove_item(queue_item_id)
        if removed:
            queue_obj.save(DEFAULT_QUEUE_FILE)
            flash(f"Removed item '{removed.product_name or removed.asin}' ({removed.queue_item_id}) from queue. Underlying product data, content, and history remain intact.", "success")
        else:
            flash(f"Item '{queue_item_id}' not found in queue.", "error")
        return redirect(url_for("queue"))

    @app.route("/queue/<queue_item_id>/move", methods=["POST"])
    def queue_move_item(queue_item_id: str):
        direction = request.form.get("direction", "up").strip().lower()
        if not DEFAULT_QUEUE_FILE.exists():
            flash("Queue file not found.", "error")
            return redirect(url_for("queue"))
        queue_obj = PublishingQueue.load(DEFAULT_QUEUE_FILE)
        moved = queue_obj.move_item(queue_item_id, direction)
        if moved:
            queue_obj.save(DEFAULT_QUEUE_FILE)
            flash(f"Item moved {direction} successfully.", "success")
        else:
            flash(f"Could not move item {direction}.", "warning")
        return redirect(url_for("queue"))

    @app.route("/queue/reorder", methods=["POST"])
    def queue_reorder():
        ordered_ids = request.form.getlist("order") or (request.json.get("order", []) if request.is_json else [])
        if not DEFAULT_QUEUE_FILE.exists():
            if request.is_json:
                return jsonify({"success": False, "error": "Queue file not found."}), 404
            flash("Queue file not found.", "error")
            return redirect(url_for("queue"))
        queue_obj = PublishingQueue.load(DEFAULT_QUEUE_FILE)
        if ordered_ids:
            queue_obj.reorder_items(ordered_ids)
            queue_obj.save(DEFAULT_QUEUE_FILE)
            if request.is_json:
                return jsonify({"success": True})
            flash("Queue reordered successfully.", "success")
        return redirect(url_for("queue"))

    @app.route("/queue/reschedule-overdue", methods=["POST"])
    def queue_reschedule_overdue():
        if not DEFAULT_QUEUE_FILE.exists():
            flash("Queue file not found.", "error")
            return redirect(url_for("queue"))
        queue_obj = PublishingQueue.load(DEFAULT_QUEUE_FILE)
        rescheduled = queue_obj.reschedule_overdue_items()
        if rescheduled:
            queue_obj.save(DEFAULT_QUEUE_FILE)
            flash(f"Successfully rescheduled {len(rescheduled)} overdue unpublished item(s) into future available slots.", "success")
        else:
            flash("No overdue unpublished items found to reschedule.", "info")
        return redirect(url_for("queue"))

    @app.route("/queue/maintain", methods=["POST"])
    def queue_maintain():
        res = execute_discovery_run()
        if res.get("success"):
            rep = res.get("report", {})
            added = rep.get("items_added_count", 0)
            flash(f"Queue maintenance executed (DRY RUN): Added {added} products.", "success")
        else:
            flash(f"Queue maintenance error: {res.get('error')}", "error")
        return redirect(url_for("queue"))

    # -------------------------------------------------------------
    # 6.5. GitHub Synchronization
    # -------------------------------------------------------------
    @app.route("/sync", methods=["POST"])
    def sync_github():
        res = execute_sync_with_github()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify(res)

        if res.get("success"):
            flash(f"Sync with GitHub succeeded: {res.get('message')}", "success")
        else:
            flash(f"Sync with GitHub failed: {res.get('error')}", "error")

        ref = request.referrer
        if ref and ("queue" in ref or "dashboard" in ref or "products" in ref or "discovery" in ref):
            return redirect(ref)
        return redirect(url_for("queue"))

    # -------------------------------------------------------------
    # 7. Content Preview
    # -------------------------------------------------------------
    @app.route("/content-preview")
    @app.route("/content-preview/<asin>")
    def content_preview(asin: Optional[str] = None):
        all_prods = get_all_products()

        # Explicit selection wins: path segment first, then the selector
        # form's ?asin= query so the GET form also works without JavaScript.
        requested = (asin or request.args.get("asin") or "").upper().strip()
        if requested:
            selected_asin = requested
        elif all_prods:
            # Default to the newest product accepted into the publishing queue
            # (READY + READY_FOR_REVIEW with generated content) so packages
            # persisted by Add Products open in the preview immediately instead
            # of the oldest scheduled entry. No status is altered here.
            accepted = [
                p for p in all_prods
                if p.get("queue_status") == STATUS_READY
                and p.get("validation_status") == STATUS_READY_FOR_REVIEW
                and p.get("content_status") == "GENERATED"
            ]
            if accepted:
                selected_asin = max(
                    accepted, key=lambda p: p.get("scheduled_at") or ""
                )["asin"]
            else:
                selected_asin = all_prods[0]["asin"]
        else:
            selected_asin = None

        detail = get_product_detail(selected_asin) if selected_asin else None
        return render_template(
            "content_preview.html",
            products=all_prods,
            selected_asin=selected_asin,
            product=detail
        )

    # -------------------------------------------------------------
    # 8. Validation Control & Regeneration
    # -------------------------------------------------------------
    @app.route("/api/validate/<asin>", methods=["POST"])
    def api_validate(asin: str):
        res = revalidate_content(asin)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(res)
        if res.get("success"):
            status_str = "PASSED" if res.get("is_valid") else "FAILED"
            flash(f"Re-validation for ASIN {asin}: {status_str} ({res.get('package_status')})", "success" if res.get("is_valid") else "warning")
        else:
            flash(res.get("message", "Validation failed"), "error")
        return redirect(url_for("product_detail", asin=asin))

    @app.route("/api/regenerate/<asin>", methods=["POST"])
    def api_regenerate(asin: str):
        res = regenerate_content(asin)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(res)
        if res.get("success"):
            flash(f"Content regenerated for ASIN {asin}. Status: {res.get('package_status')}", "success" if res.get("is_ready") else "warning")
        else:
            flash(res.get("message", "Regeneration failed"), "error")
        return redirect(url_for("product_detail", asin=asin))

    @app.route("/api/recover-failed/<asin>", methods=["POST"])
    def api_recover_failed(asin: str):
        """Explicitly recover a failed import; never invoked automatically."""
        res = recover_failed_product(asin, batch_id=request.form.get("batch_id") or request.args.get("batch_id"))
        accept = request.headers.get("Accept", "")
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        api_json = request.is_json or is_ajax or not accept or "application/json" in accept.lower()
        if api_json:
            return jsonify(res)
        if res.get("success"):
            flash(f"Recovered ASIN {asin} into the review queue; nothing was published.", "success")
        else:
            flash(res.get("message", "Recovery failed"), "error")
        return redirect(url_for("product_detail", asin=asin))

    # -------------------------------------------------------------
    @app.route("/data/creatives/<asin>/<filename>", endpoint="serve_creative_data")
    @app.route("/creatives/<asin>/<filename>", endpoint="serve_creative")
    def serve_creative(asin: str, filename: str):
        """Serve generated Pinterest marketing creative image files."""
        safe_filename = Path(filename).name
        creatives_dir = Path(__file__).resolve().parent.parent / "data" / "creatives" / asin.upper().strip()
        target_file = creatives_dir / safe_filename
        if not creatives_dir.is_dir() or not target_file.is_file():
            abort(404)
        mtype, _ = mimetypes.guess_type(safe_filename)
        return send_from_directory(creatives_dir, safe_filename, mimetype=mtype or "image/png")

    @app.route("/creatives/<asin>/reference-image", endpoint="download_reference_image")
    def download_reference_image(asin: str):
        """Download the original reference product image."""
        ref_path = get_reference_image_service(asin)
        if ref_path and ref_path.is_file():
            return send_file(
                ref_path,
                as_attachment=True,
                download_name=f"{asin.upper()}_reference_image{ref_path.suffix}",
            )
        from content.pinterest_creative import _find_product_for_asin, is_valid_image_url
        product = _find_product_for_asin(asin)
        if product and product.image_url and is_valid_image_url(product.image_url):
            return redirect(product.image_url)
        flash("Reference product image could not be resolved or downloaded.", "warning")
        return redirect(url_for("product_detail", asin=asin))

    @app.route("/api/creatives/<asin>/upload", methods=["POST"])
    def api_upload_creative(asin: str):
        """Upload generated Flow image into matching creative slot."""
        creative_type = request.form.get("creative_type", "").strip()
        uploaded_file = request.files.get("image")
        if not uploaded_file or not uploaded_file.filename:
            flash("Please choose an image file to upload.", "warning")
            return redirect(url_for("product_detail", asin=asin))

        res = upload_creative_image_service(asin, creative_type, uploaded_file)
        if res.get("success"):
            flash(res.get("message", "Image uploaded successfully."), "success")
        else:
            flash(res.get("message", "Image upload failed."), "error")
        return redirect(url_for("product_detail", asin=asin))

    @app.route("/api/creatives/<asin>/select", methods=["POST"])
    def api_select_creative(asin: str):
        """Select or deselect a creative for Pinterest publishing."""
        creative_type = request.form.get("creative_type", "").strip()
        res = select_creative_service(asin, creative_type)
        if res.get("success"):
            flash(res.get("message", "Creative selection updated."), "success")
        else:
            flash(res.get("message", "Failed to update creative selection."), "error")
        return redirect(url_for("product_detail", asin=asin))

    @app.route("/api/generate-creatives/<asin>", methods=["POST"])
    def api_generate_creatives(asin: str):
        """Generate 3-5 distinct Pinterest marketing creative variations for a product."""
        overwrite = request.form.get("overwrite", "false").lower() in ("true", "1", "yes")
        res = generate_product_creatives_service(asin, overwrite=overwrite)
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        if is_ajax or request.is_json:
            return jsonify(res)
        if res.get("success"):
            flash(res.get("message", "Pinterest creatives generated successfully."), "success")
        else:
            flash(res.get("message", "Creative generation skipped or failed."), "warning")
        return redirect(url_for("product_detail", asin=asin))

    @app.route("/api/video/<asin>/upload", methods=["POST"])
    def api_upload_video(asin: str):
        """Upload or replace a product video in Cloudflare R2 for an existing product."""
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json
        uploaded_video = request.files.get("video")
        if not uploaded_video or not str(getattr(uploaded_video, "filename", "") or "").strip():
            msg = "Please select a local MP4 or MOV video file to upload."
            if is_ajax:
                return jsonify({"success": False, "asin": asin.upper().strip(), "message": msg}), 400
            flash(msg, "warning")
            return redirect(url_for("product_detail", asin=asin))

        res = attach_product_video_service(asin, uploaded_video)
        if res.get("success"):
            if not is_ajax:
                flash(res.get("message", "Product video uploaded successfully."), "success")
            if res.get("queue_updated") and auto_sync_after_import_enabled() and not app.config.get("TESTING"):
                sync_res = execute_sync_with_github()
                res["sync"] = sync_res
                if not is_ajax:
                    if sync_res.get("success"):
                        flash(
                            f"Video update for ASIN {res.get('asin', asin.upper())} synchronized to GitHub: "
                            f"{sync_res.get('message')}",
                            "success",
                        )
                    else:
                        flash(
                            f"Automatic GitHub synchronization failed: {sync_res.get('error', 'unknown error')} "
                            f"Local package and queue updates were preserved.",
                            "error",
                        )
            if is_ajax:
                return jsonify(res)
        else:
            if is_ajax:
                return jsonify(res), 400
            flash(res.get("message", "Product video upload failed."), "error")

        return redirect(url_for("product_detail", asin=asin))

    @app.route("/api/video/<asin>/remove", methods=["POST"])
    def api_remove_video(asin: str):
        """Detach an uploaded video from an existing product's ContentPackage and queue item."""
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json
        res = detach_product_video_service(asin)
        if res.get("success"):
            if not is_ajax:
                flash(res.get("message", "Product video removed successfully."), "success")
            if res.get("queue_updated") and auto_sync_after_import_enabled() and not app.config.get("TESTING"):
                sync_res = execute_sync_with_github()
                res["sync"] = sync_res
                if not is_ajax:
                    if sync_res.get("success"):
                        flash(
                            f"Video removal for ASIN {res.get('asin', asin.upper())} synchronized to GitHub: "
                            f"{sync_res.get('message')}",
                            "success",
                        )
                    else:
                        flash(
                            f"Automatic GitHub synchronization failed: {sync_res.get('error', 'unknown error')} "
                            f"Local package and queue updates were preserved.",
                            "error",
                        )
            if is_ajax:
                return jsonify(res)
        else:
            if is_ajax:
                return jsonify(res), 400
            flash(res.get("message", "Product video removal failed."), "error")

        return redirect(url_for("product_detail", asin=asin))

    @app.route("/api/queue/<asin>/enqueue", methods=["POST"])
    def api_enqueue_product(asin: str):
        """Enqueue an existing cached product and its validated ContentPackage."""
        is_ajax = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.is_json
        )
        res = enqueue_existing_product_service(asin)
        if res.get("success"):
            if not is_ajax:
                flash(res.get("message", "Added product to publishing queue."), "success")
            if (
                res.get("queue_updated")
                and auto_sync_after_import_enabled()
                and not app.config.get("TESTING")
            ):
                sync_res = execute_sync_with_github()
                res["sync"] = sync_res
                if not is_ajax:
                    if sync_res.get("success"):
                        flash(
                            f"Queue update for ASIN {res.get('asin', asin.upper())} synchronized to GitHub: "
                            f"{sync_res.get('message')}",
                            "success",
                        )
                    else:
                        flash(
                            f"Automatic GitHub synchronization failed: {sync_res.get('error', 'unknown error')} "
                            f"Local queue update was preserved.",
                            "error",
                        )
            if is_ajax:
                return jsonify(res)
        else:
            if is_ajax:
                return jsonify(res), 400
            flash(res.get("message", "Could not add product to publishing queue."), "error")

        return redirect(url_for("product_detail", asin=asin))

    # 9. Publisher Control (DRY RUN)
    # -------------------------------------------------------------
    @app.route("/publisher/run", methods=["POST"])
    def publisher_run():
        res = execute_publisher_run()
        if res.get("success"):
            rep = res.get("report", {})
            pub_count = rep.get("published_count", 0)
            due_count = rep.get("due_count", 0)
            flash(f"Publisher executed (DRY RUN): Processed {due_count} due items ({pub_count} simulated).", "success")
        else:
            flash(f"Publisher failed: {res.get('error')}", "error")
        return redirect(url_for("queue"))

    @app.route("/queue/<queue_item_id>/publish", methods=["POST"])
    def publish_queue_item(queue_item_id: str):
        """Explicitly confirm and execute one live publication."""
        # The checkbox is acknowledgement only; the named submit control is
        # the server-side action boundary.  A crafted checkbox-only POST must
        # not be able to trigger live publication.
        confirmed = (
            request.form.get("confirm_live") == "yes"
            and request.form.get("publish_live") == "confirm"
        )
        result = execute_live_publish_item(queue_item_id, confirmed=confirmed)
        if result.get("success"):
            if result.get("skipped"):
                flash(result.get("message", "Already published; no action taken."), "info")
            else:
                parts = [f"Confirmed platforms: {result.get('confirmed_platforms', 0)}"]
                if result.get("skipped_platforms", 0) > 0:
                    parts.append(f"previously published / skipped: {result.get('skipped_platforms')}")
                parts.append(f"failed platforms: {result.get('failed_platforms', 0)}")
                flash(
                    f"Live publication finished for {result.get('asin')}: {result.get('status')}. "
                    f"{'; '.join(parts)}.",
                    "success" if result.get("status") == STATUS_PUBLISHED else "warning",
                )
        else:
            flash(result.get("error", "Live publication was not executed."), "error")
        return redirect(url_for("queue"))

    # -------------------------------------------------------------
    # 10. Log Viewer
    # -------------------------------------------------------------
    @app.route("/logs")
    def logs():
        log_type = request.args.get("type", "publisher")
        max_lines = int(request.args.get("lines", 200))
        lines = get_log_content(log_type=log_type, max_lines=max_lines)
        return render_template("logs.html", lines=lines, log_type=log_type, max_lines=max_lines)

    # -------------------------------------------------------------
    # 11. Automation & Windows Task Scheduler
    # -------------------------------------------------------------
    @app.route("/automation")
    def automation():
        summary = get_dashboard_summary()
        tasks = check_windows_tasks_status()
        return render_template("automation.html", summary=summary, tasks=tasks)

    # -------------------------------------------------------------
    # 12. Settings / Configuration
    # -------------------------------------------------------------
    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        cfg = load_application_config()
        if request.method == "POST":
            # Update safe fields only
            disclosure = request.form.get("disclosure", "").strip()
            
            try:
                target_cap = int(request.form.get("target_capacity", 28))
                min_cap = int(request.form.get("min_capacity", 14))
            except ValueError:
                target_cap = 28
                min_cap = 14

            tracking_tag = request.form.get("tracking_tag", "homeideas4a01-20").strip()
            raw_queries = request.form.get("queries", "")
            queries_list = [q.strip() for q in raw_queries.splitlines() if q.strip()]

            if disclosure:
                cfg.setdefault("affiliate", {})["disclosure"] = disclosure
            cfg.setdefault("discovery", {})
            cfg["discovery"]["target_future_capacity"] = target_cap
            cfg["discovery"]["min_future_capacity"] = min_cap
            cfg["discovery"]["tracking_tag"] = tracking_tag
            if queries_list:
                cfg["discovery"]["queries"] = queries_list

            save_application_config(cfg)
            flash("Configuration updated successfully.", "success")
            return redirect(url_for("settings"))

        platform_statuses = get_platform_statuses(cfg)
        return render_template("settings.html", config=cfg, platforms=platform_statuses)

    return app


def main():
    """Run local dashboard on 127.0.0.1."""
    import argparse
    parser = argparse.ArgumentParser(description="AmazonAffiliateBot - Local Management Dashboard")
    parser.add_argument("--port", "-p", type=int, default=5000, help="Local port (default: 5000)")
    args = parser.parse_args()

    app = create_app()
    print(f"\n========================================================")
    print(f" AmazonAffiliateBot Management Dashboard")
    print(f" Address: http://127.0.0.1:{args.port}")
    print(f" Mode: Production-first; explicit live queue actions require confirmation")
    print(f" Safety: Previews and local maintenance never publish")
    print(f" Localhost only (Strictly binds to 127.0.0.1)")
    print(f"========================================================\n", flush=True)

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
