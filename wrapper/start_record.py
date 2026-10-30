"""
레코딩 래퍼 (lerobot 0.5 / python3.13).
lerobot.policies.__init__ 우회 후 lerobot-record를 실행.

사용법:
  python start_record.py --robot.type=piper_follower --robot.port=can_follower1 ...
"""

import os
import sys
import time

# lerobot.policies.__init__ 우회 — 다른 lerobot import 보다 먼저여야 한다
import lerobot_bootstrap

lerobot_bootstrap.load_groot_config()

# 플러그인 등록
from lerobot.utils.import_utils import register_third_party_plugins
register_third_party_plugins()

# `--dataset.vcodec=auto` 가 FFmpeg 빌드에 "들어 있는" NVENC 를 장치 없이도 고르던 것을
# 막는다 — 실제로 열리는 하드웨어 인코더만 후보로 본다 (hw_encoders.py).
try:
    import hw_encoders
    hw_encoders.install()
except Exception as _e:   # 판정이 깨져도 녹화는 LeRobot 의 원래 판정으로 계속된다
    print(f"[start_record] hw encoder probe install failed: {_e}", flush=True)


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
class _Scenario:
    """시나리오 계획을 따르는 녹화 (feature/scenario-designer.md §9-3).

    어느 시도를 찍는지는 여기(커서)가 정한다 — 에피소드 진입·저장·버림을 직접 보므로
    ←/→ 와 경합하지 않는다. 게이트웨이는 버스의 진행 키를 읽기만 한다.
    저장 순간 `(episode_index, trial)` 를 사이드카 로그에 덧붙인다 — **이 줄이 진실이다.**
    재녹화된 에피소드는 저장되지 않으므로 줄도 안 남는다.
    """

    def __init__(self, bus, payload: dict) -> None:
        from scenario_cursor import Cursor

        self.bus = bus
        self.plan = {"plan_id": payload["plan_id"], **payload["plan"]}
        self.cursor = Cursor(payload["plan_id"], self.plan.get("episodes") or [], payload.get("done") or ())
        self.events: dict | None = None
        self._plan_written = False
        print(f"[start_record] scenario plan={payload['plan_id']} "
              f"remaining={len(self.cursor.queue)}/{self.cursor.total}", flush=True)
        self.publish()

    def publish(self) -> None:
        try:
            self.bus.set_record_scenario_state(self.cursor.state())
        except Exception as e:   # 진행 표시가 죽어도 녹화·바인딩은 계속된다
            print(f"[start_record] scenario state publish failed: {e}", flush=True)

    def begin(self) -> dict | None:
        trial = self.cursor.begin()
        if trial:
            print(f"[start_record] scenario begin trial={trial['id']}", flush=True)
        self.publish()
        return trial

    def saved(self, root, episode_index: int) -> None:
        import scenario_cursor as SC

        line = self.cursor.saved(episode_index)
        if line is not None:
            try:
                if not self._plan_written:
                    SC.write_plan(root, self.plan)
                    self._plan_written = True
                SC.append_line(root, line)
            except Exception as e:
                # 여기서 녹화를 죽이면 저장한 에피소드까지 잃는다 — 로그로 남겨 손으로 맞춘다
                print(f"[start_record] scenario log write failed: {e} line={line}", flush=True)
            print(f"[start_record] scenario trial={line['trial']} episode={episode_index} "
                  f"outcome={line['outcome']}", flush=True)
        self.publish()
        if self.cursor.finished and self.events is not None:
            print("[start_record] scenario finished — stopping", flush=True)
            self.events["stop_recording"] = True

    def dropped(self) -> None:
        self.cursor.dropped()
        self.publish()

    def command(self, cmd: str) -> None:
        from piper_bus import contract as C

        if cmd == C.CONTROL_RERECORD:
            self.cursor.mark_rerecord()
        elif cmd in (C.CONTROL_FAIL, C.CONTROL_UNFAIL):
            if not self.cursor.set_failed(cmd == C.CONTROL_FAIL):
                print(f"[start_record] scenario {cmd} ignored — no episode in progress", flush=True)
        elif cmd == C.CONTROL_DEFER:
            tid = self.cursor.defer()
            print(f"[start_record] scenario deferred {tid or '(nothing)'}", flush=True)
        self.publish()


