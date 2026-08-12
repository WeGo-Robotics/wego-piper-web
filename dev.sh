#!/usr/bin/env bash
# 백엔드 + 프론트엔드 + E-stop 워치독 동시 실행
set -e

trap 'kill 0' EXIT

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Redis 는 이제 **버스 전체**의 전송로다 (refactor/daemon-split.md 3단계):
#   E-stop heartbeat/PID · 추론 실시간 파라미터 · 녹화 제어 · 녹화 미리보기
# ZMQ 소켓 3개(5555/5556/5557)를 대체했으므로, 없으면 위 네 가지가 전부 안 된다.
#
# E-stop 워치독은 **독립 프로세스**다 (daemons/estopd.py).
# 배포에서는 systemd 유닛으로 뜬다 — deploy/install-estopd.sh 참고.
if redis-cli ping >/dev/null 2>&1; then
  echo "Starting E-stop watchdog (estopd)..."
  python3 "$REPO/daemons/estopd.py" &
  # rsd 는 RealSense 를 독점하는 데몬이다 (daemon-inventory.md #4).
  # 안 띄우면 게이트웨이가 RealSense 를 전혀 못 본다 — 이제 장치를 직접 열지 않는다.
  echo "Starting RealSense daemon (rsd)..."
  python3 "$REPO/daemons/rsd.py" &
else
  echo "⚠ Redis 미실행 — 게이트웨이는 뜨지만 다음이 전부 동작하지 않습니다:"
  echo "   · heartbeat 타임아웃 정지(estopd)  · 추론 실시간 파라미터 변경"
  echo "   · RealSense 카메라 전체(rsd)"
  echo "   · 녹화 제어(건너뛰기/재녹화/정지)   · 녹화 미리보기"
  echo "   sudo systemctl start redis-server"
fi

echo "Starting backend (FastAPI)..."
cd "$REPO/backend"
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 &

echo "Starting frontend (Vite)..."
cd "$REPO/frontend"
npm run dev &

wait
