"""시뮬 카메라 — MJCF `<camera>` 를 오프스크린 렌더해 카메라 세그먼트로 발행 (feature/sim-env.md §4).

camerad 와 같은 계약·같은 어휘. 다른 점:

- **렌더는 스레드 하나가 전부 한다 — probe 까지.** EGL 컨텍스트는 만든 스레드에
  귀속된다(실측: RPC 스레드가 probe 로 렌더러를 만든 뒤 렌더 스레드가 쓰자
  5분에 EGLError 139회). 렌더러는 렌더 스레드 안에서만 만들고, 다른 스레드는
  **요청 큐**로 부탁한다(`_ask`). `update_scene` 만 세계 락 안에서, `render` 는 밖에서.
- **컨트롤은 에뮬레이션이다.** 이름은 v4l2(uvcvideo) 것을 그대로 쓴다 —
  `piper_cam.controls` 의 자동 모드 순서 규칙(AUTO_SWITCHES)·단위(CONTROL_UNITS)가
  그 이름을 보므로, 프로파일 적용·회색카드·조명 감시가 시뮬에서 그대로 시험된다.
  노출×게인 = 밝기 배율, 색온도 = 채널 게인, brightness = 오프셋.
- **세그먼트는 BGR** 이다 (yolod 가 RGB 로 오독했던 사고의 재발 방지 — 소비자 규약).
"""

import logging
import queue
import threading
import time

import numpy as np

from piper_cam import controls as controls_mod
from piper_cam import publish

logger = logging.getLogger(__name__)

CAMERAS = ("top", "front", "wrist")
DEFAULT_WHT = (640, 480, 15)

#: v4l2 이름의 컨트롤 — 기본값은 "에뮬레이션 배율 1.0" 이 되는 값들.
#: exposure_time_absolute 는 ×100µs (CONTROL_UNITS) — 156 = 15.6ms, 게인 0..255.
_CONTROL_SPECS: dict[str, dict] = {
    "auto_exposure":             {"min": 1, "max": 3, "step": 2, "default": 3, "type": "menu"},
    "exposure_time_absolute":    {"min": 1, "max": 5000, "step": 1, "default": 156, "type": "int"},
    "gain":                      {"min": 0, "max": 255, "step": 1, "default": 64, "type": "int"},
    "white_balance_automatic":   {"min": 0, "max": 1, "step": 1, "default": 1, "type": "bool"},
    "white_balance_temperature": {"min": 2800, "max": 6500, "step": 10, "default": 4600, "type": "int"},
    "brightness":                {"min": -64, "max": 64, "step": 1, "default": 0, "type": "int"},
}


def cam_id_of(name: str) -> str:
    return f"sim:{name}"


def emulate(rgb: np.ndarray, c: dict[str, float]) -> np.ndarray:
    """컨트롤 → 화소. 순수 함수 (테스트가 여기를 직접 민다).

    자동 노출(3)이면 노출·게인 배율 1, 수동(1)이면 노출×게인 / 기본값.
    자동 WB 면 채널 1, 수동이면 색온도: 4600K 기준으로 낮으면 붉게, 높으면 푸르게.
    """
    img = rgb.astype(np.float32)
    if int(c.get("auto_exposure", 3)) == 1:
        e = float(c.get("exposure_time_absolute", 156)) / 156.0
        g = float(c.get("gain", 64)) / 64.0
        img *= e * g
    if int(c.get("white_balance_automatic", 1)) == 0:
        k = float(c.get("white_balance_temperature", 4600))
        t = (k - 4600.0) / 1900.0                 # -1(2800K) .. +1(6500K)
        img[..., 0] *= 1.0 - 0.35 * t             # R
        img[..., 2] *= 1.0 + 0.35 * t             # B
    img += float(c.get("brightness", 0)) * 2.0
    return np.clip(img, 0, 255).astype(np.uint8)


class _SimCamera:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = cam_id_of(name)
        self.connected = False
        self.want = DEFAULT_WHT
        self.controls: dict[str, float] = {k: v["default"] for k, v in _CONTROL_SPECS.items()}
        self.published = 0
        self.lost_at = 0.0          # 시뮬 카메라는 사라지지 않는다 — 계약상 자리


