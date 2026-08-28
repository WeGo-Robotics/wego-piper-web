"""RealSense 파이프라인 소유 — **rsd 데몬의 본체**.

게이트웨이(`backend/`)를 import 하지 않는다. 데몬이 백엔드에 의존하면 분리한 의미가 없다.
백엔드 결합은 shm 발행 하나뿐이었고 그것도 `piper_shm` 으로 갈아끼웠다.

원래 `backend/app/services/realsense_manager.py` 였다 (refactor/daemon-inventory.md #4).
게이트웨이 쪽에는 같은 이름의 **얇은 버스 클라이언트**가 남는다 — 라우터는 그대로다.
"""

_ORIG_DOC = """
Intel RealSense 카메라 관리 (pyrealsense2 기반).

OpenCV/V4L2로는 RealSense의 Depth(Z16)·IR(Y8) 스트림을 열 수 없으므로
librealsense를 직접 사용한다. 한 물리 디바이스(시리얼)당 파이프라인 1개를 열고
color/depth/infrared 스트림을 공유하며, 각 스트림을 UI에서 별도 카메라 엔트리
("rs:<serial>:<stream>")로 노출한다.

camera_manager 의 OpenCV 경로와 분리된 협력 모듈. RealSense가 아닌 일반 웹캠은
기존 OpenCV 경로를 그대로 사용한다.
"""

import contextlib
import logging
import re
import threading
import time

import numpy as np

from piper_cam import controls as controls_mod
from piper_rs.depth import DEFAULT_UNITS_M, DepthEncoding, encode_depth
from piper_rs.mask import apply_mask, shapes_match

logger = logging.getLogger(__name__)

# UI/엔트리에서 다루는 스트림 종류
STREAM_TYPES = ("color", "depth", "infrared")

# rs 미설치 환경에서도 import 가 깨지지 않도록 지연 로딩
try:
    import pyrealsense2 as rs  # type: ignore
    _RS_AVAILABLE = True
except Exception as exc:  # pragma: no cover - 환경 의존
    rs = None  # type: ignore
    _RS_AVAILABLE = False
    logger.info("pyrealsense2 unavailable: %s", exc)


def rs_available() -> bool:
    return _RS_AVAILABLE


def _run_guarded(fn, timeout: float, default, what: str):
    """블로킹 librealsense 호출을 데몬 스레드에서 돌리고 timeout 초과 시 default 반환.

    D405 등에서 UVC 컨트롤(XU) 질의가 커널 uvcvideo 드라이버 안에서 D-state로
    무한히 멈추는 경우가 있다. 이때 호출 스레드는 SIGKILL로도 회수 불가능하므로,
    서버 스레드가 영구히 잡히지 않도록 시간 상한을 강제한다. 초과한 호출 스레드는
    격리(누수)되지만 서버는 살아남는다."""
    result = [default]
    done = threading.Event()

    def runner():
        try:
            result[0] = fn()
        except Exception as exc:
            logger.warning("RealSense %s failed: %s", what, exc)
        finally:
            done.set()

    threading.Thread(target=runner, daemon=True).start()
    if not done.wait(timeout):
        logger.error(
            "RealSense %s timed out after %.1fs (orphaned thread; device may be hung in kernel)",
            what, timeout,
        )
        return default
    return result[0]


def _short_usb_port(physical_port: str) -> str:
    """librealsense physical_port(긴 sysfs 경로)에서 'N-N[.N]' 버스-포트 토큰 추출.
    OpenCV 경로의 usb_port 표기('4-3:1.0')와 형식을 맞춘다."""
    if not physical_port:
        return ""
    m = re.search(r"\b(\d+-\d+(?:\.\d+)*(?::\d+\.\d+)?)\b", physical_port)
    return m.group(1) if m else physical_port


def make_id(serial: str, stream: str) -> str:
    return f"rs:{serial}:{stream}"


def parse_id(cam_id: str) -> tuple[str, str] | None:
    """'rs:<serial>:<stream>' → (serial, stream). RealSense id가 아니면 None."""
    if not cam_id.startswith("rs:"):
        return None
    parts = cam_id.split(":")
    if len(parts) != 3:
        return None
    _, serial, stream = parts
    return serial, stream


def to_bgr(img: "np.ndarray", fmt) -> "np.ndarray":
    """librealsense 색 프레임 → BGR (우리가 발행하는 포맷).

    **무엇이 왔는지 보고 정한다.** 스트림을 여는 코드와 읽는 코드가 떨어져 있어서,
    한쪽이 포맷을 바꿔도 다른 쪽은 모른 채 계속 같은 변환을 돌린다. 실제로 그렇게
    노란색이 파란색이 됐다 — 색이 틀린 화면은 정책 입력으로도 그대로 틀린다.
    """
    import cv2  # 이 파일의 규약 — cv2 는 쓰는 자리에서 import 한다

    name = str(fmt)
    if name.endswith("rgb8"):
        return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    if name.endswith("rgba8"):
        return cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    if name.endswith("bgra8"):
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    # bgr8 — 이미 맞다. 모르는 포맷도 손대지 않는다: 잘못 바꾸는 것보다
    # 그대로 두는 편이 원인을 찾기 쉽다.
    return img


