"""
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
        self._lock = threading.Lock()       # _latest 보호
        # 디바이스 작업 직렬화: 한 디바이스에 동시에 두 파이프라인을 start 할 수
        # 없고, librealsense 는 디바이스 단위로 thread-safe 하지 않다. 따라서
        # connect/disconnect/probe(파이프라인) 와 list/set_control(센서 옵션) 를
        # 모두 이 락으로 직렬화한다 — 그렇지 않으면 컨트롤 질의와 스트림 시작이
        # 같은 USB 디바이스에서 충돌해 uvcvideo 가 D-state 로 멈춘다.
        self._op_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._running = False

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

    def is_d405(self) -> bool:
        return "405" in (self.model or "")

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
            try:
                if s == "color":
                    cfg.enable_stream(rs.stream.color)
                elif s == "depth":
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
        if self._pipeline is not None and streams.issubset(self._active):
            return True
        # 재구성 필요 → 기존 정지 후 재시작
        self._stop_pipeline()
        pipeline = rs.pipeline()
        cfg = self._build_config(streams)
        try:
            pipeline.start(cfg)
        except Exception as exc:
            logger.error("RealSense %s pipeline start failed (%s): %s", self.serial, streams, exc)
            return False
        self._pipeline = pipeline
        self._active = set(streams)
        self._start_thread()
        return True

    def _stop_pipeline(self) -> None:
        self._running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        self._thread = None
        if self._pipeline is not None:
            try:
                self._pipeline.stop()
            except Exception:
                pass
            self._pipeline = None
        self._active = set()

    # ── 프레임 캡처 ──

    def _start_thread(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        import numpy as np
        import cv2
        while self._running and self._pipeline is not None:
            try:
                ok, frames = self._pipeline.try_wait_for_frames(timeout_ms=1000)
                if not ok or frames is None:
                    continue
                updates: dict[str, object] = {}
                if "color" in self._active:
                    cf = frames.get_color_frame()
                    if cf:
                        img = np.asanyarray(cf.get_data())  # rgb8
                        updates["color"] = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                if "depth" in self._active:
                    df = frames.get_depth_frame()
                    if df:
                        depth = np.asanyarray(df.get_data())  # uint16 (mm)
                        vis = cv2.convertScaleAbs(depth, alpha=0.03)
                        updates["depth"] = cv2.applyColorMap(vis, cv2.COLORMAP_JET)
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
                    from piper_shm import segment_for_camera

                    from app.services.shm_publisher import shm_publisher

                    for stream, frame in updates.items():
                        shm_publisher.publish(
                            segment_for_camera(f"rs:{self.serial}:{stream}"), frame
                        )
            except Exception as exc:
                logger.debug("RealSense %s read error: %s", self.serial, exc)
                time.sleep(0.1)

    def get_frame(self, stream: str):
        with self._lock:
            return self._latest.get(stream)

    # ── 연결 수명주기 (스트림 단위 refcount) ──

    def connect_stream(self, stream: str) -> bool:
        if stream not in self.available:
            return False
        with self._op_lock:
            self._refcount[stream] += 1
            ok = self._ensure_streams({s for s, c in self._refcount.items() if c > 0})
            if not ok:
                self._refcount[stream] = max(0, self._refcount[stream] - 1)
            return ok

    def disconnect_stream(self, stream: str) -> None:
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

    def probe_stream(self, stream: str, timeout: float = 4.0) -> bool:
        """스캔용: 임시로 스트림을 켜서 프레임 1장을 확보. 연결 유지 안 함."""
        if stream not in self.available:
            return False
        with self.op_guard():
            temporary = self._pipeline is None or stream not in self._active
            if not self._ensure_streams(self._active | {stream}):
                return False
            deadline = time.time() + timeout
            got = False
            while time.time() < deadline:
                if self.get_frame(stream) is not None:
                    got = True
                    break
                time.sleep(0.1)
            # refcount 가 0이고 임시로 켠 것이면 정지
            if temporary and not any(c > 0 for c in self._refcount.values()):
                self._stop_pipeline()
            return got


class RealSenseHub:
    def __init__(self) -> None:
        self._devices: dict[str, _RSDevice] = {}
        self._lock = threading.Lock()

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

    def connect(self, cam_id: str) -> tuple[bool, str]:
        parsed = parse_id(cam_id)
        if not parsed:
            return False, f"Not a RealSense id: {cam_id}"
        serial, stream = parsed
        dev = self._device(serial)
        if dev is None:
            return False, f"RealSense {serial} not found (rescan)"
        if dev.connect_stream(stream):
            return True, "OK"
        return False, f"Failed to start RealSense {serial}/{stream}"

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
        ok = _run_guarded(lambda: dev.probe_stream(stream), 8.0, False, f"probe({cam_id})")
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
