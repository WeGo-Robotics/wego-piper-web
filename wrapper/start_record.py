"""
레코딩 래퍼 (lerobot 0.5 / python3.13).
lerobot.policies.__init__ 우회 후 lerobot-record를 실행.

사용법:
  python start_record.py --robot.type=piper_follower --robot.port=can_follower1 ...
"""

import os
import sys

# lerobot.policies.__init__ 우회 — 다른 lerobot import 보다 먼저여야 한다
import lerobot_bootstrap

lerobot_bootstrap.load_groot_config()

# 플러그인 등록
from lerobot.utils.import_utils import register_third_party_plugins
register_third_party_plugins()


# ── 웹 미리보기 탭 (선택) ──
# PIPER_PREVIEW=1 이면, LeRobot 이 매 프레임 호출하는 log_rerun_data 를 가로채
# 카메라 이미지를 JPEG 로 버스에 올린다. 무거운 Rerun WASM 뷰어 대신
# 기존 JPEG 미리보기 UI 로 보내기 위함. record 가 display_data=true 여야 동작한다.
#
# 중요: log_rerun_data 는 record 루프 스레드에서 동기 호출된다. 여기서 직접 인코딩하면
# 루프 FPS 가 절반으로 떨어진다(실측 6.6→3.5Hz, CPU 부족 시 악화). 따라서 탭은 raw
# 프레임 복사만(수백 µs) 하고 즉시 반환하고, 인코딩/전송은 별도 워커 스레드에서 ~10fps
# 로 제한해 처리한다 — 녹화 루프에 부하를 주지 않는다.
def _install_preview_tap() -> None:
    import threading
    import time

    import numpy as np
    import cv2
    from piper_bus.client import Bus

    # ZMQ PUSH(SNDHWM=2) 를 버스 키 덮어쓰기로 교체 (refactor/daemon-split.md 3단계).
    # 큐가 아니라 키라서 "밀리면 최신만 의미 있음"이 구조적으로 보장된다 —
    # HWM 으로 드롭을 유도할 필요가 없어졌다.
    bus = Bus()

    MAX_SIDE = 320  # 긴 변 기준 다운스케일 (인코딩 비용/대역폭 최소화)
    latest: dict = {}              # name -> raw ndarray (최신만, 덮어쓰기로 드롭)
    lock = threading.Lock()
    _sent = set()                  # 카메라별 첫 프레임 전송 시 1회만 로그

    def _tap(observation=None, action=None, compress_images=False):
        # record 루프 스레드. 이미지 판별 + 복사만 하고 즉시 반환한다.
        if not observation:
            return
        with lock:
            for k, v in observation.items():
                if v is None or not isinstance(v, np.ndarray) or v.ndim != 3:
                    continue
                # HWC(끝이 3/4) 또는 CHW(앞이 1/3/4) 인 것만 이미지로 본다.
                if v.shape[2] in (3, 4) or v.shape[0] in (1, 3, 4):
                    # 복사: record 루프가 다음 프레임에 버퍼를 재사용할 수 있어 ref 보관은 위험.
                    latest[str(k).split(".")[-1]] = v.copy()

    def _worker():
        while True:
            time.sleep(0.1)  # ~10fps 상한 (브라우저 폴링 5fps 보다 여유)
            with lock:
                items = list(latest.items())
                latest.clear()
            for name, arr in items:
                try:
                    # CHW -> HWC 보정 (visualization_utils.log_rerun_data 와 동일 규칙)
                    if arr.shape[0] in (1, 3, 4) and arr.shape[-1] not in (1, 3, 4):
                        arr = np.transpose(arr, (1, 2, 0))
                    if arr.ndim != 3 or arr.shape[2] not in (3, 4):
                        continue
                    if arr.dtype != np.uint8:
                        arr = np.clip(arr, 0, 255).astype(np.uint8)
                    # LeRobot 카메라는 RGB → cv2 JPEG 는 BGR 가정
                    bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR if arr.shape[2] == 4 else cv2.COLOR_RGB2BGR)
                    h, w = bgr.shape[:2]
                    scale = MAX_SIDE / max(h, w)
                    if scale < 1.0:
                        bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if not ok:
                        continue
                    bus.put_preview(name, buf.tobytes())
                    if name not in _sent:
                        _sent.add(name)
                        print(f"[start_record] preview frame streaming: {name}", flush=True)
                except Exception:
                    pass  # 버스가 느리거나 끊겨도 녹화는 계속된다

    threading.Thread(target=_worker, daemon=True).start()

    import lerobot.scripts.lerobot_record as LR
    LR.init_rerun = lambda *a, **k: None   # 실제 Rerun 뷰어/연결 시도 차단 (헤드리스 hang 회피)
    LR.log_rerun_data = _tap


# 주소는 `PIPER_REDIS_URL` 에서 온다. 게이트웨이가 미리보기를 원할 때만 켠다.
if os.environ.get("PIPER_PREVIEW") == "1":
    try:
        _install_preview_tap()
    except Exception as _e:
        print(f"[start_record] preview tap install failed: {_e}", flush=True)