def _install_scenario(bus) -> "_Scenario | None":
    """게이트웨이가 계획을 올렸으면 커서를 세우고 LeRobot 의 저장·버림에 건다."""
    try:
        payload = bus.record_scenario_plan()
    except Exception as e:
        print(f"[start_record] scenario plan read failed: {e}", flush=True)
        return None
    if not payload:
        return None
    scen = _Scenario(bus, payload)

    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    _orig_save = LeRobotDataset.save_episode
    _orig_clear = LeRobotDataset.clear_episode_buffer
    # ⚠ save_episode 가 끝에서 clear_episode_buffer 를 부른다 — 그걸 재녹화로 읽으면
    #   저장한 시도를 큐에 도로 넣는다. 저장 중의 비우기는 버림이 아니다.
    saving = [False]

    def _save(self, *args, **kwargs):
        episode_data = args[0] if args else kwargs.get("episode_data")
        index = None
        if episode_data is None and isinstance(getattr(self, "episode_buffer", None), dict):
            index = self.episode_buffer.get("episode_index")
        saving[0] = True
        try:
            out = _orig_save(self, *args, **kwargs)
        finally:
            saving[0] = False
        if index is not None:
            scen.saved(self.root, int(index))
        return out

    def _clear(self, *args, **kwargs):
        out = _orig_clear(self, *args, **kwargs)
        if not saving[0]:
            scen.dropped()
        return out

    LeRobotDataset.save_episode = _save
    LeRobotDataset.clear_episode_buffer = _clear
    return scen


def _install_control() -> None:
    import threading

    from piper_bus import contract as C
    from piper_bus.client import Bus

    # 제어 루프는 BRPOP 으로 연결을 오래 잡고 있으므로 읽기용 연결을 따로 둔다.
    _task_bus = Bus()
    scen = _install_scenario(_task_bus)

    def _patched_listener():
        events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}
        if scen is not None:
            scen.events = events
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
                if scen is not None:
                    scen.command(cmd)
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

    # ── 첫 에피소드 전 준비 대기 (PIPER_RECORD_WAIT_FIRST=1) ──
    #
    # LeRobot 은 시작하자마자 첫 에피소드를 찍는다 — 판을 놓을 틈이 없다(시나리오면 첫 배치).
    # 첫 에피소드 진입 전에 **리셋과 같은 루프**(프레임을 안 남기고 텔레옵만)를 돌리고 → 로 끝낸다.
    # 그 동안은 "Recording episode N" 을 붙잡아 두었다가 준비가 끝나면 낸다 — 기다리는 동안
    # 게이트웨이·화면이 "녹화 중" 으로 읽으면 안 된다(경과 시간·음성이 그 줄을 따른다).
    wait_first = [os.environ.get("PIPER_RECORD_WAIT_FIRST") == "1"]
    held_say: list = []
    _orig_log_say = LR.log_say

    def _log_say(text, *args, **kwargs):
        if wait_first[0] and str(text).startswith("Recording episode"):
            held_say[:] = [(text, args, kwargs)]
            return None
        return _orig_log_say(text, *args, **kwargs)

    LR.log_say = _log_say

    def _prepare(args, kwargs) -> bool:
        """준비 대기. 녹화를 이어 가면 True, 기다리는 중에 정지했으면 False."""
        print("[start_record] Reset the environment before the first episode — waiting for ready (→)", flush=True)
        events = kwargs.get("events") or {}
        limit_s = float(os.environ.get("PIPER_RECORD_WAIT_FIRST_S") or 0) or WAIT_FIRST_MAX_S
        deadline = time.monotonic() + limit_s
        gaps: list[float] = []
        while True:
            try:
                _orig_record_loop(*args, **{**kwargs, "dataset": None,
                                            "control_time_s": max(0.0, deadline - time.monotonic())})
                break
            except TimeoutError as e:
                # 카메라가 프레임을 한 번 늦게 줬다(shm async_read 200ms). 대기는 아무것도 남기지
                # 않으므로 그 한 번에 녹화 전체를 죽일 이유가 없다 — 다시 기다린다.
                # 짧은 사이에 거듭되면 카메라가 정말 멈춘 것이니 그때는 그대로 죽는다.
                now = time.monotonic()
                gaps[:] = [t for t in gaps if now - t < WAIT_GAP_WINDOW_S] + [now]
                if len(gaps) >= WAIT_GAP_LIMIT:
                    raise
                print(f"[start_record] camera late while waiting — still waiting: {e}", flush=True)
                if events.get("exit_early") or events.get("stop_recording") or now >= deadline:
                    break
        if not events.get("stop_recording") and time.monotonic() >= deadline:
            print(f"[start_record] ready wait ran out ({limit_s:.0f}s) — starting the first episode", flush=True)
        events["exit_early"] = False
        events["rerecord_episode"] = False      # 준비 중의 ← 는 버릴 것이 없다
        if events.get("stop_recording"):
            print("[start_record] stopped before the first episode — nothing recorded", flush=True)
            return False
        for text, a, k in held_say:
            _orig_log_say(text, *a, **k)
        return True

    def _record_loop(*args, **kwargs):
        if wait_first[0] and kwargs.get("dataset") is not None:
            wait_first[0] = False
            if not _prepare(args, kwargs):
                return None     # 빈 버퍼 — 저장은 _guard_empty_save 가 건너뛴다
        # 계획을 따르면 task 는 시도가 정한다 — 에피소드 진입(dataset 이 붙은 호출)에서만.
        # 리셋 루프(dataset 없음)는 프레임을 안 남기므로 아래의 원래 길로 둔다.
        if scen is not None and kwargs.get("dataset") is not None:
            trial = scen.begin()
            if trial is not None:
                if trial["task"]:
                    kwargs["single_task"] = trial["task"]
                return _orig_record_loop(*args, **kwargs)
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


