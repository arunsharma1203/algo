#!/usr/bin/env bash
# Starts both Legacy (port 8000) and Qlib (port 8001) backend servers in background
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$DIR/venv/bin/python" "$DIR/scripts/manage_services.py" start-all

