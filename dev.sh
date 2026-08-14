#!/usr/bin/env bash
# 백엔드 + 프론트엔드 + E-stop 워치독 동시 실행
set -e

trap 'kill 0' EXIT

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Redis 는 이제 **버스 전체**의 전송로다 (refactor/daemon-split.md 3단계):
#   E-stop heartbeat/PID · 추론 실시간 파라미터 · 녹화 제어 · 녹화 미리보기
# ZMQ 소켓 3개(5555/5556/5557)를 대체했으므로, 없으면 위 네 가지가 전부 안 된다.
#
# 데몬 넷은 **독립 프로세스**다. 배포에서는 systemd 유닛으로 뜬다 —
# deploy/install-daemons.sh 참고. 유닛으로 깔면 죽어도 되살아나고
# journald 에 이유가 남는다 (수동 실행은 조용히 죽는다 — 실제로 겪었다).
#
# ⚠ **이미 유닛으로 돌고 있으면 여기서 또 띄우면 안 된다.** 같은 장치를 두
# 프로세스가 다투게 되고, 소유가 하나라는 게 데몬을 나눈 전제다.
start_daemon() {  # $1 = 데몬 이름, $2 = 한 줄 설명
  if systemctl --user is-active --quiet "piper-$1" 2>/dev/null; then
    echo "· $1 은 systemd 유닛으로 이미 실행 중 — 건너뜀"
    return
  fi
  echo "Starting $2 ($1)..."
  python3 "$REPO/daemons/$1.py" &
}

if redis-cli ping >/dev/null 2>&1; then
  start_daemon estopd  "E-stop watchdog"
  # rsd 는 RealSense 를 독점한다 (daemon-inventory.md #4).
  # 안 띄우면 게이트웨이가 RealSense 를 전혀 못 본다 — 이제 장치를 직접 열지 않는다.
  start_daemon rsd     "RealSense daemon"
  # camerad 는 /dev/video* 를 독점한다. rsd 와 **합치지 않는다** —
  # RealSense 가 죽어도 웹캠은 살아야 하기 때문이다 (D405 hang 이력).
  start_daemon camerad "v4l2 daemon"
  # robotd 는 CAN 을 독점한다 (daemon-inventory.md #2).
  # 팔에 명령하는 넷(추론·녹화·수동제어·파킹)이 전부 여기를 지나므로
  # **안전층(하드리밋·데드맨)도 이 프로세스가 없으면 없는 것이다.**
  start_daemon robotd  "robot daemon"
else
  echo "⚠ Redis 미실행 — 게이트웨이는 뜨지만 다음이 전부 동작하지 않습니다:"
  echo "   · heartbeat 타임아웃 정지(estopd)  · 추론 실시간 파라미터 변경"
  echo "   · 카메라 전체 (rsd: RealSense · camerad: 웹캠)"
  echo "   · 로봇팔 전체 (robotd: CAN·안전층) — 스캔·연결·추론·녹화가 다 막힌다"
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