# 준비 대기는 길어도 된다 — 사람이 판을 놓는 동안이다. 끝은 → (또는 정지).
# 화면에서 상한을 주면(PIPER_RECORD_WAIT_FIRST_S) 그 시간이 지나 첫 에피소드를 찍는다.
WAIT_FIRST_MAX_S = 24 * 3600
# 대기 중 카메라 지연을 몇 번까지 넘기나 — 10초 안에 5번이면 카메라가 멈춘 것으로 본다.
WAIT_GAP_LIMIT = 5
WAIT_GAP_WINDOW_S = 10.0


def _install_dataset_guards() -> None:
    """저장·업로드 둘레.

    - 빈 에피소드는 저장하지 않는다 — 첫 에피소드 전 준비 중에 정지하면 LeRobot 이 프레임 없는
      버퍼로 `save_episode()` 를 불러 ValueError 로 죽는다.
    - 허브 업로드의 시작·끝을 찍는다. 게이트웨이의 정지는 `escape` 뒤 60초를 기다리고 프로세스를
      내리는데, 업로드는 에피소드가 **다 저장된 뒤**라 그 시계로 죽이면 올리던 것만 잃는다
      (실기 2026-10-29: 정지가 안 되는 것처럼 보였다). 이 줄을 보면 게이트웨이가 기다리기를 멈춘다.
    """
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    _orig_save = LeRobotDataset.save_episode
    _orig_push = LeRobotDataset.push_to_hub

    def _save(self, *args, **kwargs):
        buf = getattr(self, "episode_buffer", None)
        # ⚠ LeRobot 은 버퍼를 **첫 프레임에서야** 만든다(`add_frame`) — 준비 대기 중에 정지하면 `None` 이다.
        #   dict 만 비었다고 보면 `validate_episode_buffer` 가 `argument of type 'NoneType'` 로 죽는다.
        nothing = buf is None or (isinstance(buf, dict) and not buf.get("size"))
        if not args and kwargs.get("episode_data") is None and nothing:
            print("[start_record] empty episode — not saved", flush=True)
            return None
        return _orig_save(self, *args, **kwargs)

    def _push(self, *args, **kwargs):
        if not self.meta.total_episodes:
            print("[start_record] hub upload skipped — no episodes", flush=True)
            return None
        print("[start_record] hub upload started — episodes are saved", flush=True)
        out = _orig_push(self, *args, **kwargs)
        print("[start_record] hub upload finished", flush=True)
        return out

    LeRobotDataset.save_episode = _save
    LeRobotDataset.push_to_hub = _push


# 주소는 `PIPER_REDIS_URL` 에서 온다. 헤드리스에서 유일한 에피소드 제어 경로다.
try:
    _install_control()
except Exception as _e:
    print(f"[start_record] control install failed: {_e}", flush=True)

# 시나리오의 저장 훅(_install_control 안) **바깥**에 건다 — 빈 버퍼는 거기까지 가기 전에 걸러야 한다
try:
    _install_dataset_guards()
except Exception as _e:
    print(f"[start_record] dataset guard install failed: {_e}", flush=True)

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
