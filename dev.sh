#!/usr/bin/env bash
# 백엔드 + 프론트엔드 + E-stop 워치독 동시 실행
set -e

trap 'kill 0' EXIT

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# E-stop 워치독은 **독립 프로세스**다 (daemons/estopd.py).
# 배포에서는 systemd 유닛으로 뜬다 — deploy/install-estopd.sh 참고.
# Redis 가 없으면 게이트웨이는 그대로 뜨지만 heartbeat 타임아웃 정지가 동작하지 않는다.
if redis-cli ping >/dev/null 2>&1; then
  echo "Starting E-stop watchdog (estopd)..."
  python3 "$REPO/daemons/estopd.py" &
else
  echo "⚠ Redis 미실행 — estopd 를 띄우지 않습니다. heartbeat 타임아웃 정지가 동작하지 않습니다."
  echo "   sudo systemctl start redis-server"
fi

echo "Starting backend (FastAPI)..."
cd "$REPO/backend"
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 &

echo "Starting frontend (Vite)..."
cd "$REPO/frontend"
npm run dev &

wait
