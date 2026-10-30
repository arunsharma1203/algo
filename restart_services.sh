#!/usr/bin/env bash
# restart_services.sh - Cleanly stops and restarts FastAPI Backend (8000), Qlib Engine (8001), and Vite Frontend (5173)

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================================="
echo "🔄 [1/4] Stopping stale services on ports 8000, 8001, 5173"
echo "=========================================================="
PIDS=$(lsof -ti :8000 -ti :8001 -ti :5173 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PIDS" ]; then
    echo "Killing processes on ports 8000, 8001, 5173 (PIDs: $PIDS)..."
    echo "$PIDS" | xargs kill -9 2>/dev/null || true
    sleep 2
else
    echo "No processes currently running on ports 8000, 8001, 5173."
fi

echo "=========================================================="
echo "🚀 [2/4] Starting Microsoft Qlib Engine (Port 8001)"
echo "=========================================================="
cd "$PROJECT_ROOT/backend"
nohup ./venv/bin/python -m uvicorn app.qlib_server:app --host 0.0.0.0 --port 8001 < /dev/null > /tmp/qlib_server.log 2>&1 &
QLIB_PID=$!
echo "Qlib engine started (PID: $QLIB_PID) -> Logs at /tmp/qlib_server.log"

echo "=========================================================="
echo "🚀 [3/4] Starting FastAPI Main Backend (Port 8000)"
echo "=========================================================="
nohup ./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 < /dev/null > /tmp/fastapi_backend.log 2>&1 &
BACKEND_PID=$!
echo "FastAPI backend started (PID: $BACKEND_PID) -> Logs at /tmp/fastapi_backend.log"

echo "=========================================================="
echo "🚀 [4/4] Starting Vite Frontend Dev Server (Port 5173)"
echo "=========================================================="
cd "$PROJECT_ROOT/frontend"
nohup ./node_modules/.bin/vite --host 0.0.0.0 --port 5173 < /dev/null > /tmp/frontend_vite.log 2>&1 &
FRONTEND_PID=$!
disown $FRONTEND_PID 2>/dev/null || true
echo "Vite frontend started (PID: $FRONTEND_PID) -> Logs at /tmp/frontend_vite.log"
cd "$PROJECT_ROOT"

echo "=========================================================="
echo "⏳ Waiting 12 seconds for services to initialize..."
echo "=========================================================="
sleep 12

echo "=========================================================="
echo "🔍 Health Checks"
echo "=========================================================="
curl -s http://localhost:8000/docs > /dev/null && echo "✅ FastAPI Backend (8000): Online" || echo "❌ FastAPI Backend (8000): FAILED"
curl -s http://localhost:8001/docs > /dev/null && echo "✅ Qlib Server (8001): Online" || echo "❌ Qlib Server (8001): FAILED"
curl -s http://localhost:5173 > /dev/null && echo "✅ Frontend Dev (5173): Online" || echo "❌ Frontend Dev (5173): FAILED"
echo "=========================================================="
echo "✨ Restart complete! You can view frontend at http://localhost:5173"

