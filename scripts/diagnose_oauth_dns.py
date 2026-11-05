"""Offline-safe reproduction of the application's pre-OAuth DNS diagnostic."""

from __future__ import annotations

import os
import re
import socket
import sys
from pathlib import Path

from dotenv import load_dotenv


def _sanitize_message(message: str) -> str:
    """Keep diagnostics secret-free without exposing environment values."""
    sanitized = str(message)
    for value in os.environ.values():
        if value:
            sanitized = sanitized.replace(value, "[REDACTED]")
    sanitized = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(
        r"(?i)(client[_-]?secret|access[_-]?token|authorization|password|token)"
        r"(\s*[:=]\s*)([^\s,;}]+)",
        r"\1\2[REDACTED]",
        sanitized,
    )
    return sanitized


def main() -> int:
    # Match the application's load_dotenv() behavior when run from the project root.
    load_dotenv()

    print(f"sys.executable={sys.executable}")
    print(f"python_version={sys.version.split()[0]}")
    print(f"cwd={Path.cwd()}")
    print("environment_variable_names=" + ",".join(sorted(os.environ)))

    try:
        socket.getaddrinfo("api.amazon.com", 443)
    except Exception as exc:
        print("dns_result=failed")
        print(f"dns_exception_type={type(exc).__name__}")
        print(f"dns_exception_message={_sanitize_message(str(exc))}")
        return 1

    print("dns_result=success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
