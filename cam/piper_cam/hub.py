"""v4l2 카메라 소유 — camerad 가 돌리는 허브.

## RealSenseHub 와 **같은 메서드 이름**을 쓴다

게이트웨이의 `CameraInfo` 가 `cam_type` 으로 허브만 고르면 되게 하려는 것이다.
이름이 갈리면 호출부마다 분기가 생기고, 그 분기가 곧 두 번째 진실이 된다.

    scan · connect · disconnect · release_all · probe · list_controls · set_control

프레임은 여기 없다 — `/dev/shm` 세그먼트로 나가고 소비자가 직접 읽는다.
"""

import logging
import threading

from piper_cam import v4l2

logger = logging.getLogger(__name__)


class _V4l2Camera:
    """카메라 하나. 열면 백그라운드로 계속 읽어 세그먼트에 흘린다."""

    def __init__(self, dev_path: str) -> None:
        self.id = dev_path
        self._cap = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self.width: int | None = None
        self.height: int | None = None
        self.fps: int | None = None
        # 요청 프로파일 `(w, h, fps)`. None 이면 드라이버 기본값.
        self._want: tuple[int, int, int] | None = None

    @property
    def connected(self) -> bool:
        return self._cap is not None

    def _open(self):
        import cv2

        cap = cv2.VideoCapture(self.id, cv2.CAP_V4L2)
        if not cap.isOpened():
            return None, None
        # 요청 프로파일 적용. **드라이버가 거절해도 조용히 무시한다** — 아래에서
        # 실제 값을 다시 읽으므로, 못 맞춘 채로 맞췄다고 착각할 일은 없다.
        if self._want:
            w, h, fps = self._want
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, fps)
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            return None, None
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = int(cap.get(cv2.CAP_PROP_FPS)) or 30
        return cap, frame

    def connect(self, want: tuple[int, int, int] | None = None) -> tuple[bool, str]:
        # 이미 열려 있는데 다른 프로파일을 요청하면 다시 연다 — 안 그러면
        # UI 에서 해상도를 바꿔도 예전 설정 그대로 돈다.
        if self.connected and want is not None and want != self._want:
            self.disconnect()
        if want is not None:
            self._want = want
        if self.connected:
            return True, "OK"
        try:
            cap, frame = self._open()
        except Exception as exc:
            return False, str(exc)
        if cap is None:
            return False, f"Cannot open {self.id}"
        self._cap = cap
        self._running = True
        self._publish(frame)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True, "OK"

    def _publish(self, frame) -> None:
        from piper_cam.publish import publish_frame

        publish_frame(self.id, frame)

    def _loop(self) -> None:
        while self._running and self._cap:
            try:
                ok, frame = self._cap.read()
                if ok and frame is not None:
                    self._publish(frame)
            except Exception:
                break

    def disconnect(self) -> None:
        from piper_cam.publish import stop as stop_publish

        # ⚠ **순서가 중요하다.** 읽기 스레드를 먼저 멈춘다 — 발행자를 먼저 닫으면
        # 그 사이 루프가 한 프레임 더 발행해 **세그먼트를 되살린다.**
        # (rsd 에서 실제로 겪은 버그다.)
        self._running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        stop_publish(self.id)

    def probe(self, timeout: float = 3.0) -> tuple[bool, str]:
        """연결 테스트 + 프레임 1장 → 즉시 해제. 스캔용."""
        import concurrent.futures

        def _do():
            cap, frame = self._open()
            if cap is None:
                return False, f"Cannot open {self.id}"
            self._publish(frame)      # 스캔 썸네일 — 세그먼트를 지우지 않고 남긴다
            cap.release()
            return True, "OK"

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(_do).result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return False, f"Timeout ({timeout}s) probing {self.id}"
        except Exception as exc:
            return False, str(exc)


class V4l2Hub:
    def __init__(self) -> None:
        self.cams: dict[str, _V4l2Camera] = {}
        self._info: dict[str, dict] = {}

    def scan(self) -> list[dict]:
        found = v4l2.scan_cameras()
        for d in found:
            self._info[d["id"]] = d
            self.cams.setdefault(d["id"], _V4l2Camera(d["id"]))
        return found

    def _cam(self, cam_id: str) -> _V4l2Camera | None:
        if cam_id not in self.cams and cam_id.startswith("/dev/"):
            self.cams[cam_id] = _V4l2Camera(cam_id)
        return self.cams.get(cam_id)

    def connect(self, cam_id: str, width: int = 0, height: int = 0,
                fps: int = 0) -> tuple[bool, str]:
        """셋 다 주면 그 프로파일로 연다. 하나라도 0이면 드라이버 기본값."""
        cam = self._cam(cam_id)
        if not cam:
            return False, f"Unknown camera: {cam_id}"
        want = (int(width), int(height), int(fps)) if width and height and fps else None
        return cam.connect(want)

    def disconnect(self, cam_id: str) -> None:
        cam = self.cams.get(cam_id)
        if cam:
            cam.disconnect()

    def release_all(self) -> bool:
        released = False
        for cam in self.cams.values():
            if cam.connected:
                cam.disconnect()
                released = True
        return released

    def probe(self, cam_id: str) -> tuple[bool, str]:
        cam = self._cam(cam_id)
        return cam.probe() if cam else (False, f"Unknown camera: {cam_id}")

    def list_controls(self, cam_id: str) -> list[dict]:
        return v4l2.v4l2_list_controls(cam_id)

    def set_control(self, cam_id: str, name: str, value: float) -> bool:
        for ctrl in v4l2.v4l2_list_controls(cam_id):
            if ctrl["name"] == name:
                return v4l2.v4l2_set_control(cam_id, ctrl["cid"], int(value))
        return False

    def info(self, cam_id: str) -> dict:
        """해상도 등 — 연결 중이면 실제 값, 아니면 스캔 값."""
        cam = self.cams.get(cam_id)
        base = dict(self._info.get(cam_id) or {})
        base["connected"] = bool(cam and cam.connected)
        # 이미 반영한 요청 — rsd 와 같은 계약이다(게이트웨이가 둘을 구분 안 한다)
        base["want"] = list(cam._want or ()) if cam else []
        if cam and cam.connected:
            base.update(width=cam.width, height=cam.height, fps=cam.fps)
        return base
