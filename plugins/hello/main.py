#!/usr/bin/env python3
"""hello — 플러그인 배선 확인용. 의존은 표준 라이브러리 + piper_bus 뿐.

게이트웨이가 넘기는 것(전부 환경변수):
  PIPER_PLUGIN_NAME · PIPER_PLUGIN_DIR · PIPER_PLUGIN_RUNTIME_DIR
  PIPER_PLUGIN_CONFIG (JSON) · PIPER_PLUGIN_CAMERAS (JSON {슬롯: 스트림})
  PIPER_API_BASE · PIPER_API_TOKEN (이번 실행의 범위 토큰)
  PIPER_REDIS_URL (버스)

하는 일: `interval_s` 마다 `GET /api/activity` 를 토큰으로 부르고, 결과를 버스 상태
KV 로 올린다. SIGTERM 이 오면 마지막 상태를 남기고 끝난다.
"""

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

NAME = os.environ.get("PIPER_PLUGIN_NAME", "hello")
CONFIG = json.loads(os.environ.get("PIPER_PLUGIN_CONFIG") or "{}")
API = os.environ.get("PIPER_API_BASE", "http://127.0.0.1:8000")
TOKEN = os.environ.get("PIPER_API_TOKEN", "")

_running = True


def _stop(*_):
    global _running
    _running = False


def api_get(path: str):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = float(CONFIG.get("interval_s", 2))
    greeting = str(CONFIG.get("greeting", "안녕"))
    print(f"[{NAME}] {greeting} — 주기 {interval}s, API {API}, 토큰 {'있음' if TOKEN else '없음'}", flush=True)

    bus = None
    try:
        from piper_bus.client import Bus
        bus = Bus()
    except Exception as e:
        print(f"[{NAME}] 버스 없음 — 상태를 올리지 못한다: {e}", flush=True)

    ticks = 0
    while _running:
        ticks += 1
        activities = []
        err = None
        try:
            snap = api_get("/api/activity")
            activities = [k for k, v in (snap.get("running") or {}).items() if v] \
                if isinstance(snap.get("running"), dict) else snap.get("running", [])
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
        except Exception as e:
            err = str(e)
        state = {"ticks": ticks, "activities": activities, "error": err, "ts": time.time()}
        if bus is not None:
            try:
                bus.put_plugin_state(NAME, state)
            except Exception as e:
                print(f"[{NAME}] 상태 발행 실패: {e}", flush=True)
        print(f"[{NAME}] #{ticks} activities={activities} error={err}", flush=True)
        # 짧게 쪼개 자면 SIGTERM 에 바로 깬다
        for _ in range(int(interval / 0.1) or 1):
            if not _running:
                break
            time.sleep(0.1)
    print(f"[{NAME}] 종료", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
