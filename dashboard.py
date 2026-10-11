#!/usr/bin/env python3
"""
Direct Launcher for AmazonAffiliateBot Management Dashboard.
Runs locally on 127.0.0.1 (localhost only).
"""

import argparse
from web.app import create_app


def main():
    parser = argparse.ArgumentParser(description="AmazonAffiliateBot - Local Management Dashboard")
    parser.add_argument("--port", "-p", type=int, default=5000, help="Local port (default: 5000)")
    args = parser.parse_args()

    app = create_app()
    print("\n========================================================")
    print(" AmazonAffiliateBot Management Dashboard")
    print(f" Address: http://127.0.0.1:{args.port}")
    print(" Safety: DRY RUN ENABLED (No live social posts published)")
    print(" Localhost only (Strictly binds to 127.0.0.1)")
    print("========================================================\n", flush=True)

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
