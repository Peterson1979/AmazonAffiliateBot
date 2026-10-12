"""
AmazonAffiliateBot Management Dashboard - Flask Application.
Primary human-facing control surface for the local automated affiliate pipeline.
Strictly binds to 127.0.0.1 (localhost only).
"""

from typing import Dict, Any, Optional
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
    jsonify
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.services import (
    load_application_config,
    save_application_config,
    get_dashboard_summary,
    get_queue_item_presentation,
    get_all_products,
    get_product_detail,
    add_products_from_urls,
    revalidate_content,
    regenerate_content,
    execute_discovery_run,
    execute_publisher_run,
    get_log_content,
    check_windows_tasks_status,
    get_platform_statuses,
    execute_sync_with_github,
    DEFAULT_CONFIG_QUERIES,
    DEFAULT_TRACKING_TAG,
    DEFAULT_TARGET_FUTURE_CAPACITY,
    DEFAULT_MIN_FUTURE_CAPACITY
)
from publishing.queue import PublishingQueue, DEFAULT_QUEUE_FILE


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
        dry_run = cfg.get("publishing", {}).get("dry_run", True)
        return {
            "dry_run": dry_run,
            "timezone": "Europe/Budapest",
            "app_name": "AmazonAffiliateBot Control Center"
        }

    # -------------------------------------------------------------
    # 1. Dashboard Overview
    # -------------------------------------------------------------
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
        status_filter = request.args.get("status", "all").strip()
        products_list = get_all_products(query=q, status_filter=status_filter)
        return render_template(
            "products.html",
            products=products_list,
            search_query=q,
            current_filter=status_filter
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
        return render_template("product_detail.html", product=detail)

    # -------------------------------------------------------------
    # 4. Add Amazon Products
    # -------------------------------------------------------------
    @app.route("/add-product", methods=["GET", "POST"])
    def add_product():
        if request.method == "POST":
            raw_urls = request.form.get("urls", "").strip()
            single_url = request.form.get("url", "").strip()
            combined = "\n".join(filter(None, [single_url, raw_urls]))

            if not combined.strip():
                flash("Please enter at least one Amazon product or affiliate URL.", "error")
                return render_template("add_product.html", last_input="")

            res = add_products_from_urls(combined)
            if res.get("success"):
                acc = res.get("accepted", 0)
                rej = res.get("rejected", 0)
                if acc > 0:
                    flash(f"Successfully processed {acc} product(s) into publishing queue!", "success")
                if rej > 0:
                    flash(f"{rej} URL(s) were rejected, duplicate, or failed validation. See details below.", "warning")
            else:
                flash(res.get("message", "Processing failed."), "error")

            return render_template("add_product.html", result=res, last_input=combined)

        return render_template("add_product.html")

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
        queue_obj = PublishingQueue.load(DEFAULT_QUEUE_FILE) if DEFAULT_QUEUE_FILE.exists() else None
        
        items = queue_obj.items if queue_obj else []
        for item in items:
            presentation = get_queue_item_presentation(queue_obj, item) if queue_obj else {}
            item.ui_state = presentation.get("state", "UNKNOWN")
            item.ui_retryable = presentation.get("retryable", False)
            item.ui_platforms = presentation.get("platforms", {})
            item.ui_published_at = presentation.get("published_at")
        if status_filter != "ALL":
            if status_filter in ("SCHEDULED", "PUBLISHING", "SIMULATED", "PUBLISHED", "PARTIAL", "FAILED", "UNKNOWN"):
                items = [it for it in items if it.ui_state == status_filter]
            elif status_filter == "RETRYABLE":
                items = [it for it in items if it.ui_retryable]
            elif status_filter == "READY":
                items = [it for it in items if it.status == "READY"]

        # Sort chronologically by scheduled date and daily slot
        items = sorted(items, key=lambda x: (x.scheduled_date or "9999-99-99", x.daily_slot or 0, x.queue_item_id))

        summary = get_dashboard_summary()
        return render_template(
            "queue.html",
            queue=queue_obj,
            items=items,
            current_filter=status_filter,
            summary=summary
        )

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
        selected_asin = asin.upper().strip() if asin else (all_prods[0]["asin"] if all_prods else None)
        
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

    # -------------------------------------------------------------
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
            ollama_model = request.form.get("ollama_model", "qwen2.5:7b").strip()
            ollama_url = request.form.get("ollama_url", "http://localhost:11434").strip()
            
            try:
                target_cap = int(request.form.get("target_capacity", 28))
                min_cap = int(request.form.get("min_capacity", 14))
            except ValueError:
                target_cap = 28
                min_cap = 14

            tracking_tag = request.form.get("tracking_tag", DEFAULT_TRACKING_TAG).strip()
            raw_queries = request.form.get("queries", "")
            queries_list = [q.strip() for q in raw_queries.splitlines() if q.strip()]

            if disclosure:
                cfg.setdefault("affiliate", {})["disclosure"] = disclosure
            if ollama_model:
                cfg.setdefault("ollama", {})["model"] = ollama_model
            if ollama_url:
                cfg.setdefault("ollama", {})["base_url"] = ollama_url
            
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
    print(f" Safety: DRY RUN ENABLED (No social posts published)")
    print(f" Localhost only (Strictly binds to 127.0.0.1)")
    print(f"========================================================\n", flush=True)

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
