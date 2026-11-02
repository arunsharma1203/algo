#!/usr/bin/env bash
# Probes both Legacy (port 8000) and Qlib (port 8001) runtime health and prints status
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$DIR/venv/bin/python" "$DIR/scripts/manage_services.py" status