class _RSDevice:
    """물리 디바이스 1대. 파이프라인 1개를 활성 스트림 집합에 맞춰 운용."""

    def __init__(self, serial: str, model: str, usb_port: str, available: set[str]):
        self.serial = serial
        self.model = model
        self.usb_port = usb_port
        self.available = available  # 이 디바이스가 제공 가능한 스트림 종류
        self._pipeline = None
        self._active: set[str] = set()  # 현재 파이프라인이 스트리밍 중인 스트림
        self._refcount: dict[str, int] = {s: 0 for s in STREAM_TYPES}
        self._latest: dict[str, object] = {}  # stream -> numpy frame (BGR/grey)
        self._logged_color_fmt = None  # 열린 색 포맷을 한 번만 로그로
        self._lock = threading.Lock()       # _latest 보호
        # 디바이스 작업 직렬화: 한 디바이스에 동시에 두 파이프라인을 start 할 수
        # 없고, librealsense 는 디바이스 단위로 thread-safe 하지 않다. 따라서
        # connect/disconnect/probe(파이프라인) 와 list/set_control(센서 옵션) 를
        # 모두 이 락으로 직렬화한다 — 그렇지 않으면 컨트롤 질의와 스트림 시작이
        # 같은 USB 디바이스에서 충돌해 uvcvideo 가 D-state 로 멈춘다.
        self._op_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._running = False
        # 깊이 인코딩 파라미터. **rsd 가 단일 소유자다** — 같은 픽셀값이 실행마다
        # 다른 거리를 뜻하면 데이터셋이 조용히 오염된다. `info()` 로 내보낸다.
        self.depth_encoding = DepthEncoding()
        # raw 한 단위가 몇 미터인가. **장치가 답한다** — D435 는 0.001 이지만
        # D405 는 0.0001 이라 안 물어보면 10배 틀린 거리를 인코딩한다.
        self.depth_units_m: float = DEFAULT_UNITS_M
        # 깊이로 컬러의 배경을 지울 것인가.
        self.mask_background: bool = False
        # 지우는 경계(mm). **None 이면 깊이 창의 `far_mm` 을 따라간다** — 처음엔
        # 그 값만 썼는데, 인코딩 창은 해상도를 위해 좁히고 마스킹은 더 멀리까지
        # 남기고 싶은 경우가 갈렸다. 기본을 따라가게 두면 둘을 맞출 필요가 없고,
        # 필요할 때만 떼어낼 수 있다.
        self.mask_far_mm: float | None = None
        # 깊이를 못 읽은 픽셀을 남길 것인가. 기본은 남긴다 — D405 는 프레임의
        # 42% 가 무효라, 지우면 물체 한가운데가 뚫린다. 다만 깊이가 잘 잡히는
        # 장면에서는 지우는 쪽이 깔끔해서 **장면이 정할 일**이다.
        self.mask_keep_unknown: bool = True
        # 정렬기. **마스킹을 켤 때만 만든다** — 매 프레임 비용이 붙는다.
        self._align = None
        self._mask_warned = False
        # 스트림별 요청 프로파일 `(w, h, fps)`. 없으면 장치 기본값.
        self._want: dict[str, tuple[int, int, int]] = {}
        self._profiles: dict[str, list[tuple[int, int, int]]] = {}  # supported() 캐시
        # 지금 파이프라인이 **실제로** 돌리고 있는 프로파일. 요청과 다를 수 있다.
        self._running_profile: dict[str, tuple[int, int, int] | None] = {}
        # 장치가 사라졌다고 **데몬이 판정한** 시각. 게이트웨이가 추론하지 않게 한다.
        self.lost_at: float = 0.0

    @contextlib.contextmanager
    def op_guard(self, timeout: float = 6.0):
        """op_lock 을 시간 상한을 두고 획득한다. 이전 작업 스레드가 커널 D-state
        로 멈춰 락을 영구히 쥐고 있어도(고아 스레드) 서버가 무한 대기하지 않도록,
        획득 실패 시 TimeoutError 를 던져 호출자가 깔끔히 포기하게 한다."""
        if not self._op_lock.acquire(timeout=timeout):
            raise TimeoutError(
                f"RealSense {self.serial} busy (op_lock held >{timeout}s; device may be hung)"
            )
        try:
            yield
        finally:
            self._op_lock.release()

    # ── 파이프라인 구성 ──

    def _can_widen(self) -> bool:
        """스캔 최적화(전 스트림 한 번에 켜기)를 써도 되는가.

        USB 2.0 이면 안 된다 — 대역폭이 모자라 실패하고, 그 실패가 장치를 물린다.
        이미 누가 스트림을 쥐고 있어도 안 된다 — 그 스트림을 굶길 수 있다.
        """
        if any(c > 0 for c in self._refcount.values()):
            return False
        return self.usb_speed_mbps() >= 5000

    def usb_speed_mbps(self) -> int:
        """USB 링크 속도(Mbps). 못 읽으면 **넉넉한 쪽으로 가정하지 않는다**(0 을 준다) —
        모르는 채로 대역폭을 밀어붙이면 장치가 물린다.

        ⚠ 시리얼로 찾지 않는다. RealSense 는 sysfs 에 `serial` 파일을 노출하지 않아
        늘 못 찾았다(그래서 로그가 "0 Mbps" 라고 거짓말했다).
        librealsense 가 준 `physical_port` 에서 뽑아둔 `usb_port`(`3-11.1`)로 찾는다.
        """
        from pathlib import Path

        node = (self.usb_port or "").split(":")[0]
        if not node:
            return 0
        try:
            return int(float(Path(f"/sys/bus/usb/devices/{node}/speed").read_text().strip()))
        except Exception:
            return 0

    def is_d405(self) -> bool:
        return "405" in (self.model or "")

    def supported(self, stream: str) -> list[tuple[int, int, int]]:
        """이 스트림이 낼 수 있는 `(w, h, fps)` 목록. 장치가 답한 그대로다.

        캐시한다 — 매번 물으면 USB 왕복이 늘고, 프로파일 목록은 펌웨어 상수다.
        """
        cached = self._profiles.get(stream)
        if cached is not None:
            return cached
        target = {"color": rs.stream.color, "depth": rs.stream.depth,
                  "infrared": rs.stream.infrared}.get(stream)
        found: set[tuple[int, int, int]] = set()
        if target is not None:
            try:
                for d in rs.context().query_devices():
                    if d.get_info(rs.camera_info.serial_number) != self.serial:
                        continue
                    for sensor in d.query_sensors():
                        for p in sensor.get_stream_profiles():
                            if not p.is_video_stream_profile() or p.stream_type() != target:
                                continue
                            # color 는 bgr8 로 받는다(발행 포맷). depth/IR 은 포맷이 하나뿐.
                            if stream == "color" and not str(p.format()).endswith("bgr8"):
                                continue
                            v = p.as_video_stream_profile()
                            found.add((v.width(), v.height(), p.fps()))
            except Exception as exc:
                logger.warning("프로파일 조회 실패 (%s/%s): %s", self.serial, stream, exc)
        out = sorted(found)
        self._profiles[stream] = out
        return out

    def resolve(self, stream: str, want: tuple[int, int, int] | None
                ) -> tuple[int, int, int] | None:
        """요청 프로파일 → **장치가 실제로 낼 수 있는** 것.

        ⚠ 요청을 그대로 `enable_stream` 에 넘기면 안 된다. 장치에 없는 조합이면
        `pipeline.start` 가 통째로 실패해 카메라가 아예 안 열린다.
        실제로 D405 에는 **848x480@15 가 없다**(10 이 상한) — UI 에서 15 를 고르면
        여기서 걸러내지 않는 한 녹화가 시작조차 못 하거나 조용히 10fps 로 돈다.

        고르는 순서:
          1. 정확히 같은 것
          2. 같은 해상도에서 요청 이하의 가장 빠른 fps
          3. 요청 fps 를 낼 수 있는 해상도 중 요청 화소수에 가장 가까운 것
          4. 못 찾으면 None → 장치 기본값에 맡긴다
        """
        if want is None:
            return None
        modes = self.supported(stream)
        if not modes:
            return None
        w, h, fps = want
        if (w, h, fps) in modes:
            return (w, h, fps)

        same_res = [m for m in modes if (m[0], m[1]) == (w, h) and m[2] <= fps]
        if same_res:
            best = max(same_res, key=lambda m: m[2])
            logger.info("%s/%s: %dx%d@%d 없음 → %dx%d@%d", self.serial, stream,
                        w, h, fps, *best)
            return best

        same_fps = [m for m in modes if m[2] == fps]
        if same_fps:
            best = min(same_fps, key=lambda m: abs(m[0] * m[1] - w * h))
            logger.info("%s/%s: %dx%d@%d 없음 → %dx%d@%d", self.serial, stream,
                        w, h, fps, *best)
            return best

        logger.warning("%s/%s: %dx%d@%d 에 맞출 프로파일이 없어 기본값을 쓴다",
                       self.serial, stream, w, h, fps)
        return None

    def _build_config(self, streams: set[str]):
        cfg = rs.config()
        rs.config.enable_device(cfg, self.serial)
        # D405는 단일 Stereo Module에서 color를 뽑는데, depth가 함께 켜져 있지
        # 않으면 color 프레임이 아예 안 나온다(color-only=0 fps). color 요청 시
        # depth를 조용히 함께 켜 준다(refcount/_active엔 안 들어가 read_loop가
        # depth를 굳이 추출하진 않음 — 파이프라인만 활성화).
        enable = set(streams)
        if "color" in enable and self.is_d405() and "depth" in self.available:
            enable.add("depth")
        for s in enable:
            # 요청이 없으면 인자 없이 켠다 = librealsense 기본 프로파일.
            # ⚠ 기본값은 우리가 고른 값이 아니다. D405 는 848x480@**10** 으로 떨어져
            # 녹화 루프를 10Hz 에 묶어버렸다. 그래서 요청을 여기까지 끌고 온다.
            got = self.resolve(s, self._want.get(s))
            try:
                if s == "color":
                    if got:
                        cfg.enable_stream(rs.stream.color, got[0], got[1],
                                          rs.format.bgr8, got[2])
                    else:
                        # 포맷을 안 건다 = librealsense 기본(보통 rgb8). 위 갈래와
                        # 포맷이 다르다 — 읽는 쪽은 `to_bgr` 가 프레임을 보고 맞춘다.
                        cfg.enable_stream(rs.stream.color)
                elif s == "depth":
                    # depth 는 color 를 살리려고 곁들이는 것이라 해상도를 맞추지 않는다
                    cfg.enable_stream(rs.stream.depth)
                elif s == "infrared":
                    # D435 계열은 IR 좌(index 1)를 사용
                    cfg.enable_stream(rs.stream.infrared, 1)
            except Exception as exc:
                logger.warning("enable_stream(%s) failed for %s: %s", s, self.serial, exc)
        return cfg

    def _ensure_streams(self, streams: set[str]) -> bool:
        """파이프라인이 주어진 스트림 집합을 스트리밍하도록 보장 (필요시 재시작)."""
        streams = {s for s in streams if s in self.available}
        if not streams:
            return False
        # 스트림 집합만이 아니라 **프로파일이 바뀌어도** 재시작해야 한다.
        # 안 그러면 UI 에서 해상도를 바꿔도 예전 파이프라인이 그대로 돈다.
        # ⚠ 요청이 없는(None) 스트림은 비교 대상이 아니다 — 장치 기본값과 비교하면
        # 영원히 "다르다"가 되어 매번 재시작한다.
        wanted = {s: self.resolve(s, self._want.get(s)) for s in streams}
        if (self._pipeline is not None and streams.issubset(self._active)
                and all(p is None or self._running_profile.get(s) == p
                        for s, p in wanted.items())
                and self._segments_alive(streams)):
            return True
        # 재구성 필요 → 기존 정지 후 재시작.
        # ⚠ **세그먼트는 남긴다.** 이건 장치를 놓는 게 아니라 잠깐 접었다 펴는 것이다.
        # 지우면 두 가지가 깨진다:
        #   1. 아직 쓰고 있는 소비자가 그 순간 `SegmentError` 로 죽는다
        #   2. 이어지는 `pipeline.start` 가 실패하면(카메라 2대가 USB 대역폭을
        #      나눠 쓸 때 실제로 일어난다) 세그먼트가 **지워진 채로 남는다** —
        #      D435 가 이렇게 조용히 사라졌다.
        # 진짜로 놓을 때(`disconnect_stream` 이 refcount 0 을 보고 부를 때)만 지운다.
        self._stop_pipeline(unlink_segments=False)
        pipeline = rs.pipeline()
        cfg = self._build_config(streams)
        try:
            pipeline.start(cfg)
        except Exception as exc:
            logger.error("RealSense %s pipeline start failed (%s): %s", self.serial, streams, exc)
            return False
        self._pipeline = pipeline
        self._active = set(streams)
        # **장치가 실제로 연 값**을 기록한다. 요청 해석값을 그대로 믿으면,
        # 요청이 없어 기본값으로 열린 스트림의 해상도를 영영 모른다.
        self._running_profile = self._read_active_profiles(pipeline, streams)
        if "depth" in self._active:
            self.depth_units_m = self._read_depth_units(pipeline)
        self._start_thread()
        return True

    def _read_depth_units(self, pipeline) -> float:
        """raw 한 단위가 몇 미터인가. **장치에 묻는다.**

        ⚠ **여기서만 묻는다** — `pipeline.start` 직후, `_op_lock` 안이다.
        읽기 루프에서 물으면 D405 의 UVC 질의가 커널 D-state 로 프로세스를
        통째로 먹통으로 만든 그 경로를 매 프레임 타게 된다. 값은 스트림이
        도는 동안 안 바뀌므로 한 번이면 된다.

        못 읽으면 기본값으로 간다 — 발행을 멈추는 것보다 낫다. 다만 D405 라면
        그 기본값이 10배 틀리므로 **조용히 넘어가지 않는다.**
        """
        try:
            scale = float(pipeline.get_active_profile().get_device()
                          .first_depth_sensor().get_depth_scale())
        except Exception as exc:
            logger.warning("RealSense %s: depth_units 를 못 읽어 %s 로 갑니다 — "
                           "이 장치가 D405 면 거리가 10배 틀립니다: %s",
                           self.serial, DEFAULT_UNITS_M, exc)
            return DEFAULT_UNITS_M
        if scale <= 0:
            logger.warning("RealSense %s: depth_units 가 %s 다 — 기본값으로 갑니다",
                           self.serial, scale)
            return DEFAULT_UNITS_M
        logger.info("RealSense %s: depth_units=%g (raw 1 = %.3gmm)",
                    self.serial, scale, scale * 1000.0)
        return scale

    @staticmethod
    def _read_active_profiles(pipeline, streams: set[str]
                              ) -> dict[str, tuple[int, int, int] | None]:
        out: dict[str, tuple[int, int, int] | None] = {}
        try:
            active = pipeline.get_active_profile()
        except Exception as exc:
            logger.warning("활성 프로파일 조회 실패: %s", exc)
            return dict.fromkeys(streams)
        for s in streams:
            target = {"color": rs.stream.color, "depth": rs.stream.depth,
                      "infrared": rs.stream.infrared}.get(s)
            try:
                v = active.get_stream(target).as_video_stream_profile()
                out[s] = (v.width(), v.height(), v.fps())
            except Exception:
                out[s] = None
        return out

    def _segments_alive(self, streams: set[str]) -> bool:
        """스트리밍 중이라는 말이 참이려면 **세그먼트가 있어야 한다.**

        ⚠ 세그먼트의 존재 자체가 lease 다. 누가 지우면 발행자는 열린 fd 로 계속
        쓰지만 소비자는 열 수 없다 — 그런데 `_active` 는 여전히 "스트리밍 중"이라
        `connect` 가 0초에 OK 를 돌려준다. 그래서 추론이 시작 직후
        `SegmentError` 로 죽는다(실제로 D435 가 이랬다).

        여기서 걸러내면 파이프라인을 다시 세워 세그먼트가 되살아난다.
        """
        from piper_shm import segment_path

        return all(segment_path(f"rs_{self.serial}_{s}").exists() for s in streams)

    def _stop_pipeline(self, unlink_segments: bool = True,
                       device_gone: bool = False) -> None:
        from piper_rs.publish import stop as stop_publish

        stopping = set(self._active)
        # ⚠ **순서가 중요하다.** 읽기 스레드를 먼저 멈춘다 — 발행자를 먼저 닫으면
        # 그 사이 루프가 한 프레임 더 발행해 **세그먼트를 되살린다.**
        # (그러면 소비자가 발행자 없는 세그먼트를 열어 "멈춘 화면"을 본다.)
        self._running = False
        t = self._thread
        # ⚠ **읽기 스레드가 자기 자신을 부를 수 있다.** `_declare_lost()` 는 루프
        # 안에서 불리는데, 거기서 자기를 join 하면 `RuntimeError: cannot join
        # current thread` 로 **이 함수가 여기서 끊긴다** — 세그먼트도 안 지워지고
        # 파이프라인도 안 닫힌 채로. 실기에서 D405 가 USB 에서 떨어졌을 때
        # 그 상태가 됐고, rsd 를 재시작하기 전엔 다시 잡히지 않았다.
        #
        # 자기 자신일 때 join 을 건너뛰어도 안전하다: 부른 쪽이 곧 `return` 하고,
        # 같은 스레드라 그 사이 프레임이 하나 더 나갈 수 없다 — 위 순서 규칙이
        # 지키려던 것이 바로 그것이다.
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2)
        self._thread = None
        for stream in stopping:
            stop_publish(f"rs:{self.serial}:{stream}", unlink_segment=unlink_segments)
        if self._pipeline is not None:
            # ⚠ **장치가 이미 사라졌으면 `stop()` 을 부르지 않는다.**
            #
            #   librealsense 가 없는 USB 장치의 전송을 정리하려다 힙을 깨뜨린다.
            #   그건 파이썬 예외가 아니라 **SIGABRT** 라 `except` 로 못 잡고,
            #   프로세스가 통째로 코어덤프한다 — 카메라 한 대가 빠졌을 뿐인데
            #   **나머지 두 대까지 같이 죽는다.** 실측(2026-08-28):
            #
            #       [WARNING] 335122271186: USB 노드가 사라졌습니다
            #       free(): corrupted unsorted chunks
            #       piper-rsd.service: Main process exited, code=dumped, status=6/ABRT
            #
            #   같은 날 `double free or corruption (!prev)` 로도 한 번 죽었다.
            #
            #   그래서 참조만 버린다. **파이프라인 하나를 흘리는 셈이지만**,
            #   장치가 없으니 그게 붙잡고 있는 USB 자원도 이미 없다. 데몬이
            #   살아서 나머지 카메라를 계속 발행하는 편이 낫다 — 재연결은
            #   어차피 새 파이프라인을 연다.
            if device_gone:
                logger.warning("RealSense %s: 장치가 없어 파이프라인 정리를 "
                               "건너뜁니다 (librealsense 가 죽습니다)", self.serial)
            else:
                try:
                    self._pipeline.stop()
                except Exception:
                    pass
            self._pipeline = None
        self._active = set()
        self._running_profile = {}

    # ── 프레임 캡처 ──

    def _start_thread(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    # USB 노드가 아직 있는지 보는 주기. sysfs 경로 확인이라 사실상 공짜다.
    _PRESENCE_S = 1.0
    # 노드는 남아 있는데 프레임만 안 오는 경우의 상한.
    _MAX_EMPTY = 5          # try_wait_for_frames(timeout_ms=1000) 기준 = 약 5초

    def _device_present(self) -> bool:
        """이 카메라의 USB 노드가 아직 있는가 — **결정적 증거**다.

        뽑으면 커널이 `/sys/bus/usb/devices/<포트>` 를 즉시 지운다.
        camerad 의 `/dev/videoN`, robotd 의 `/sys/class/net/can0` 과 같은 신호다.

        ⚠ **librealsense 에 다시 묻지 않는다.** `query_devices()` 를 주기적으로
        부르면 D405 를 커널 D-state 로 물리게 하는 그 질의를 늘리는 셈이고,
        프로세스를 나눠 얻은 격리를 스스로 깨는 짓이다. 이건 파일 존재 확인뿐이라
        장치를 건드리지 않는다.

        포트를 모르면(빈 문자열) 판정하지 않는다 — 모르는 것과 없는 것은 다르다.
        """
        from pathlib import Path

        node = (self.usb_port or "").split(":")[0]
        if not node:
            return True
        return Path(f"/sys/bus/usb/devices/{node}").exists()

    def _aligned_depth(self, frames):
        """마스킹용 **컬러에 정렬된** 깊이. 켜져 있지 않으면 None.

        ⚠ 켜졌을 때만 정렬한다. `rs.align` 은 프레임마다 재투영을 돌리므로
        안 쓰는 실행에 비용을 물리면 안 된다.
        """
        if not (self.mask_background and {"color", "depth"} <= self._active):
            return None
        try:
            if self._align is None:
                self._align = rs.align(rs.stream.color)
            df = self._align.process(frames).get_depth_frame()
            return np.asanyarray(df.get_data()) if df else None
        except Exception as exc:
            if not self._mask_warned:
                self._mask_warned = True
                logger.warning("RealSense %s: 깊이 정렬 실패 — 배경 마스킹을 건너뜁니다: %s",
                               self.serial, exc)
            return None

    def _mask_color(self, bgr, depth_aligned):
        """배경을 지운 컬러. 모양이 안 맞으면 **원본 그대로** 돌려준다.

        어긋난 마스크는 안 하느니만 못하다 — 물체를 엉뚱한 데서 지운다.
        """
        if not shapes_match(bgr, depth_aligned):
            if not self._mask_warned:
                self._mask_warned = True
                logger.warning("RealSense %s: 정렬 결과가 컬러와 안 맞아(%s vs %s) "
                               "배경 마스킹을 건너뜁니다",
                               self.serial, bgr.shape[:2], depth_aligned.shape)
            return bgr
        return apply_mask(bgr, depth_aligned, self.effective_mask_far_mm,
                          self.depth_units_m, keep_unknown=self.mask_keep_unknown)

    @property
    def effective_mask_far_mm(self) -> float:
        """지금 실제로 쓰이는 경계. 따로 안 정했으면 깊이 창을 따라간다."""
        return float(self.mask_far_mm if self.mask_far_mm else self.depth_encoding.far_mm)

    def _read_loop(self) -> None:
        import numpy as np
        import cv2
        empty = 0
        next_presence = 0.0
        while self._running and self._pipeline is not None:
            now = time.monotonic()
            if now >= next_presence:
                next_presence = now + self._PRESENCE_S
                if not self._device_present():
                    self._declare_lost("USB 노드가 사라졌습니다")
                    return
            try:
                ok, frames = self._pipeline.try_wait_for_frames(timeout_ms=1000)
                if not ok or frames is None:
                    empty += 1
                    if empty >= self._MAX_EMPTY:
                        self._declare_lost("프레임이 오지 않습니다")
                        return
                    continue
                empty = 0
                updates: dict[str, object] = {}
                # ⚠ 정렬한 깊이는 **마스킹에만** 쓴다. 발행하는 깊이는 원본
                #   그대로다 — 정렬하면 시점과 해상도가 컬러 쪽으로 바뀌는데,
                #   그걸 조용히 바꾸면 예전 데이터셋과 뜻이 달라진다.
                aligned_depth = self._aligned_depth(frames)
                if "color" in self._active:
                    cf = frames.get_color_frame()
                    if cf:
                        # ⚠ 포맷을 **가정하지 않는다.** 여기는 rgb8 이라고 적혀
                        #   있었는데, 프로파일 요청이 닿게 고치면서 색 스트림이
                        #   bgr8 로 열리기 시작했다. 그래도 RGB→BGR 을 계속 돌려
                        #   R 과 B 를 맞바꿨다 — 노란 물체가 파랗게 나왔다.
                        #   해상도를 지정한 카메라만 그래서 더 안 보였다.
                        img = np.asanyarray(cf.get_data())
                        fmt = cf.get_profile().format()
                        if fmt != self._logged_color_fmt:
                            # 열린 포맷을 한 번 남긴다. 이 값을 몰라서 색이 뒤집힌
                            # 것을 한참 못 찾았다 — 로그 한 줄이면 끝날 일이었다.
                            logger.info("색 스트림 포맷: %s (%s)", fmt, self.serial)
                            self._logged_color_fmt = fmt
                        bgr = to_bgr(img, fmt)
                        if aligned_depth is not None:
                            bgr = self._mask_color(bgr, aligned_depth)
                        updates["color"] = bgr
                if "depth" in self._active:
                    df = frames.get_depth_frame()
                    if df:
                        depth = np.asanyarray(df.get_data())  # uint16 (mm)
                        # ⚠ 프리뷰용 JET 컬러맵을 쓰지 않는다 — 단조롭지 않아
                        # 정책 입력으로 최악이고, 무효 픽셀(0)이 "가장 가까움"이
                        # 된다. `depth.encode_depth` 참고.
                        updates["depth"] = encode_depth(depth, self.depth_encoding,
                                                        self.depth_units_m)
                if "infrared" in self._active:
                    irf = frames.get_infrared_frame(1)
                    if irf:
                        ir = np.asanyarray(irf.get_data())  # y8 grey
                        updates["infrared"] = cv2.cvtColor(ir, cv2.COLOR_GRAY2BGR)
                if updates:
                    with self._lock:
                        self._latest.update(updates)
                    # 스트림마다 **항상** 세그먼트에 흘린다 — 어떤 실행에 어떤 키로
                    # 쓰일지는 발행자가 알 바 아니다 (rsd 로 옮겨도 그대로).
                    from piper_rs.publish import publish_frame

                    for stream, frame in updates.items():
                        publish_frame(f"rs:{self.serial}:{stream}", frame)
            except Exception as exc:
                # ⚠ **예외도 실패로 센다.** 장치를 뽑으면 librealsense 가 던지는데,
                # 예전에는 여기서 삼키고 0.1초 자며 영원히 돌았다 — `empty` 가 안 늘어
                # 판정에 **도달할 수가 없었다.** 그래서 뽑아도 아무 일이 없었다.
                empty += 1
                logger.debug("RealSense %s read error: %s", self.serial, exc)
                if empty >= self._MAX_EMPTY:
                    self._declare_lost(f"읽기가 계속 실패합니다 ({exc})")
                    return
                time.sleep(0.1)

    def get_frame(self, stream: str):
        with self._lock:
            return self._latest.get(stream)

    # ── 연결 수명주기 (스트림 단위 refcount) ──

    def connect_stream(self, stream: str, want: tuple[int, int, int] | None = None) -> bool:
        if stream not in self.available:
            return False
        with self._op_lock:
            if want is not None:
                self._want[stream] = want
            self._refcount[stream] += 1
            ok = self._ensure_streams({s for s, c in self._refcount.items() if c > 0})
            if not ok:
                self._refcount[stream] = max(0, self._refcount[stream] - 1)
            return ok

    def disconnect_stream(self, stream: str) -> None:
        # 사용자가 일부러 닫는 것 — 사라진 게 아니므로 판정을 지운다
        self.lost_at = 0.0
        with self._op_lock:
            if self._refcount.get(stream, 0) > 0:
                self._refcount[stream] -= 1
            active = {s for s, c in self._refcount.items() if c > 0}
            if not active:
                self._stop_pipeline()
                with self._lock:
                    self._latest.clear()
            else:
                self._ensure_streams(active)

    def _declare_lost(self, why: str = "프레임이 오지 않습니다") -> None:
        """장치가 없어진 것으로 판정하고 발행을 끊는다.

        세그먼트를 남기지 않는 것이 중요하다: 남겨두면 소비자가 열어놓고
        **멈춘 화면**을 본다. 등록·설정은 게이트웨이가 들고 있다.
        """
        logger.warning("RealSense %s: %s — 발행을 중단합니다", self.serial, why)
        self.lost_at = time.time()
        self._running = False
        # 노드가 진짜 사라졌는지 다시 본다 — "프레임이 안 온다" 는 장치가 아직
        # 있을 수도 있고, 그때는 정리를 해야 자원이 돌아온다.
        gone = not self._device_present()
        try:
            self._stop_pipeline(device_gone=gone)
        except Exception as exc:
            logger.warning("파이프라인 정지 실패 (%s): %s", self.serial, exc)
        with self._lock:
            self._latest.clear()

    def force_release(self) -> None:
        """모든 스트림 refcount를 0으로 만들고 파이프라인을 정지한다.
        외부 프로세스(LeRobot 녹화)가 같은 USB 디바이스를 점유하기 전에
        웹 미리보기 스트림을 강제로 비워 대역폭/디바이스 경합을 막는다."""
        with self.op_guard():
            for s in self._refcount:
                self._refcount[s] = 0
            self._stop_pipeline()
            with self._lock:
                self._latest.clear()

    # 첫 프레임 뒤 더 기다리는 시간. RealSense 는 파이프라인을 켠 직후 자동노출이
    # 안 잡혀 **까만 프레임**이 먼저 나온다 — 그걸 썸네일로 남기면 화면이 검다.
    # 읽기 루프가 그동안 계속 발행하므로, 마지막에 남는 것은 안정화된 프레임이다.
    SETTLE_S = 1.2

    # 스캔은 스트림마다 probe 를 부른다. 한 번 켤 때 장치의 모든 스트림을 함께
    # 켜므로, 이 시간 안의 다음 probe 는 **이미 확보한 프레임을 재사용**한다.
    # 없으면 파이프라인 기동·안정화가 스트림 수만큼 반복된다 (실측 6스트림 14초).
    PROBE_REUSE_S = 20.0

    def probe_stream(self, stream: str, timeout: float = 8.0) -> bool:
        """스캔용: 임시로 스트림을 켜서 프레임을 확보. 연결 유지 안 함.

        타임아웃이 넉넉한 이유: 카메라 2대가 USB 대역폭을 나눠 초기화하면
        첫 프레임까지 수 초가 걸린다 (옛 `warmup_s: 5` 가 있던 이유와 같다).
        """
        if stream not in self.available:
            return False
        # 방금 이 장치를 켜서 모든 스트림 프레임을 남겼다면 다시 켜지 않는다
        if time.time() - getattr(self, "_last_probe_at", 0.0) < self.PROBE_REUSE_S:
            from piper_shm import Subscriber, SegmentError, segment_for_camera

            try:
                sub = Subscriber(segment_for_camera(f"rs:{self.serial}:{stream}"))
            except SegmentError:
                pass
            else:
                try:
                    if sub.read() is not None:
                        return True
                finally:
                    sub.close()
        with self.op_guard():
            temporary = self._pipeline is None or stream not in self._active
            # **장치의 모든 스트림을 한 번에 켠다.** 스캔은 스트림마다 probe 를 부르는데,
            # 하나씩 켜면 파이프라인 재시작과 안정화 대기가 스트림 수만큼 곱해진다
            # (실측 6스트림 17초). 어차피 같은 USB 장치라 함께 켜는 게 싸다.
            #
            # ⚠ **단, 대역폭이 남을 때만이다.** 실측: D435/D405 가 USB 2.0(480Mbps)
            # 허브 하나를 나눠 쓰는 구성에서, color 가 이미 돌 때 depth+IR 을 더
            # 켜면 depth 가 프레임을 못 내고(`No frame`) **그 실패가 장치를 물려
            # color 가 15fps → 0.3fps 로 주저앉는다.** 파이프라인을 되돌려도 안 돌아온다.
            # 그래서 USB2 이거나 이미 쓰는 스트림이 있으면 요청한 것만 켠다.
            # ⚠ **USB 2.0 에서는 쓰는 중인 스트림에 다른 스트림을 더하지 않는다.**
            # 실측: D435 가 color 를 돌리는 중에 depth 를 켜면 depth 는 프레임을 못
            # 내고(`No frame`), **그 실패가 장치를 물려 color 가 15fps → 0.3fps 로
            # 주저앉는다.** 파이프라인을 되돌려도, 스트림을 하나만 더해도 마찬가지다.
            # 즉 USB2 대역폭에서 둘은 공존할 수 없다 — 시도 자체가 손해다.
            #
            # 썸네일 한 장 얻자고 돌아가는 카메라를 죽이지 않는다. 조용히 실패하지도
            # 않는다: 사유를 돌려줘 사용자가 USB3 로 옮길 판단을 할 수 있게 한다.
            held = {s for s, c in self._refcount.items() if c > 0}
            if held and stream not in held and self.usb_speed_mbps() < 5000:
                speed = self.usb_speed_mbps()
                logger.warning(
                    "%s/%s probe 생략: USB %s 에서 %s 와 공존할 수 없다 "
                    "(USB 3 포트로 옮기면 해결된다)",
                    self.serial, stream,
                    f"{speed} Mbps" if speed else "속도 미상",
                    sorted(held),
                )
                return False
            want = self._active | self.available if self._can_widen() else self._active | {stream}
            if not self._ensure_streams(want):
                return False
            deadline = time.time() + timeout
            got = False
            while time.time() < deadline:
                if self.get_frame(stream) is not None:
                    got = True
                    break
                time.sleep(0.1)
            # 자동노출이 잡힐 때까지 조금 더 흘려보낸다 (마지막 발행분이 썸네일이 된다)
            if got:
                time.sleep(self.SETTLE_S)
                self._last_probe_at = time.time()
            # ⚠ **probe 로 켠 여분 스트림을 반드시 내린다.**
            # 스캔은 장치의 모든 스트림을 한 번에 켜는데(그게 빠르다), 끝나고
            # 그대로 두면 아무도 안 쓰는 depth/IR 이 USB 대역폭을 계속 먹는다.
            # 실측: 그 상태에서 D435 color 가 15fps → **0.3fps** 로 떨어져
            # 추론이 `200ms 안에 새 프레임이 없습니다` 로 죽었다.
            if temporary:
                held = {s for s, c in self._refcount.items() if c > 0}
                if held:
                    # ⚠ `_ensure_streams(held)` 만으로는 **안 줄어든다.**
                    # 그 함수의 조기 반환이 `issubset` 이라 "이미 그 이상을 켜고
                    # 있으면 그대로"가 되기 때문이다. 늘릴 때만 재구성한다.
                    # 그래서 여기서는 명시적으로 접었다 편다 (세그먼트는 유지).
                    self._stop_pipeline(unlink_segments=False)
                    self._ensure_streams(held)
                else:
                    # 아무도 안 쥐면 통째로 정지.
                    # **세그먼트는 남긴다** — 스캔 썸네일이 그 한 장이다.
                    self._stop_pipeline(unlink_segments=False)
            return got


class RealSenseHub:
    def __init__(self) -> None:
        self._devices: dict[str, _RSDevice] = {}
        self._lock = threading.Lock()
        # 마지막 프로파일 적용 결과 — 연결 안에서 적용하므로 응답에 실을 수 없다.
        self._last_apply: dict[str, dict] = {}

    def _available_streams(self, device) -> set[str]:
        """디바이스 센서 프로파일에서 제공 가능한 스트림 종류 추출."""
        avail: set[str] = set()
        try:
            for sensor in device.query_sensors():
                for p in sensor.get_stream_profiles():
                    if not p.is_video_stream_profile():
                        continue
                    st = p.stream_type()
                    if st == rs.stream.color:
                        avail.add("color")
                    elif st == rs.stream.depth:
                        avail.add("depth")
                    elif st == rs.stream.infrared:
                        avail.add("infrared")
        except Exception as exc:
            logger.warning("query stream profiles failed: %s", exc)
        return avail

    def lost(self) -> list[dict]:
        """**데몬이 판정한** 사라진 스트림들. 게이트웨이가 추론하지 않게 하려는 것이다.

        디바이스 단위로 판정하고 스트림 단위로 펼친다 — 화면과 매핑이 스트림 단위라
        "어느 카메라"를 스트림 이름으로 말해야 알아본다.
        """
        out = []
        for serial, dev in self._devices.items():
            if not dev.lost_at:
                continue
            streams = sorted(dev._active) or sorted(dev.available)
            out += [{"id": make_id(serial, st), "at": dev.lost_at} for st in streams]
        return out

    def scan(self) -> list[dict]:
        """연결된 RealSense를 스트림별 엔트리로 반환."""
        if not _RS_AVAILABLE:
            return []
        entries: list[dict] = []
        try:
            ctx = rs.context()
            devices = ctx.query_devices()
        except Exception as exc:
            logger.warning("RealSense enumeration failed: %s", exc)
            return []

        seen_serials: set[str] = set()
        for device in devices:
            try:
                serial = device.get_info(rs.camera_info.serial_number)
                name = device.get_info(rs.camera_info.name)  # "Intel RealSense D435I"
                try:
                    usb_port = _short_usb_port(device.get_info(rs.camera_info.physical_port))
                except Exception:
                    usb_port = ""
            except Exception as exc:
                logger.warning("RealSense device info failed: %s", exc)
                continue
            seen_serials.add(serial)
            available = self._available_streams(device)
            model = name.replace("Intel RealSense ", "").strip() or name
            with self._lock:
                dev = self._devices.get(serial)
                if dev is None:
                    dev = _RSDevice(serial, model, usb_port, available)
                    self._devices[serial] = dev
                else:
                    dev.model = model
                    dev.usb_port = usb_port
                    dev.available = available
            for stream in STREAM_TYPES:
                if stream not in available:
                    continue
                entries.append({
                    "id": make_id(serial, stream),
                    "name": f"{model} {stream.capitalize()}",
                    "usb_port": usb_port,
                    # ⚠ **USB 2.0 이면 대역폭이 모자란다.** 두 대를 같이 쓰거나
                    # depth 를 켜면 프레임이 끊기고 장치가 물린다. 사용자가
                    # 화면에서 바로 알아야 하므로 스캔 결과에 싣는다.
                    "usb_speed_mbps": dev.usb_speed_mbps() if dev else 0,
                    "cam_type": "realsense",
                    "serial": serial,
                    "stream_type": stream,
                })
        return entries

    def _device(self, serial: str) -> _RSDevice | None:
        with self._lock:
            return self._devices.get(serial)

    def is_d405(self, serial: str) -> bool:
        """serial이 D405 모델인지 (마지막 scan 캐시 기준, 미확인이면 False).

        D405는 depth 스트림이 함께 켜져야 color 프레임이 나오므로, 추론/녹화
        카메라 설정에서 이 카메라만 use_depth를 켜기 위해 쓴다."""
        dev = self._device(serial)
        return dev is not None and dev.is_d405()

    def connect(self, cam_id: str, width: int = 0, height: int = 0,
                fps: int = 0, controls: dict | None = None) -> tuple[bool, str]:
        """스트림 시작. 셋 다 주면 그 프로파일로 **맞춰서** 연다.

        하나라도 0이면 요청 없음으로 보고 장치 기본값에 맡긴다 — 프리뷰처럼
        해상도를 따지지 않는 호출부가 그대로 쓰던 방식이다.

        `controls` 를 주면 스트림이 뜬 뒤 적용한다. RealSense 는 파이프라인
        재시작에서 옵션이 기본값으로 돌아가는 경우가 있어, **여기가 유일하게
        확실한 지점**이다. 적용 실패는 연결 실패로 만들지 않는다.
        """
        parsed = parse_id(cam_id)
        if not parsed:
            return False, f"Not a RealSense id: {cam_id}"
        serial, stream = parsed
        dev = self._device(serial)
        if dev is None:
            return False, f"RealSense {serial} not found (rescan)"
        want = (int(width), int(height), int(fps)) if width and height and fps else None
        if dev.connect_stream(stream, want):
            if controls:
                self.apply_controls(cam_id, controls)
            return True, "OK"
        return False, f"Failed to start RealSense {serial}/{stream}"

    def set_depth_encoding(self, cam_id: str, near_mm: int, far_mm: int) -> tuple[bool, str]:
        """깊이 인코딩 범위를 바꾼다. **다음 프레임부터 바로 적용된다.**

        ⚠ 녹화 중에 바꾸면 **한 데이터셋 안에서 픽셀값의 뜻이 달라진다.**
        여기서 막지는 않는다 — 녹화 중인지는 게이트웨이가 안다(배타 가드).
        """
        parsed = parse_id(cam_id)
        if not parsed:
            return False, f"Not a RealSense id: {cam_id}"
        serial, _ = parsed
        with self._lock:
            dev = self._devices.get(serial)
        if dev is None:
            return False, f"RealSense {serial} not found"
        try:
            enc = DepthEncoding(near_mm=int(near_mm), far_mm=int(far_mm))
            # 만들어보고 검증한다 — 잘못된 범위를 넣어두면 다음 프레임에서 터진다
            encode_depth(np.zeros((1, 1), dtype=np.uint16), enc)
        except (ValueError, TypeError) as exc:
            return False, str(exc)
        dev.depth_encoding = enc
        logger.info("깊이 인코딩 변경 %s: %s", serial, enc.to_dict())
        return True, "OK"

    # 자동이 수렴할 때까지 주는 시간. RealSense 의 자동 노출·WB 는 몇 프레임
    # 걸리고, 조명이 어두우면 더 걸린다.
    _CAL_SETTLE_S = 2.0
    # 노출 비례 보정을 몇 번까지 반복하나. 보통 1~2번이면 들어온다.
    _CAL_ROUNDS = 3

    def calibrate_gray_card(self, cam_id: str, roi=None, target: float | None = None
                            ) -> dict:
        """회색 카드로 화이트밸런스·노출을 맞춘다 (feature/gray-card-calibration.md).

        절차:

        1. **자동을 켜고 기다린다** — 회색 카드는 자동 WB 가 가장 잘 맞는 조건이다
           (중성 피사체가 화면을 채운 상태). 카메라 자기 알고리즘을 쓰는 편이
           켈빈 값을 이분 탐색하는 것보다 빠르고 정확하다.
        2. **자동을 끈다** — 값이 그 자리에 얼어붙는다. 이게 "캘리브레이션"의 실체다.
           켜둔 채로는 카드를 치우는 순간 다시 흔들려 재현이 안 된다.
        3. 카드를 재서 **노출만** 비례 보정한다. 밝기는 노출에 거의 비례한다.
        4. 다시 재서 결과를 돌려준다.

        값을 저장하지는 않는다 — 저장은 카메라 프로파일이 하는 일이고, 그쪽은
        이미 연결할 때 적용까지 한다. 여기서는 **장치에 올려놓고 보고**만 한다.
        """
        from piper_cam import graycard as gc

        parsed = parse_id(cam_id)
        if not parsed:
            return {"ok": False, "error": f"Not a RealSense id: {cam_id}"}
        serial, stream = parsed
        if stream != "color":
            return {"ok": False, "error": "회색 카드 보정은 컬러 스트림에만 합니다"}
        dev = self._device(serial)
        if dev is None or "color" not in dev._active:
            return {"ok": False, "error": "컬러 스트림이 연결돼 있지 않습니다"}

        target = gc.TARGET_LUMA if target is None else float(target)
        steps: list[dict] = []

        def _read():
            frame = dev.get_frame("color")
            if frame is None:
                return None
            return gc.measure(frame, tuple(roi) if roi else None)

        # 1) 자동으로 맞추게 두고 기다린다
        for name in ("enable_auto_white_balance", "enable_auto_exposure"):
            self.set_control(cam_id, name, 1)
        time.sleep(self._CAL_SETTLE_S)

        before = _read()
        if before is None:
            return {"ok": False, "error": "프레임을 받지 못했습니다"}
        usable, why = before.usable
        if not usable:
            # ⚠ 못 믿을 측정으로 값을 정하면 다음 주에 재현이 안 된다 —
            #   그게 이 기능의 존재 이유인데 스스로 깨는 셈이다.
            return {"ok": False, "error": why, "before": before.to_dict()}

        # 2) 자동을 끈다 — 자동이 찾은 값이 그대로 얼어붙는다
        for name in ("enable_auto_white_balance", "enable_auto_exposure"):
            self.set_control(cam_id, name, 0)
        time.sleep(0.4)

        # 3) 노출만 비례 보정한다. WB 는 자동이 카드에서 이미 맞췄다.
        lo, hi, cur = self._exposure_range(cam_id)
        reading = _read() or before
        for _ in range(self._CAL_ROUNDS):
            if abs(reading.luma - target) <= gc.LUMA_TOLERANCE:
                break
            new_us = gc.exposure_for(reading, cur, lo, hi, target)
            if abs(new_us - cur) < 1:
                break
            self.set_control(cam_id, "exposure", new_us)
            cur = new_us
            time.sleep(0.4)
            reading = _read() or reading
            steps.append({"exposure_us": round(cur), "luma": round(reading.luma, 1)})

        ok, verdict = reading.verdict(target)
        logger.info("회색 카드 보정 %s: %s (노출 %.0fus, 밝기 %.0f, 치우침 %.1f%%)",
                    serial, verdict, cur, reading.luma, reading.neutral_error_pct)
        return {"ok": ok, "verdict": verdict, "target": target,
                "before": before.to_dict(), "after": reading.to_dict(),
                "exposure_us": round(cur), "steps": steps,
                "roi": list(roi) if roi else list(gc.center_roi(
                    dev.get_frame("color").shape))}

    def measure_gray_card(self, cam_id: str, roi=None) -> dict:
        """카드 영역을 **재기만** 한다. 장치는 안 건드린다.

        상자를 어디에 둘지 고르려면 즉시 되먹임이 있어야 한다 — 그림자나 반사가
        걸린 자리는 눈으로 잘 안 보이고 숫자로만 드러난다(얼룩 %).
        보정을 눌러보고 거절당하는 것보다 옮기면서 보는 편이 낫다.
        """
        from piper_cam import graycard as gc

        parsed = parse_id(cam_id)
        if not parsed:
            return {"ok": False, "error": f"Not a RealSense id: {cam_id}"}
        serial, stream = parsed
        dev = self._device(serial)
        if dev is None:
            return {"ok": False, "error": f"RealSense {serial} not found"}
        frame = dev.get_frame(stream)
        if frame is None:
            return {"ok": False, "error": "프레임을 받지 못했습니다"}
        try:
            reading = gc.measure(frame, tuple(roi) if roi else None)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        ok, verdict = reading.verdict()
        return {"ok": ok, "verdict": verdict, "reading": reading.to_dict(),
                "frame": [int(frame.shape[1]), int(frame.shape[0])],
                "roi": list(roi) if roi else list(gc.center_roi(frame.shape))}

    def _exposure_range(self, cam_id: str) -> tuple[float, float, float]:
        """`(min, max, 현재)` 노출. 못 읽으면 넓게 잡되 현재값은 보수적으로."""
        for c in self.list_controls(cam_id):
            if c.get("name") == "exposure":
                return (float(c.get("min") or 1), float(c.get("max") or 165000),
                        float(c.get("value") or 1000))
        return 1.0, 165000.0, 1000.0

    def set_background_mask(self, cam_id: str, enabled: bool,
                            far_mm=None, keep_unknown=None) -> tuple[bool, str]:
        """깊이로 컬러의 배경을 지울지 켜고 끈다. **다음 프레임부터 적용된다.**

        경계는 `depth_encoding.far_mm` 이다 — 창을 따로 두지 않는다.

        ⚠ 녹화 중에 바꾸면 한 데이터셋 안에 배경이 있는 프레임과 없는 프레임이
        섞인다. 여기서 막지는 않는다 — 녹화 중인지는 게이트웨이가 안다(배타 가드).
        """
        parsed = parse_id(cam_id)
        if not parsed:
            return False, f"Not a RealSense id: {cam_id}"
        serial, _ = parsed
        with self._lock:
            dev = self._devices.get(serial)
        if dev is None:
            return False, f"RealSense {serial} not found"
        dev.mask_background = bool(enabled)
        if far_mm is not None:
            # 0 이나 None 은 "깊이 창을 따라간다"는 뜻이다
            dev.mask_far_mm = float(far_mm) if far_mm else None
        if keep_unknown is not None:
            dev.mask_keep_unknown = bool(keep_unknown)
        dev._mask_warned = False        # 다시 켜면 경고도 다시 할 기회를 준다
        logger.info("배경 마스킹 %s: %s (경계 %.0fmm%s, 무효 %s)", serial,
                    "켬" if enabled else "끔", dev.effective_mask_far_mm,
                    "" if dev.mask_far_mm else " — 깊이 창 따라감",
                    "남김" if dev.mask_keep_unknown else "지움")
        return True, "OK"

    def info(self, cam_id: str) -> dict:
        """지금 **실제로** 돌고 있는 프로파일. 요청값이 아니다.

        요청과 다를 수 있어서(장치에 없는 조합은 근사로 떨어진다) 데이터셋 메타에
        박을 fps 는 요청이 아니라 이걸 봐야 한다. 안 그러면 15fps 라고 적어놓고
        10fps 로 채운 데이터셋이 나온다.
        """
        parsed = parse_id(cam_id)
        if not parsed:
            return {}
        serial, stream = parsed
        with self._lock:
            dev = self._devices.get(serial)
        if dev is None:
            return {}
        got = dev._running_profile.get(stream)
        # ⚠ **세그먼트가 곧 lease 다.** 파이프라인이 돌아도 세그먼트가 없으면
        # 소비자는 못 연다 — 그 상태로 `connected: True` 를 돌려주면 게이트웨이가
        # 재연결을 건너뛰고, 추론이 시작 직후 `SegmentError` 로 죽는다.
        # (실제로 D435 가 이랬다: `connect` 0초에 OK, 그런데 세그먼트 없음.)
        alive = stream in dev._active and dev._segments_alive({stream})
        out: dict = {
            "id": cam_id, "model": dev.model, "connected": alive,
            # 이 스트림에 대해 **이미 반영한 요청**. 게이트웨이가 같은 요청을
            # 또 보내지 않도록(= 불필요한 재연결·refcount 증가) 판단 근거로 쓴다.
            "want": list(dev._want.get(stream) or ()),
        }
        if got:
            out.update(width=got[0], height=got[1], fps=got[2])
        if stream == "depth":
            # 데이터셋 메타에 실려야 하는 값 — 없으면 나중에 같은 픽셀값이
            # 무슨 거리였는지 알 방법이 없다
            out["depth_encoding"] = dev.depth_encoding.to_dict()
            # 어떤 스케일로 계산했는지 남긴다. near/far 는 이미 실제 mm 라
            # 해석에 필요하진 않지만, 틀렸을 때 **어디서 틀렸는지**가 보인다.
            out["depth_units_m"] = dev.depth_units_m
        # 컬러 쪽 사실이다 — 배경이 지워진 채로 녹화됐는지는 컬러 프레임의 성질이다
        if stream == "color":
            out["background_mask"] = {
                "enabled": dev.mask_background,
                "far_mm": dev.effective_mask_far_mm,
                # 따로 정한 값인가, 깊이 창을 따라가는 중인가
                "follows_depth": dev.mask_far_mm is None,
                "keep_unknown": dev.mask_keep_unknown,
            }
        return out

    def disconnect(self, cam_id: str) -> None:
        parsed = parse_id(cam_id)
        if not parsed:
            return
        serial, stream = parsed
        dev = self._device(serial)
        if dev is not None:
            dev.disconnect_stream(stream)

    def release_all(self) -> bool:
        """모든 디바이스의 활성 스트림을 강제 해제한다. 녹화/추론 등 외부
        프로세스가 RealSense를 직접 열기 전에 호출해 경합을 막는다.
        하나라도 해제했으면 True."""
        released = False
        with self._lock:
            devices = list(self._devices.values())
        for dev in devices:
            if dev._running:
                try:
                    dev.force_release()
                    released = True
                except Exception as exc:
                    logger.warning("RealSense %s force_release failed: %s", dev.serial, exc)
        return released

    def hardware_reset(self, cam_id: str) -> tuple[bool, str]:
        """librealsense hardware_reset — 카메라 펌웨어를 파워사이클해 강제 재열거.

        D405 등이 멈췄거나(프레임 정지) 상태가 꼬였을 때 재부팅/재연결 없이 복구한다.
        먼저 활성 파이프라인·스트림을 정지(force_release)해 디바이스를 놓은 뒤 reset
        명령을 보낸다. reset 후 카메라는 USB에서 수 초간 사라졌다 다시 나타나므로,
        호출자는 이후 재스캔(scan)으로 엔트리를 갱신해야 한다.

        주의: reset 명령도 UVC 경로로 디바이스에 접근하므로 이미 커널 D-state로 물린
        경우 멈출 수 있어 _run_guarded 로 시간 상한을 강제한다. 이 경우 USB 리바인딩
        (robot_manager.recover_usb_controllers)만이 복구 수단이다."""
        if not _RS_AVAILABLE:
            return False, "pyrealsense2 unavailable"
        parsed = parse_id(cam_id)
        if not parsed:
            return False, f"Not a RealSense id: {cam_id}"
        serial, _ = parsed
        dev = self._device(serial)
        if dev is None:
            return False, f"RealSense {serial} not found (rescan)"
        ok = _run_guarded(
            lambda: self._hardware_reset_impl(dev), 8.0, False, f"hardware_reset({cam_id})"
        )
        if ok:
            return True, "OK"
        return False, f"RealSense {serial} 하드웨어 리셋 실패 또는 응답 없음(디바이스 멈춤 의심)"

    def _hardware_reset_impl(self, dev: "_RSDevice") -> bool:
        # 파이프라인 정지 + refcount/프레임 비움 (op_guard 내부에서 획득)
        dev.force_release()
        with dev.op_guard(timeout=5.0):
            device = self._find_device(dev.serial)
            if device is None:
                return False
            device.hardware_reset()
            return True

    def probe(self, cam_id: str) -> tuple[bool, str]:
        parsed = parse_id(cam_id)
        if not parsed:
            return False, f"Not a RealSense id: {cam_id}"
        serial, stream = parsed
        dev = self._device(serial)
        if dev is None:
            return False, f"RealSense {serial} not found (rescan)"
        # 컨트롤 질의(초기화)와 동시에 호출되면 probe_stream 이 op_lock 을 기다리거나
        # 디바이스 충돌로 멈출 수 있으므로 시간 상한을 강제한다.
        ok = _run_guarded(lambda: dev.probe_stream(stream), 20.0, False, f"probe({cam_id})")
        if ok:
            return True, "OK"
        return False, f"No frame from RealSense {serial}/{stream}"

    # ── 컨트롤 (librealsense sensor option) ──

    def _find_device(self, serial: str):
        try:
            for d in rs.context().query_devices():
                if d.get_info(rs.camera_info.serial_number) == serial:
                    return d
        except Exception as exc:
            logger.warning("RealSense device lookup failed: %s", exc)
        return None

    def _sensor_for_stream(self, device, stream: str):
        """스트림 종류에 해당하는 센서 반환 (color→RGB 센서, depth/IR→스테레오 센서)."""
        target = {
            "color": rs.stream.color,
            "depth": rs.stream.depth,
            "infrared": rs.stream.infrared,
        }.get(stream)
        if target is None:
            return None
        try:
            for s in device.query_sensors():
                for p in s.get_stream_profiles():
                    if p.is_video_stream_profile() and p.stream_type() == target:
                        return s
        except Exception as exc:
            logger.warning("RealSense sensor lookup failed: %s", exc)
        return None

    def list_controls(self, cam_id: str) -> list[dict]:
        """해당 스트림 센서가 지원하는 option을 v4l2 컨트롤과 동일한 dict 형태로 반환."""
        if not _RS_AVAILABLE:
            return []
        parsed = parse_id(cam_id)
        if not parsed:
            return []
        return _run_guarded(
            lambda: self._list_controls_impl(*parsed), 3.0, [], f"list_controls({cam_id})"
        )

    def _list_controls_impl(self, serial: str, stream: str) -> list[dict]:
        dev = self._device(serial)
        guard = dev.op_guard(timeout=2.5) if dev else contextlib.nullcontext()
        with guard:
            return self._list_controls_locked(serial, stream)

    def _list_controls_locked(self, serial: str, stream: str) -> list[dict]:
        device = self._find_device(serial)
        if device is None:
            return []
        sensor = self._sensor_for_stream(device, stream)
        if sensor is None:
            return []

        controls: list[dict] = []
        try:
            options = sensor.get_supported_options()
        except Exception as exc:
            logger.warning("get_supported_options failed: %s", exc)
            return []

        for opt in options:
            try:
                rng = sensor.get_option_range(opt)
                if rng.min == rng.max:  # 조절 불가 항목 제외
                    continue
                readonly = sensor.is_option_read_only(opt)
                value = sensor.get_option(opt)
                label = sensor.get_option_description(opt) or str(opt)
            except Exception:
                continue
            is_bool = rng.min == 0 and rng.max == 1 and rng.step == 1
            controls.append({
                "cid": int(opt),
                "name": str(opt).lower().replace("-", "_").replace(" ", "_"),
                "label": str(opt).replace("_", " ").title(),
                "type": 2 if is_bool else 1,
                "min": rng.min,
                "max": rng.max,
                "step": rng.step,
                "default": rng.default,
                "value": value,
                "inactive": False,
                "readonly": bool(readonly),
                "description": label,
            })
        return controls

    def set_control(self, cam_id: str, name: str, value: float) -> bool:
        if not _RS_AVAILABLE:
            return False
        parsed = parse_id(cam_id)
        if not parsed:
            return False
        return _run_guarded(
            lambda: self._set_control_impl(*parsed, name, value),
            3.0, False, f"set_control({cam_id},{name})",
        )

    def _set_control_impl(self, serial: str, stream: str, name: str, value: float) -> bool:
        dev = self._device(serial)
        guard = dev.op_guard(timeout=2.5) if dev else contextlib.nullcontext()
        with guard:
            return self._set_control_locked(serial, stream, name, value)

    def _set_control_locked(self, serial: str, stream: str, name: str, value: float) -> bool:
        device = self._find_device(serial)
        if device is None:
            return False
        sensor = self._sensor_for_stream(device, stream)
        if sensor is None:
            return False
        try:
            for opt in sensor.get_supported_options():
                opt_name = str(opt).lower().replace("-", "_").replace(" ", "_")
                if opt_name == name:
                    if sensor.is_option_read_only(opt):
                        return False
                    rng = sensor.get_option_range(opt)
                    v = max(rng.min, min(rng.max, float(value)))
                    sensor.set_option(opt, v)
                    return True
        except Exception as exc:
            logger.warning("RealSense set_option %s=%s failed: %s", name, value, exc)
        return False

    def apply_controls(self, cam_id: str, wanted: dict,
                       budget_s: float = 2.0) -> dict:
        """프로파일 컨트롤을 순서대로 적용하고 read-back 으로 검증한다.

        ⚠ **op_guard 를 한 번만 잡는다.** 컨트롤마다 `set_control` 을 부르면
        락을 N 번 잡고 놓는데, `op_lock` 은 재진입이 아니고 획득마다 2.5s 상한이
        붙어 있어 항목이 늘수록 최악 지연이 곱해진다. 여기서 한 번 잡고
        `*_locked` 를 직접 쓴다 — v4l2 쪽과 계획 로직은 그대로 공유한다.
        """
        parsed = parse_id(cam_id)
        if not parsed:
            return {}
        serial, stream = parsed

        def _do() -> dict:
            dev = self._device(serial)
            guard = dev.op_guard(timeout=3.0) if dev else contextlib.nullcontext()
            with guard:
                return controls_mod.apply_controls(
                    lambda: self._list_controls_locked(serial, stream),
                    lambda n, v: self._set_control_locked(serial, stream, n, v),
                    wanted, budget_s=budget_s, label=cam_id,
                )

        # 예산 + 락 획득 + 여유. D405 가 물리면 이 스레드는 고아가 되지만 데몬은 산다.
        report = _run_guarded(_do, budget_s + 6.0, {}, f"apply_controls({cam_id})")
        self._last_apply[cam_id] = report
        return report

    def last_apply_report(self, cam_id: str) -> dict:
        return self._last_apply.get(cam_id, {})

    def has_frame(self, cam_id: str) -> bool:
        parsed = parse_id(cam_id)
        if not parsed:
            return False
        serial, stream = parsed
        dev = self._device(serial)
        return dev is not None and dev.get_frame(stream) is not None

    def get_jpeg(self, cam_id: str) -> bytes | None:
        parsed = parse_id(cam_id)
        if not parsed:
            return None
        serial, stream = parsed
        dev = self._device(serial)
        if dev is None:
            return None
        frame = dev.get_frame(stream)
        if frame is None:
            return None
        try:
            import cv2
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return buf.tobytes() if ok else None
        except Exception:
            return None


realsense_hub = RealSenseHub()
