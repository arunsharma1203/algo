#!/usr/bin/env bash
# Gracefully stops both Legacy and Qlib backend servers
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$DIR/venv/bin/python" "$DIR/scripts/manage_services.py" stop-all

