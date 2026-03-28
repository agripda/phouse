#!/usr/bin/env bash
# .devcontainer/start.sh
# Auto-starts FastAPI + Streamlit when the Codespace resumes.
# Both processes run in the background; logs written to poc/logs/.

set -e

cd /workspaces/phouse/poc

mkdir -p data logs

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Leave Submission API — PoC"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. FastAPI ────────────────────────────────────────────
echo "▶  Starting FastAPI on port 8090..."
uvicorn main:app \
  --host 0.0.0.0 \
  --port 8090 \
  --reload \
  > logs/uvicorn.log 2>&1 &
echo "   PID $! — logs/uvicorn.log"

# ── 2. Wait for API to be ready ───────────────────────────
echo "   Waiting for API..."
for i in $(seq 1 20); do
  if curl -sf http://localhost:8090/health > /dev/null 2>&1; then
    echo "   ✅ API ready"
    break
  fi
  sleep 1
done

# ── 3. Streamlit UI ───────────────────────────────────────
echo "▶  Starting Streamlit on port 8501..."
streamlit run app.py \
  --server.port 8501 \
  --server.address 0.0.0.0 \
  --server.headless true \
  > logs/streamlit.log 2>&1 &
echo "   PID $! — logs/streamlit.log"

echo ""
echo "  FastAPI   → PORTS tab → port 8090  (or /docs for Swagger)"
echo "  Streamlit → PORTS tab → port 8501  (opens automatically)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"