# ── 헤드리스 에피소드 제어 ──
# 헤드리스라 LeRobot 의 키보드 리스너가 꺼져 에피소드 제어가 동작하지 않는다.
# init_keyboard_listener 를 가로채, 백엔드가 PUSH 하는 명령으로 events dict 를
# 직접 set 한다(record() 가 이 dict 를 모든 record_loop 에 공유 사용).
def _install_control() -> None:
    import threading

    from piper_bus import contract as C
    from piper_bus.client import Bus

    def _patched_listener():
        events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}
        # ZMQ PULL bind 를 버스 큐 소비로 교체 (refactor/daemon-split.md 3단계).
        # 게이트웨이가 녹화 시작·종료 때 큐를 비우므로 지난 세션의 명령은 오지 않는다.
        bus = Bus()

        def loop():
            while True:
                try:
                    cmd = bus.pop_control()
                except Exception:
                    break
                if cmd is None:      # 타임아웃 — 빈 큐일 뿐이다
                    continue
                if cmd == C.CONTROL_NEXT:
                    events["exit_early"] = True
                elif cmd == C.CONTROL_RERECORD:
                    events["rerecord_episode"] = True
                    events["exit_early"] = True
                elif cmd == C.CONTROL_STOP:
                    events["stop_recording"] = True
                    events["exit_early"] = True
                print(f"[start_record] control: {cmd}", flush=True)

        threading.Thread(target=loop, daemon=True).start()
        return None, events

    import lerobot.scripts.lerobot_record as LR
    LR.init_keyboard_listener = _patched_listener

    # 우리는 **설계상 헤드리스**다 — 에피소드 제어가 키보드가 아니라 버스로 온다.
    # 그런데 LeRobot 의 `is_headless()` 는 매번 `import pynput` 을 시도하고,
    # X 가 없으면 ImportError 스택트레이스를 통째로 로그에 쏟는다.
    # 정상 동작인데 화면상 에러로 보여서 진짜 원인을 가린다 (녹화 종료 때마다 나왔다).
    #
    # `lerobot_record` 가 `from ... import is_headless` 로 **자기 네임스페이스에**
    # 들여왔으므로 원본 모듈만 고치면 소용없다. 양쪽 다 바꾼다.
    import lerobot.utils.control_utils as CU
    LR.is_headless = lambda: True
    CU.is_headless = lambda: True

    # ── 녹화 중 task 변경 ──
    #
    # LeRobot 은 에피소드마다 `record_loop(..., single_task=cfg.dataset.single_task)` 를
    # 부르고, 그 값을 그 에피소드의 **모든 프레임**에 찍는다. 그래서 진입 시점에
    # 버스 값으로 덮으면 **다음 에피소드부터** 새 task 가 적용된다.
    # (도중에 바꾸면 한 에피소드 안에서 프레임마다 task 가 달라진다 — 하지 않는다.)
    _orig_record_loop = LR.record_loop
    _last_task = [None]
    # 제어 루프는 BRPOP 으로 연결을 오래 잡고 있으므로 읽기용 연결을 따로 둔다.
    _task_bus = Bus()

    def _record_loop(*args, **kwargs):
        try:
            task = _task_bus.record_task()
        except Exception:
            task = None
        if task and task != kwargs.get("single_task"):
            kwargs["single_task"] = task
            if task != _last_task[0]:
                _last_task[0] = task
                print(f"[start_record] task: {task}", flush=True)
        return _orig_record_loop(*args, **kwargs)

    LR.record_loop = _record_loop


# 주소는 `PIPER_REDIS_URL` 에서 온다. 헤드리스에서 유일한 에피소드 제어 경로다.
try:
    _install_control()
except Exception as _e:
    print(f"[start_record] control install failed: {_e}", flush=True)

# ── 레코딩 실행 ──
from lerobot.scripts.lerobot_record import record

if __name__ == "__main__":
    record()

    # `record()` 가 돌아왔다 = 데이터셋 기록과 로봇 해제가 모두 끝났다.
    #
    # 여기서 그냥 두면 인터프리터 종료 중 C++ 정적 소멸자가 돌면서
    # `terminate called without an active exception` 으로 abort 한다
    # (librealsense/ffmpeg 스레드가 아직 joinable 한 채로 파괴되는 흔한 형태).
    # 작업은 이미 끝난 뒤라 데이터에는 영향이 없지만, **종료 코드가 SIGABRT 가 되어**
    # 게이트웨이가 정상 종료를 실패로 판정하고 로그에도 에러처럼 남는다.
    #
    # 버퍼를 비운 뒤 정적 소멸자를 건너뛰고 끝낸다. `record()` 가 예외를 던지면
    # 여기까지 오지 않으므로 **진짜 실패는 그대로 0 이 아닌 코드로 드러난다.**
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
