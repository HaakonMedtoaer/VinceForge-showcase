"""
Minimal .env loader - stdlib only, matches the rest of this project (no
python-dotenv dependency for three lines of parsing). Reads KEY=VALUE pairs
into os.environ. Deliberately simple: no quoting rules, no variable
expansion, no export keyword - just what this project's own .env actually
needs. Silently does nothing if the file doesn't exist, since a fresh
checkout without a .env should still run (just without a seeded default
Target Tenant - see tenants.py).
"""

import os


def load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Don't override a value already set in the real environment -
            # an explicit environment variable should win over the file.
            os.environ.setdefault(key, value)