class SimCameraHub:
    def __init__(self, world_provider) -> None:
        self._world_provider = world_provider     # () -> World (지연 생성)
        self.cams = {n: _SimCamera(n) for n in CAMERAS}
        self._last_apply: dict[str, dict] = {}
        self._renderers: dict[tuple[int, int], object] = {}   # 렌더 스레드 전용
        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self.light_scale = 1.0       # 조명 배율 — 조명 감시(급변·표류) 시험용

    # ── 계약 동사 (camerad 어휘) ──

    def scan(self) -> list[dict]:
        # ⚠ connected 를 싣는다 — 게이트웨이가 이 사실로 자기 connected 를 맞춘다.
        #   simd 가 재시작하면 여기가 False 가 되고, 게이트웨이가 True 로 캐시한 채면
        #   창이 연결을 건너뛰어 렌더가 안 돌고 화면이 빈다(사용자 보고 2026, sim 팔과 같은 버그).
        return [{"id": c.id, "name": f"Sim {c.name}", "usb_port": "sim", "connected": c.connected,
                 "usb_speed_mbps": 0, "cam_type": "sim"} for c in self.cams.values()]

    def _cam(self, cam_id: str) -> _SimCamera | None:
        return next((c for c in self.cams.values() if c.id == cam_id), None)

    def connect(self, cam_id: str, width: int = 0, height: int = 0,
                fps: int = 0, controls: dict | None = None) -> tuple[bool, str]:
        cam = self._cam(cam_id)
        if cam is None:
            return False, f"Unknown camera: {cam_id}"
        cam.want = (int(width), int(height), int(fps)) if width and height and fps else DEFAULT_WHT
        cam.connected = True
        self._ensure_thread()
        if controls:
            self.apply_controls(cam_id, controls)
        return True, f"{cam.want[0]}x{cam.want[1]}@{cam.want[2]}"

    def disconnect(self, cam_id: str) -> None:
        cam = self._cam(cam_id)
        if cam and cam.connected:
            cam.connected = False
            publish.stop(cam_id)

    def release_all(self) -> bool:
        any_ = False
        for c in self.cams.values():
            if c.connected:
                self.disconnect(c.id); any_ = True
        return any_

    def probe(self, cam_id: str) -> tuple[bool, str]:
        """한 장 그려 세그먼트에 남긴다 (스캔 썸네일) — 발행 루프는 안 켠다."""
        cam = self._cam(cam_id)
        if cam is None:
            return False, f"Unknown camera: {cam_id}"
        frame = self._ask(cam)              # ⚠ 이 스레드에서 그리면 EGL 이 깨진다
        if frame is None:
            return False, "렌더 실패 (EGL)"
        publish.publish_frame(cam_id, frame)
        if not cam.connected:
            publish.stop(cam_id, unlink_segment=False)
        return True, "ok"

    def list_controls(self, cam_id: str) -> list[dict]:
        cam = self._cam(cam_id)
        if cam is None:
            return []
        return [{"name": n, "value": cam.controls[n], **spec}
                for n, spec in _CONTROL_SPECS.items()]

    def set_control(self, cam_id: str, name: str, value: float) -> bool:
        cam = self._cam(cam_id)
        spec = _CONTROL_SPECS.get(name)
        if cam is None or spec is None:
            return False
        cam.controls[name] = float(max(spec["min"], min(spec["max"], value)))
        return True

    def apply_controls(self, cam_id: str, wanted: dict, budget_s: float = 2.0) -> dict:
        """camerad 와 **같은 적용기**(`piper_cam.controls.apply_controls`) — 자동 모드
        순서 규칙이 시뮬에서도 같은 함정을 같은 순서로 지난다."""
        report = controls_mod.apply_controls(
            lambda: self.list_controls(cam_id),
            lambda name, value: self.set_control(cam_id, name, value),
            wanted, budget_s=budget_s, label=cam_id)
        self._last_apply[cam_id] = report
        return report

    def last_apply_report(self, cam_id: str) -> dict:
        return self._last_apply.get(cam_id, {})

    def info(self, cam_id: str) -> dict:
        cam = self._cam(cam_id)
        if cam is None:
            return {}
        w, h, f = cam.want
        return {"width": w, "height": h, "fps": f, "connected": cam.connected,
                "published": cam.published}

    def lost(self) -> list[dict]:
        return []

    # ── 시뮬 전용 ──

    def set_light(self, scale: float) -> float:
        """조명 배율. 서서히 올리면 표류 알람이, 확 올리면 급변 알람이 울어야 한다."""
        self.light_scale = float(max(0.05, min(4.0, scale)))
        return self.light_scale

    # ── 렌더 ──

    def _renderer(self, w: int, h: int):
        import mujoco

        key = (w, h)
        r = self._renderers.get(key)
        if r is None:
            r = self._renderers[key] = mujoco.Renderer(self._world_provider().model, height=h, width=w)
        return r

    def _render(self, cam: _SimCamera) -> np.ndarray:
        world = self._world_provider()
        w, h, _ = cam.want
        r = self._renderer(w, h)
        m = world.model
        with world._lock:                      # 물리 스레드와 data 경합
            # 조명 배율 — 월드 라이트 **와 헤드라이트 둘 다** 스케일한다.
            # 실측: 한쪽만 0.3배면 top 이 244→~225 (포화라 거의 안 변함),
            # 둘 다면 244→122. 헤드라이트(카메라 부착 조명+앰비언트)가 씬 밝기의
            # 절반을 낸다 — 그걸 빼면 조명 감시 시험이 성립하지 않는다.
            base = getattr(self, "_light_base", None)
            if base is None:
                base = self._light_base = (m.light_diffuse.copy(),
                                           m.vis.headlight.diffuse.copy(),
                                           m.vis.headlight.ambient.copy())
            m.light_diffuse[:] = base[0] * self.light_scale
            m.vis.headlight.diffuse[:] = base[1] * self.light_scale
            m.vis.headlight.ambient[:] = base[2] * self.light_scale
            r.update_scene(world.data, camera=cam.name)
        rgb = r.render()
        out = emulate(rgb, cam.controls)
        return np.ascontiguousarray(out[..., ::-1])   # RGB → **BGR** (세그먼트 규약)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True, name="sim-render")
            self._thread.start()

    def _ask(self, cam: _SimCamera, timeout_s: float = 5.0):
        """다른 스레드에서 한 장 부탁한다 — 렌더는 렌더 스레드만 한다."""
        self._ensure_thread()
        done = threading.Event()
        box: list = []
        self._jobs.put((cam, box, done))
        if not done.wait(timeout_s):
            return None
        return box[0] if box else None

    def _loop(self) -> None:
        next_t: dict[str, float] = {}
        fails = 0
        while self._running:
            # 1) 요청 큐 (probe 등) — 연결 여부와 무관하게 먼저
            try:
                cam, box, done = self._jobs.get_nowait()
            except queue.Empty:
                pass
            else:
                try:
                    box.append(self._render(cam))
                except Exception as exc:
                    logger.warning("렌더 실패 (%s, 요청): %s", cam.id, exc)
                finally:
                    done.set()
                continue
            # 2) 연결된 카메라 주기 발행
            live = [c for c in self.cams.values() if c.connected]
            if not live:
                time.sleep(0.02)
                continue
            now = time.monotonic()
            due = [c for c in live if now >= next_t.get(c.id, 0.0)]
            if not due:
                time.sleep(0.002)
                continue
            for cam in due:
                try:
                    frame = self._render(cam)
                    if publish.publish_frame(cam.id, frame):
                        cam.published += 1
                    fails = 0
                except Exception as exc:
                    fails += 1
                    if fails in (1, 10, 100):       # 매번 뱉으면 저널이 묻힌다
                        logger.warning("렌더 실패 (%s, %d회째): %s", cam.id, fails, exc)
                    time.sleep(0.1)
                next_t[cam.id] = now + 1.0 / max(1, cam.want[2])

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.release_all()
        for r in self._renderers.values():
            try:
                r.close()
            except Exception:
                pass
        self._renderers.clear()
