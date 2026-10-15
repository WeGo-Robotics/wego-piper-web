"""
카메라 관리 서비스.
시스템 카메라 스캔, 연결 테스트, 설정, 등록, 프리뷰.
"""

import logging
from dataclasses import dataclass, field

from app.core.config import settings
from app.services import camera_streams
from app.services.realsense_manager import realsense_hub
from app.services.v4l2_client import v4l2_hub

logger = logging.getLogger(__name__)


# USB 3.0 = 5000Mbps. 그 미만이면 RealSense 두 대나 color+depth 를 감당 못 한다.
USB3_MBPS = 5000


def _usb_warning(speed_mbps: int) -> str | None:
    """USB 대역폭 경고. 없으면 `None`.

    ⚠ **여기 한 곳에서만 판정한다.** 화면이 임계값을 따로 적으면 한쪽만 고쳐져
    어긋난다. 속도를 못 읽었으면(0) 경고하지 않는다 — 모르는 것과 느린 것은 다르다.
    """
    if not speed_mbps or speed_mbps >= USB3_MBPS:
        return None
    return (
        f"USB {speed_mbps}Mbps (2.0) 로 연결돼 있습니다. 대역폭이 모자라 "
        "카메라를 여러 대 쓰거나 depth 를 켜면 프레임이 끊깁니다 — "
        "USB 3 포트로 옮기세요."
    )


# 이만큼 새 프레임이 없으면 스트림이 죽은 것으로 본다.
# `device_watch.STALE_S` 와 같은 뜻이라 거기서 가져온다 — 두 곳에 적으면 갈린다.
def _is_fresh(cam_id: str) -> bool:
    """이 카메라의 세그먼트가 **지금도** 갱신되고 있는가."""
    try:
        import time

        from app.services.device_watch import STALE_S
        from piper_shm import Subscriber, segment_for_camera

        sub = Subscriber(segment_for_camera(cam_id), observer=True)
        try:
            wall = sub.wall_ns()
        finally:
            sub.close()
        return bool(wall) and (time.time_ns() - wall) / 1e9 <= STALE_S
    except Exception:
        return False


def _idle_record(cam_id: str) -> dict | None:
    """데몬이 "읽는 곳이 없어서" 닫았다고 남긴 기록. 없으면 `None`."""
    try:
        import json

        from piper_bus import contract as C
        from app.services.v4l2_client import _bus

        raw = _bus().r.get(C.cam_idle_key(cam_id))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _clear_idle_record(cam_id: str) -> None:
    try:
        from piper_bus import contract as C
        from app.services.v4l2_client import _bus

        _bus().r.delete(C.cam_idle_key(cam_id))
    except Exception:
        pass


def _header_shape(cam) -> tuple[int, int] | None:
    """세그먼트 헤더의 `(높이, 너비)`. 읽을 수 없으면 `None`.

    RPC 가 아니라 헤더 한 번 읽기다 — 데몬이 **실제로 발행하는** 크기이고, 요청값과
    다를 수 있다(D405 는 848x480 이 상한이라 640x480 을 요청해도 그렇다).
    """
    try:
        from app.services.camera_streams import segment_of
        from piper_shm import Subscriber

        sub = Subscriber(segment_of(cam), observer=True)
        try:
            h, w, _ = sub.shape
        finally:
            sub.close()
        return h, w
    except Exception:
        return None


def _active_controls(cam) -> dict:
    """활성 프로파일에서 이 카메라의 컨트롤 값. 없으면 빈 dict.

    지연 import 다 — `camera_profiles` 는 카메라 목록이 필요할 때 인자로 받으므로
    이쪽을 import 하지 않지만, 순환의 씨앗을 아예 두지 않는다.
    실패해도 연결을 막지 않는다: 프로파일 때문에 카메라가 안 열리면 본말전도다.
    """
    try:
        from app.services import camera_profiles

        return camera_profiles.controls_for(cam)
    except Exception as exc:
        logger.warning("프로파일 조회 실패 (%s): %s", getattr(cam, "id", "?"), exc)
        return {}



@dataclass
class CameraInfo:
    """카메라 **기록**. 장치 I/O 는 데몬이 한다.

    예전에는 이 클래스가 `cv2.VideoCapture` 를 직접 열고 캡처 스레드를 돌렸다.
    이제 장치는 camerad(v4l2) / rsd(RealSense) 가 소유하고, 여기는 게이트웨이 상태만
    들고 있다 — 등록(`ready`)·스트림 이름(`stream`)·요청 해상도.

    두 허브가 **같은 메서드 이름**을 쓰므로 분기는 `_hub` 한 줄이다.
    """

    id: str           # "/dev/video0" / "rs:<serial>:<stream>"
    name: str         # 하드웨어가 말하는 이름 — "D435 Color", "HD Webcam" 등
    # **스트림 이름** — 사용자가 붙이는 이름이자 소비자(수집·추론)가 이 카메라를 부르는
    # 이름이다. 등록할 때 정해진다(기본 `장치명_N`). 미등록이면 빈 값.
    # 규칙(영문·숫자·`_`·`-`, 유일)은 `camera_streams` 한 곳이다.
    #
    # 옛 별칭(`label`)을 흡수했다. ⚠ **LeRobot 카메라 키(`top`)와는 여전히 다른 값이다.**
    # 데이터셋 피처는 `observation.images.<키>` 로 굳고 정책도 그 키로 학습되므로, 이
    # 이름을 바꿔도 키는 그대로다 — 바뀌는 것은 `{키: 이름}` 매핑의 오른쪽뿐이다.
    stream: str = ""
    # 개명 이력 — 옛 이름은 같은 카메라로 해석된다(저장된 매핑이 개명으로 안 깨지게).
    aliases: list[str] = field(default_factory=list)
    usb_port: str = ""
    # USB 링크 속도(Mbps). 0 이면 못 읽은 것 — **빠르다고 가정하지 않는다.**
    # 480 이면 USB 2.0 이고, 카메라 두 대나 depth 를 같이 쓰기엔 대역폭이 모자란다.
    usb_speed_mbps: int = 0
    cam_type: str = "opencv"  # opencv | realsense
    serial: str = ""
    stream_type: str = ""
    connected: bool = False
    ready: bool = False       # 등록 완료 → 사용 가능 리스트에 올라감
    # 조명 급변·표류 **경보**를 이 카메라에 대해 낼 것인가 (feature/lighting-watch.md).
    # ⚠ 끄는 것은 **경보뿐**이다 — 측정·발행은 계속한다. 수집·추론 화면의 실시간 표시와
    #   에피소드 뷰어가 같은 값을 읽는다. 판정도 계속 돌려서 다시 켜면 지금 이상한 것을
    #   그 자리에서 말한다. 손목 카메라는 팔과 같이 움직여 조명이 늘 바뀐다 — 거기서 울리는 경보는
    #   맞는 말이지만 쓸모가 없고, 쓸모없는 경보는 **옆의 진짜 경보까지 무시하게 만든다**
    #   (사용자 보고 2026-09-18: "손목 카메라에서 너무 자주 뜸").
    light_alarm: bool = True
    # **캡처 모드** `[w, h]` — 장치를 이 크기로 열고, 요청 해상도로 줄여 발행한다.
    # 화각 때문이다: 많은 UVC 카메라가 저해상도를 **센서 가운데를 잘라** 만든다
    # (Global Shutter AR0234 실측 2026-10-06: 640×480 이 가운데 1/3). 데이터셋 크기는
    # 맞추되 화각은 넓게 쓰려면 넓은 모드로 받아 줄여야 한다. 어느 모드가 전체 화각인지는
    # 장치가 알려주지 않으므로 사람이 정한다. None 이면 요청 크기 그대로 연다.
    capture: list[int] | None = None
    # **출력 해상도** `[w, h]` — 이 크기로 줄여 발행한다. None 이면 **원본 해상도**(캡처 그대로).
    # 데이터셋 크기를 맞추는 손잡이다. shm 녹화는 해상도를 세그먼트에서 읽으므로
    # 여기서 정한 크기가 그대로 데이터셋에 박힌다.
    output: list[int] | None = None
    # **유휴 자동 종료** — 읽는 곳(수집·추론·미리보기)이 이만큼(초) 없으면 데몬이 이 카메라를
    # 닫는다. 0 = 끔(기본). 닫아도 등록은 남고, 선택기에서 [연결] 로 다시 켠다.
    # 하한은 30초다(데몬이 맞춘다) — 설정 실수로 연결 직후에 닫히지 않게.
    # feature/stream-based-cameras.md §5-5.
    idle_stop_s: int = 0
    # 이 값을 데몬에 알렸는가(0 이 아닌 값으로). 알린 적이 있으면 0 으로 바꿔도 **알려야**
    # 데몬의 타이머가 풀린다. 저장하지 않는다 — 데몬이 재시작하면 타이머도 사라진다.
    idle_pushed: bool = field(default=False, repr=False)
    # 마지막 스캔에서 데몬이 이 장치를 **봤는가**. `connected` 와 다른 사실이다:
    # `present && !connected` = 꽂혀 있는데 안 열었다 (정상)
    # `!present`              = 아예 없다 (뽑혔다)
    # 예전엔 둘 다 `connected: false` 라 화면이 구분할 수 없었고, 뽑아둔 카메라가
    # 스캔 목록에 그대로 남아 "왜 계속 나오지" 가 됐다.
    present: bool = True
    # 설정값 (요청 해상도 — 실제 값은 데몬이 정한다)
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    color_mode: str = "rgb"
    rotation: int = 0
    fourcc: str | None = None

    @property
    def _hub(self):
        """장치를 소유한 데몬의 클라이언트. **이 한 줄이 유일한 분기다.**"""
        # 시뮬(simd)은 세 번째 데몬이다 — 분기는 여기 **한 곳**뿐이다
        if self.cam_type == "sim":
            from app.services.sim_camera_client import sim_camera_hub
            return sim_camera_hub
        return realsense_hub if self.cam_type == "realsense" else v4l2_hub

    @property
    def label(self) -> str:
        """**옛 이름.** 스트림 이름과 합쳐졌다 — 읽기 전용 별칭으로만 남긴다.

        `cam.label or cam.name` 으로 표시명을 만들던 호출부가 그대로 돌게 하려는 것이다.
        쓰기는 막았다(setter 없음): 대입이 조용히 되면 이름 규칙·유일성 검사를 우회한다.
        """
        return self.stream

    @property
    def profile_key(self) -> str:
        """재열거·리셋에도 안 바뀌는 식별자. 설정을 이 키에 매단다.

        `/dev/videoN` 은 **키가 아니다.** USB 컨트롤러가 죽어 리바인딩하면
        (이 저장소에서 실제로 겪었다) 번호가 바뀌고, id 로 매칭하던 설정은
        "스캔에 없다"며 조용히 버려진다.

        - RealSense: `rs:<시리얼>:<스트림>` 인 `id` 가 이미 안정적이다 —
          별도 키를 만들면 같은 사실을 두 벌 갖게 된다
        - v4l2: 물리 포트(`usb:4-3:1.0`). 같은 모델 두 대도 구분된다
        - 포트를 못 읽으면 이름 폴백 — 후보가 하나일 때만 쓴다(아래 `match_saved`)

        ⚠ `cam_type` 으로 갈라 묻지 않는다. 장치 종류 분기는 `_hub` 한 곳뿐이라는
        규칙이 있고(테스트가 강제한다), 여기서 물어야 하는 건 종류가 아니라
        **"이 id 가 재열거로 바뀌는 종류인가"** 다. 바뀌는 건 `/dev/videoN` 뿐이다.
        """
        if not self.id.startswith("/dev/"):
            return self.id
        if self.usb_port:
            return f"usb:{self.usb_port}"
        return f"name:{self.name}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            # 스트림 이름 — 사용자가 붙이고 소비자가 이 카메라를 부르는 이름.
            "stream": self.stream,
            "aliases": list(self.aliases),
            # ⚠ 옛 필드. 별칭이 스트림 이름으로 합쳐졌으므로 같은 값이다 — 낡은 번들을 든
            # 브라우저가 `label` 을 읽어도 이름이 보이게 남긴다. 새 코드는 `stream`.
            "label": self.stream,
            # 화면에 한 줄로 쓰기 좋은 표시명 — 각 화면이 `stream || name` 을
            # 따로 적으면 규칙이 갈린다. 등록 전에는 하드웨어 이름이다.
            "display_name": self.stream or self.name,
            "usb_port": self.usb_port,
            # 설정이 매달리는 안정 키. 화면이 "이 카메라가 프로파일에 있나"를
            # 판단하려면 id 가 아니라 이걸 봐야 한다.
            "profile_key": self.profile_key,
            "usb_speed_mbps": self.usb_speed_mbps,
            # 화면이 임계값을 따로 적지 않게 판정까지 여기서 한다 —
            # 두 곳에 적으면 한쪽만 고쳐져 어긋난다.
            "usb_warning": _usb_warning(self.usb_speed_mbps),
            "cam_type": self.cam_type,
            "serial": self.serial,
            "stream_type": self.stream_type,
            "connected": self.connected,
            "present": self.present,
            "ready": self.ready,
            "light_alarm": self.light_alarm,
            "idle_stop_s": self.idle_stop_s,
            "capture": self.capture,
            "output": self.output,
            "has_preview": self._hub.has_frame(self.id),
            # ⚠ **프레임이 지금도 오고 있는가.** `has_preview` 와 다른 사실이다:
            # 세그먼트에 마지막 프레임이 남아 있으면 그건 True 지만 스트림은
            # 죽었을 수 있다. 그래서 뽑힌 카메라가 화면에서 **정상처럼 보였다.**
            # 원인(케이블·데몬·멈춤)과 무관하게 "지금 영상이 오나"만 말한다.
            "streaming": self.connected and _is_fresh(self.id),
            # 깊이 인코딩은 **데이터셋 해석의 근거**다 — 화면에서 현재 값을 보고
            # 고칠 수 있어야 한다.
            # ⚠ depth 일 때만 묻는다. `to_dict` 는 스캔에서 카메라마다 불리므로
            # 무조건 물으면 RPC 가 카메라 수만큼 늘어 스캔이 그만큼 느려진다.
            "depth_encoding": (self.running_profile().get("depth_encoding")
                               if self.stream_type == "depth" else None),
            # raw 한 단위가 몇 미터인가. **화면에 보여준다** — 이게 안 보여서
            # D405(0.0001)가 D435(0.001)처럼 계산되는 걸 오래 못 알아챘다.
            "depth_units_m": (self.running_profile().get("depth_units_m")
                              if self.stream_type == "depth" else None),
            # 배경이 지워진 채로 나오는지는 **컬러 프레임의 성질**이다.
            # ⚠ depth 일 때 묻지 않는다 — `to_dict` 는 스캔에서 카메라마다 불린다.
            "background_mask": (self.running_profile().get("background_mask")
                                if self.stream_type == "color" else None),
            "config": {
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "color_mode": self.color_mode,
                "rotation": self.rotation,
                "fourcc": self.fourcc,
            },
        }

    def update_config(self, cfg: dict) -> None:
        for key in ("width", "height", "fps", "color_mode", "rotation", "fourcc"):
            if key in cfg:
                setattr(self, key, cfg[key])

    def probe(self, timeout: float = 3.0) -> tuple[bool, str]:
        return self._hub.probe(self.id)

    def connect(self, width: int = 0, height: int = 0, fps: int = 0) -> tuple[bool, str]:
        """셋 다 주면 그 프로파일로 연다. 하나라도 0이면 장치 기본값에 맡긴다.

        장치가 못 내는 조합이면 데몬이 가장 가까운 것으로 낮춰서 연다 —
        `running_profile()` 로 실제 값을 확인할 수 있다.

        ⚠ **활성 프로파일의 컨트롤을 여기서 함께 넘긴다.** 장치가 열리는 순간은
        이 한 곳뿐이라(데몬이 유일한 소유자다) 트리거를 여기저기 배선할 필요가 없다.
        노출·화이트밸런스가 초기화되는 경로가 여럿이었던 건 여는 주체가 여럿이라서다.
        """
        # ⚠ **이름을 먼저 전한다.** 데몬은 첫 프레임을 연결 직후에 발행하므로, 이름이 그보다
        # 늦으면 첫 세그먼트가 규칙 이름으로 만들어졌다가 옮겨진다 — 그 사이 소비자가 옛 이름을 본다.
        self.push_stream()
        self.push_idle()
        ok, msg = self._hub.connect(self.id, width, height, fps,
                                    _active_controls(self), capture=self.capture,
                                    output=self.output)
        self.connected = ok
        if ok:
            _clear_idle_record(self.id)      # 사람이 다시 연결했다 — "유휴로 종료됨" 은 지난 일이다
        return ok, msg

    def push_idle(self) -> int | None:
        """유휴 종료 시간을 데몬에 알린다. 돌려주는 값이 **데몬이 받아들인 값**이다.

        0 이고 알린 적도 없으면 보내지 않는다 — 옛 데몬이 매 연결마다 "알 수 없는 메서드"
        경고를 남기는 것을 피한다. 알린 적이 있으면 0 도 보낸다(데몬의 타이머를 풀어야 한다).
        `None` 은 못 알렸다(데몬이 없거나 옛 코드) — 그 카메라는 유휴 종료가 안 된다.
        """
        if not self.idle_stop_s and not self.idle_pushed:
            return 0
        fn = getattr(self._hub, "set_idle", None)
        if fn is None:
            return None
        try:
            got = fn(self.id, int(self.idle_stop_s))
        except Exception as exc:
            logger.warning("유휴 종료 시간 지정 실패 (%s): %s", self.id, exc)
            return None
        if got is not None:
            self.idle_pushed = bool(got)
        return got

    def hold(self, seconds: int) -> bool:
        """시작 요청이 소비자가 붙을 때까지 이 카메라를 유휴로 닫지 않게 한다. 꺼져 있으면 할 일 없다."""
        if not self.idle_stop_s:
            return False
        fn = getattr(self._hub, "hold", None)
        if fn is None:
            return False
        try:
            return bool(fn(self.id, int(seconds)))
        except Exception as exc:
            logger.warning("유휴 종료 유예 실패 (%s): %s", self.id, exc)
            return False

    def push_stream(self) -> bool:
        """등록된 스트림 이름을 데몬에 정해 주고, **확인된 이름**을 이 프로세스에도 든다.

        ⚠ 확인이 와야 든다. 옛 데몬(이 동사를 모른다)은 여전히 규칙 이름으로 발행하는데,
        게이트웨이가 먼저 새 이름을 들면 **없는 세그먼트를 찾는다.** 못 정했으면 규칙
        이름 그대로 두고 `False` — 연결은 계속 된다.
        """
        if not self.stream:
            return False
        fn = getattr(self._hub, "set_stream", None)
        if fn is None:
            return False
        try:
            acked = fn(self.id, self.stream)
        except Exception as exc:        # 이름 지정 때문에 연결이 막히면 본말전도다
            logger.warning("스트림 이름 지정 실패 (%s → %s): %s", self.id, self.stream, exc)
            return False
        if not acked:
            return False
        try:
            from piper_shm import set_segment_name

            set_segment_name(self.id, acked)
        except ValueError as exc:
            logger.warning("스트림 이름 충돌 (%s): %s", self.id, exc)
            return False
        return True

    def release_stream(self) -> None:
        """이름 지정을 푼다 — 등록 해제. 규칙 이름으로 돌아간다.

        연결된 채라면 데몬에게도 규칙 이름을 정해 준다. 안 그러면 데몬은 옛 이름으로 발행하는데
        이쪽은 규칙 이름을 찾는다.
        """
        from piper_shm import clear_segment_name, rule_segment_name

        fn = getattr(self._hub, "set_stream", None)
        if self.connected and fn is not None:
            try:
                fn(self.id, rule_segment_name(self.id))
            except Exception as exc:
                logger.warning("스트림 이름 해제 실패 (%s): %s", self.id, exc)
        clear_segment_name(self.id)

    def running_profile(self) -> dict:
        """지금 **실제로** 돌고 있는 `width/height/fps`. 요청값이 아니다."""
        return self._hub.info(self.id) or {}

    def disconnect(self) -> None:
        self._hub.disconnect(self.id)
        self.connected = False

    def get_controls(self) -> list[dict]:
        return self._hub.list_controls(self.id)

    def set_control(self, name: str, value: int) -> bool:
        return self._hub.set_control(self.id, name, value)

    def mark_absent(self) -> None:
        """데몬이 이 카메라를 모른다 — **장치 사실만** 지운다.

        등록(`ready`)·별칭·요청 해상도는 사람이 정한 것이라 남긴다. 하지만
        `connected` 는 장치 사실이고, USB 가 빠진 동안 살아남으면 **화면은
        "연결됨"인데 세그먼트가 없어 녹화·추론이 죽는다.** 팔에서 똑같은 구멍을
        먼저 겪었다 (robot_manager.ArmInfo.mark_absent).
        """
        self.connected = False
        self.present = False

    def apply_controls(self, wanted: dict) -> dict:
        """프로파일 컨트롤 적용. **순서와 검증은 데몬이 한다** — 자동 모드
        종속성 때문에 dict 순회로는 조용히 실패한다."""
        return self._hub.apply_controls(self.id, wanted)

    def last_apply_report(self) -> dict:
        return self._hub.last_apply_report(self.id)

    def measure_gray_card(self, roi=None) -> dict:
        """카드 영역을 재기만 — 장치를 쥔 데몬이 한다(rsd·camerad 가 같은 동사). 시뮬엔 없다."""
        fn = getattr(self._hub, "measure_gray_card", None)
        if fn is None:
            return {"ok": False, "error": "이 카메라는 회색 카드 보정을 지원하지 않습니다"}
        return fn(self.id, roi)

    def calibrate_gray_card(self, roi=None, target=None, adjust: str = "exposure") -> dict:
        fn = getattr(self._hub, "calibrate_gray_card", None)
        if fn is None:
            return {"ok": False, "error": "이 카메라는 회색 카드 보정을 지원하지 않습니다"}
        return fn(self.id, roi, target, adjust)

    def modes(self) -> list[dict]:
        """장치가 낼 수 있는 `[{width, height, fps}]` — 해상도 드롭다운의 재료.
        데몬이 장치에 직접 물은 값이다(지어내지 않는다). 모르는 데몬은 빈 목록."""
        fn = getattr(self._hub, "modes", None)
        return (fn(self.id) or []) if fn else []

    def capture_preview(self) -> bytes | None:
        """최신 프레임 JPEG. 세그먼트에서 직접 읽는다 — RPC 가 아니다."""
        return self._hub.get_jpeg(self.id)


# 화면도 같은 값으로 거른다. 데몬이 다시 한 번 하한을 맞춘다(piper_cam.idle.MIN_IDLE_S).
MIN_IDLE_STOP_S = 30
MAX_IDLE_STOP_S = 24 * 3600


def _idle_seconds(value) -> int:
    """저장된 값 → 0 또는 [하한, 상한] 안의 정수. 이상한 값은 끈다(0)."""
    try:
        s = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if s <= 0:
        return 0
    return max(MIN_IDLE_STOP_S, min(MAX_IDLE_STOP_S, s))


def parse_capture(value) -> list[int] | None:
    """캡처 모드 입력 → `[w, h]` 또는 None. `"1280x960"`·`[1280, 960]`·빈값을 받는다.

    ⚠ 틀린 값은 **조용히 버리지 않고** 예외로 알린다 — 사람이 적은 값이 무시되면
    "설정했는데 화각이 그대로" 가 된다. 빈값(None·""·[])은 "끈다" 는 뜻이다.
    """
    if value in (None, "", [], ()):
        return None
    if isinstance(value, str):
        parts = value.lower().replace("×", "x").replace(" ", "").split("x")
    else:
        parts = list(value)
    try:
        w, h = (int(v) for v in parts)
    except (TypeError, ValueError):
        raise ValueError(f"캡처 해상도는 '1280x960' 모양이어야 합니다: {value!r}")
    if not (16 <= w <= 8192 and 16 <= h <= 8192):
        raise ValueError(f"캡처 해상도가 범위를 벗어났습니다: {w}x{h}")
    return [w, h]


class CameraManager:
    def __init__(self) -> None:
        self.cameras: dict[str, CameraInfo] = {}

    def scan(self) -> list[dict]:
        """두 데몬의 스캔 결과를 합친다.

        **소유가 겹치지 않는다** — camerad 는 RealSense 노드를 건너뛰고
        rsd 는 v4l2 를 안 본다. 데몬이 죽어 있으면 그쪽만 빠지고 나머지는 보인다.
        """
        seen: set[str] = set()
        # 일반 웹캠 — camerad
        for d in v4l2_hub.scan():
            seen.add(d["id"])
            self._absorb(d)

        # RealSense — rsd. 디바이스당 color/depth/infrared 스트림 엔트리
        for d in realsense_hub.scan():
            seen.add(d["id"])
            self._absorb(d, cam_type="realsense")

        # 시뮬 — simd. 데몬이 없으면 빈 목록 (죽은 데몬을 기다리지 않는 클라이언트)
        from app.services.sim_camera_client import sim_camera_hub
        for d in sim_camera_hub.scan():
            seen.add(d["id"])
            self._absorb(d, cam_type="sim")
            # ⚠ 시뮬 카메라는 simd 의 사실로 connected 를 맞춘다 (sim 팔의 sync_sim_arms 와
            #   같은 규칙). simd 가 재시작하면 세그먼트가 사라지는데 게이트웨이가 True 로
            #   캐시한 채면 창이 연결을 건너뛰어 렌더가 안 돌고 화면이 빈다(사용자 보고 2026).
            if "connected" in d:
                self.cameras[d["id"]].connected = bool(d["connected"])

        # ⚠ **안 보인 카메라는 없는 것으로 표시한다.** 예전에는 보고된 것만 순회해서,
        # USB 가 빠져도 목록이 마지막 상태에 머물렀다 — 사용자가 스캔을 눌러도
        # 아무것도 안 바뀌었다.
        #
        # 등록된 카메라는 **남긴다** — 별칭·매핑을 사람이 정했고, 다시 꽂으면
        # 그대로 돌아와야 한다. 대신 `present: false` 로 표시해 화면이 "없음"을
        # 보여줄 수 있게 한다.
        #
        # 등록 안 된 카메라는 **지운다.** 보존할 사람의 결정이 없고, 남겨두면
        # 뽑아둔 장치가 "미등록" 목록에 계속 떠서 고를 수 있는 것처럼 보인다.
        for cam_id in [c for c in self.cameras if c not in seen]:
            cam = self.cameras[cam_id]
            cam.mark_absent()
            if not cam.ready:
                del self.cameras[cam_id]

        return [c.to_dict() for c in self.cameras.values()]

    def _absorb(self, d: dict, cam_type: str = "opencv") -> None:
        """스캔 엔트리를 흡수한다. **두 데몬이 같은 경로를 탄다.**

        ⚠ 예전에는 v4l2 와 RealSense 가 각자 갱신했고, 한쪽은
        `self.cameras[id].x = ...`, 다른 쪽은 `cam.x = ...` 모양이었다.
        필드를 하나 추가하면서 한쪽만 고쳐져, USB 3 으로 바꿔 꽂아도
        **RealSense 쪽 속도만 옛 값에 머물렀다.** 같은 사실은 한 곳에서 갱신한다.

        등록 정보(label·ready·config)는 건드리지 않는다 — 그건 사람이 정한 것이고
        스캔은 장치가 말해주는 것만 가져온다.
        """
        cam_id = d["id"]
        cam = self.cameras.get(cam_id)
        if cam is None:
            cam = self.cameras[cam_id] = CameraInfo(id=cam_id, name=d["name"],
                                                    cam_type=cam_type)
        cam.name = d["name"]
        cam.present = True
        cam.usb_port = d.get("usb_port", "")
        cam.usb_speed_mbps = int(d.get("usb_speed_mbps") or 0)
        # ⚠ `cam_type` 으로 분기하지 않는다 — "어느 데몬 소유인가"는 `_hub` 한 곳에서만
        # 판단한다(테스트가 강제한다). 여기서는 **엔트리에 있는 것만** 가져오면 된다:
        # v4l2 엔트리에는 serial/stream_type 이 없으므로 자연히 건너뛴다.
        cam.serial = d.get("serial", cam.serial)
        cam.stream_type = d.get("stream_type", cam.stream_type)

    def probe_camera(self, cam_id: str) -> tuple[bool, str]:
        """연결 테스트 + 프리뷰 1장 → 즉시 해제."""
        cam = self.cameras.get(cam_id)
        if not cam:
            return False, f"Unknown camera: {cam_id}"
        return cam.probe()

    def connect_camera(self, cam_id: str) -> tuple[bool, str]:
        """백그라운드 캡처 시작 (등록 시 사용)."""
        cam = self.cameras.get(cam_id)
        if not cam:
            return False, f"Unknown camera: {cam_id}"
        return cam.connect()

    def clear_all(self) -> dict:
        """등록·별칭·스캔 결과를 전부 버리고 연결도 끊는다 — 처음부터 다시 세팅할 때.

        ⚠ **세션 파일까지 지운다.** 메모리만 비우면 게이트웨이가 재시작할 때
        `restore_session()` 이 되살려 놓는다 — "지웠는데 재부팅하니 돌아왔다"는
        안 지운 것보다 나쁘다 (robot_manager.clear_all 과 같은 판단).
        별칭·등록은 사람이 정한 값이지만, 이 버튼의 뜻이 바로 그걸 버리는 것이다 —
        화면이 누르기 전에 그 사실을 말한다.
        """
        disconnected = [c.id for c in self.cameras.values() if c.connected]
        for cid in disconnected:
            try:
                self.cameras[cid].disconnect()
            except Exception as e:      # 한 대가 안 끊겨도 나머지는 지운다
                logger.warning("  %s 연결 해제 실패 (계속): %s", cid, e)
        cleared = len(self.cameras)
        self.cameras = {}
        try:
            from piper_shm import clear_segment_names

            clear_segment_names()
        except Exception:
            pass
        self.CAMERA_SESSION_PATH.unlink(missing_ok=True)
        self.clear_notices()
        logger.info("Camera state cleared (%d cameras, %d disconnected)",
                    cleared, len(disconnected))
        return {"cleared": cleared, "disconnected": disconnected}

    def disconnect_camera(self, cam_id: str) -> bool:
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        cam.disconnect()
        return True

    def update_config(self, cam_id: str, cfg: dict) -> bool:
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        cam.update_config(cfg)
        return True

    def register_camera(self, cam_id: str, name: str | None = None) -> tuple[bool, str]:
        """등록 = 이름 부여 + 자동 connect + `ready`. **실패하면 사유를 같이 돌려준다.**

        `name` 이 비어 있으면(안 줬거나 공백) 기본 이름(`장치명_N`)을 받는다. 이미 이름이
        있는 카메라를 다시 등록하면 **그 이름을 유지한다** — 이름 없이 재등록했다고 사람이
        정한 이름이 기본값으로 돌아가면 안 된다.

        ⚠ 이름은 **장치를 열기 전에** 검증한다. 틀린 이름 때문에 실패할 일이면 카메라를
        열었다 닫는 일(RealSense 는 수 초)을 먼저 하지 않는다.

        ⚠ 예전에는 `bool` 만 돌려주고 `ok, _ = cam.connect()` 로 **사유를 버렸다.**
        화면에는 "등록 실패: 연결되지 않은 카메라입니다" 가 떴는데, 그건 사실이 아니라
        *결과*였다 — 진짜 사유(`Cannot open /dev/video0`, 드라이버 예외)는 데몬이 이미
        말해 주고 있었고 여기서 지워졌다. 현장에서 카메라가 안 잡히는데 화면이 아무
        단서도 안 주는 원인이 이것이었다(2026-09-22, .120 을 들고 나갔을 때).
        """
        cam = self.cameras.get(cam_id)
        if not cam:
            return False, f"알 수 없는 카메라입니다: {cam_id}"
        wanted = None
        if name is not None and str(name).strip():
            try:
                wanted = camera_streams.validate(name)
                camera_streams.check_unique(wanted, self.cameras.values(), except_id=cam_id)
            except camera_streams.StreamNameError as exc:
                return False, str(exc)
        # ⚠ **이름을 연결 전에 정한다.** `connect` 가 이름을 데몬에 먼저 전하므로 그때 이미
        # 있어야 첫 세그먼트가 처음부터 그 이름으로 나간다. 연결이 실패하면 되돌린다 —
        # 등록 안 된 카메라가 이름만 쥐고 있으면 다른 카메라의 기본 번호를 건너뛰게 만든다.
        before = (cam.stream, list(cam.aliases))
        if wanted:
            camera_streams.adopt(cam, wanted, self.cameras.values())
        elif not cam.stream:
            cam.stream = camera_streams.default_name(
                cam.name, camera_streams.reserved_names(self.cameras.values()))
        # 아직 연결 안 되었으면 자동 connect (백그라운드 캡처 시작)
        if not cam.connected:
            ok, msg = cam.connect()
            if not ok:
                cam.stream, cam.aliases = before
                from piper_shm import clear_segment_name

                clear_segment_name(cam.id)
                return False, msg or "카메라를 열지 못했습니다"
        else:
            # 이미 연결 중이면 이름만 전한다 — 발행 중인 세그먼트가 새 이름으로 옮겨진다.
            cam.push_stream()
        cam.ready = True
        return True, "OK"

    def rename_stream(self, cam_id: str, new_name: str) -> bool:
        """등록된 카메라의 스트림 이름을 바꾼다. 없는 카메라면 `False`.

        규칙에 안 맞으면 `StreamNameError`, 다른 카메라가 쓰는 이름이면
        `StreamNameConflict` — 라우터가 400/409 로 옮긴다. **조용히 고쳐 쓰지 않는다.**

        미등록 카메라는 이름이 없다(이름은 등록할 때 정해진다) — 바꿀 대상이 없다.
        """
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        if not cam.ready:
            raise camera_streams.StreamNameError(
                "등록한 카메라만 이름을 바꿀 수 있습니다 — 등록할 때 이름을 정하세요")
        name = camera_streams.validate(new_name)
        camera_streams.check_unique(name, self.cameras.values(), except_id=cam_id)
        camera_streams.adopt(cam, name, self.cameras.values())
        self._apply_stream_name(cam)
        return True

    def _apply_stream_name(self, cam) -> None:
        """레지스트리의 이름을 데몬에 전하고, **확인이 오면** 이쪽도 그 이름을 든다.

        연결 여부를 보지 않는다 — 게이트웨이의 `connected` 는 2초 주기로 따라오는
        캐시라, 방금 연결된 카메라를 "안 열렸다"로 보고 알리지 않으면 데몬은 옛 이름으로
        발행하는데 이쪽은 새 이름을 찾는다. 데몬은 안 열린 카메라에 이름을 받아 두었다가
        연결할 때 그 이름으로 발행한다.

        확인이 안 오면(데몬이 없거나 옛 코드) **아무것도 바꾸지 않는다** — 세그먼트는 옛 이름
        그대로 발행되고 있거나 아직 없다. 다음 연결(`CameraInfo.connect`)이 다시 전한다.
        """
        cam.push_stream()

    # 옛 이름. 낡은 호출부(`/label`)가 그대로 돌게 남긴다 — 개명이다.
    set_label = rename_stream

    def set_idle_stop(self, cam_id: str, seconds: int) -> tuple[bool, int, bool]:
        """유휴 자동 종료 시간을 정한다. `(카메라 있음, 저장된 값, 데몬에 알렸는가)`.

        0 은 끈다. 0 이 아니면 `MIN_IDLE_STOP_S`~`MAX_IDLE_STOP_S` 안이어야 한다 — 범위 밖은
        `ValueError`(조용히 고쳐 쓰면 사람은 자기가 정한 값이 적용된 줄 안다).
        연결 중이면 바로 데몬에 알린다. 아니면 다음 연결이 알린다.
        """
        cam = self.cameras.get(cam_id)
        if not cam:
            return False, 0, False
        s = int(seconds)
        if s != 0 and not (MIN_IDLE_STOP_S <= s <= MAX_IDLE_STOP_S):
            lo, hours = MIN_IDLE_STOP_S, MAX_IDLE_STOP_S // 3600
            raise ValueError(
                f"유휴 종료 시간은 끄거나(0) {lo}초~{hours}시간 사이여야 합니다")
        cam.idle_stop_s = s
        pushed = False
        if cam.connected:
            pushed = cam.push_idle() is not None
        return True, cam.idle_stop_s, pushed

    def unregister_camera(self, cam_id: str) -> bool:
        """등록 해제. **이름도 놓는다** — 이름은 등록에 매달린 것이다.

        이력(`aliases`)도 같이 지운다. 남겨 두면 해제된 카메라의 옛 이름이 다른
        카메라의 기본 이름 번호 매기기에서 영영 비껴가고, 세션에도 안 저장돼 재시작이면
        사라지는 불일치가 생긴다.
        """
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        cam.release_stream()                 # 연결 중이면 데몬도 규칙 이름으로 되돌린다
        cam.ready = False
        cam.stream = ""
        cam.aliases = []
        return True

    # ── 스트림 디렉토리 ──

    def resolve(self, value: str):
        """스트림 이름 · 개명 이력 · 옛 장치 id · 세그먼트 이름 → `CameraInfo`(없으면 `None`)."""
        return camera_streams.resolve(value, self.cameras)

    def sync_idle_stops(self) -> list[str]:
        """데몬이 유휴로 닫은 카메라를 `connected=False` 로 내린다. 내린 카메라 id 를 돌려준다.

        ⚠ **게이트웨이는 데몬이 스스로 닫은 것을 모른다.** 사람이 [연결 해제] 를 누르면 게이트웨이가
        `connected` 를 직접 내리지만, 유휴 종료는 데몬이 혼자 한다. 이걸 안 내리면 선택기가 닫힌
        카메라를 "연결됨"으로 보여 줘서 사람이 골랐다가 시작에서 거절당한다.

        근거는 데몬이 남기는 기록(`cam_idle_key`)이다 — 연결할 때 지워지므로 **있으면 마지막
        연결 뒤에 닫힌 것**이다. 장치를 다시 묻지 않는다(RPC 없이 키 하나).
        """
        changed = []
        for cam in self.cameras.values():
            if (cam.connected and getattr(cam, "idle_stop_s", 0)
                    and _idle_record(cam.id)):
                cam.connected = False
                changed.append(cam.id)
        return changed

    def streams(self) -> list[dict]:
        """등록된 카메라의 스트림 목록 — 선택기가 "보이되 비활성"을 판단하는 재료.

        실제 크기·fps 는 **연결된 것만** 묻는다. 안 열린 카메라에 RPC 를 날려 봐야
        빈 값이고, 데몬이 죽어 있으면 타임아웃만 쌓인다.
        """
        self.sync_idle_stops()
        out = []
        for cam in self.cameras.values():
            if not cam.ready or not cam.stream:
                continue
            w = h = fps = None
            streaming = None
            if cam.connected:
                shape = _header_shape(cam)
                if shape:
                    h, w = shape
                try:
                    fps = int(cam.running_profile().get("fps") or 0) or None
                except Exception as exc:
                    logger.debug("fps 조회 실패 (%s): %s", cam.id, exc)
                streaming = _is_fresh(cam.id)
            row = camera_streams.describe(cam, width=w, height=h, fps=fps,
                                          streaming=streaming)
            # 안 열린 이유를 아는 것은 데몬뿐이다 — "유휴로 종료됨"은 사람이 켠 옵션의 결과이지
            # 사고가 아니다. 구분해서 말해야 USB 를 확인하러 가지 않는다.
            if row["state"] == "idle" and _idle_record(cam.id):
                row["reason"] = "유휴로 종료됨"
            row["idle_stop_s"] = cam.idle_stop_s
            out.append(row)
        return sorted(out, key=lambda d: d["stream"].lower())

    def get_ready_cameras(self) -> list[dict]:
        return [c.to_dict() for c in self.cameras.values() if c.ready]

    def get_preview(self, cam_id: str) -> bytes | None:
        cam = self.cameras.get(cam_id)
        if not cam:
            return None
        return cam.capture_preview()

    def _hub_for_id(self, cam_id: str):
        """등록 전 카메라의 폴백 — id 모양으로 데몬을 고른다. 등록된 것은 `CameraInfo._hub`."""
        if cam_id.startswith("rs:"):
            return realsense_hub
        if cam_id.startswith("/dev/"):
            return v4l2_hub
        return None

    def measure_gray_card(self, cam_id: str, roi=None) -> dict:
        """⚠ 예전엔 라우터가 **rsd 로만** 보내 USB 웹캠이 "Not a RealSense id" 로 거절됐다
        (2026-09-11). 카메라 종류 분기는 `CameraInfo._hub` 한 곳이다 — 여기서도 그것을 탄다."""
        cam = self.cameras.get(cam_id)
        if cam:
            return cam.measure_gray_card(roi)
        hub = self._hub_for_id(cam_id)
        if hub is None:
            return {"ok": False, "error": f"모르는 카메라: {cam_id}"}
        return hub.measure_gray_card(cam_id, roi)

    def calibrate_gray_card(self, cam_id: str, roi=None, target=None,
                            adjust: str = "exposure") -> dict:
        cam = self.cameras.get(cam_id)
        if cam:
            return cam.calibrate_gray_card(roi, target, adjust)
        hub = self._hub_for_id(cam_id)
        if hub is None:
            return {"ok": False, "error": f"모르는 카메라: {cam_id}"}
        return hub.calibrate_gray_card(cam_id, roi, target, adjust)

    def get_controls(self, cam_id: str) -> list[dict]:
        cam = self.cameras.get(cam_id)
        if not cam:
            return []
        controls = cam.get_controls()
        # 표시 단위를 붙인다. 데몬이 아니라 **게이트웨이에서** — 단위 표기를
        # 고치자고 장치를 쥔 데몬을 재시작(스트림 끊김)할 이유가 없다.
        # 지식 자체는 piper_cam.controls 한 곳이다 (조회·리셋 두 라우트가 다 지난다).
        # RealSense `exposure` 는 센서마다 단위가 달라 범위로 가른다(unit_for).
        try:
            from piper_cam.controls import unit_for

            for c in controls or []:
                unit = unit_for(c)
                if unit:
                    c["unit"] = unit
        except Exception:
            pass          # 단위는 장식이다 — 이것 때문에 컨트롤 목록이 죽으면 안 된다
        return controls

    def set_control(self, cam_id: str, name: str, value: float) -> bool:
        cam = self.cameras.get(cam_id)
        if not cam:
            return False
        return cam.set_control(name, value)

    def get_current(self) -> dict:
        return {"cameras": [c.to_dict() for c in self.cameras.values()]}


    # ── 세션 저장/복원 ──

    CAMERA_SESSION_PATH = settings.config_dir / "camera_session.json"

    def save_session(self) -> None:
        """등록된 카메라 상태 저장."""
        import json
        self.CAMERA_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for cam in self.cameras.values():
            if cam.ready:
                data.append({
                    "id": cam.id,
                    # 복원 매칭의 **진짜 키**. `id` 는 참고용으로만 남긴다 —
                    # v4l2 는 재열거로 번호가 바뀐다.
                    "key": cam.profile_key,
                    "name": cam.name,
                    "stream": cam.stream,
                    "aliases": list(cam.aliases),
                    # 옛 필드 — 이 값을 읽는 낡은 게이트웨이로 되돌려도 이름이 보이게 둔다.
                    "label": cam.stream,
                    "cam_type": cam.cam_type,
                    # 사람이 끈 경보는 재시작에도 꺼져 있어야 한다 — 다시 켜지면
                    # "껐는데 또 뜬다"가 되고, 그건 안 끈 것보다 나쁘다.
                    "light_alarm": cam.light_alarm,
                    "idle_stop_s": cam.idle_stop_s,
                    "capture": cam.capture,
                    "output": cam.output,
                    "config": {
                        "width": cam.width, "height": cam.height, "fps": cam.fps,
                        "color_mode": cam.color_mode, "rotation": cam.rotation, "fourcc": cam.fourcc,
                    },
                })
        self.CAMERA_SESSION_PATH.write_text(json.dumps(data, indent=2))
        logger.info("Camera session saved (%d cameras)", len(data))

    def match_saved(self, saved: dict):
        """저장된 항목을 지금 보이는 카메라에 맞춘다. **키 → id → 이름** 순.

        이름 폴백은 **후보가 하나일 때만** 쓴다. 같은 모델 두 대를 서로 다른 포트에
        꽂아 뒀는데 이름으로 맞추면 설정이 엉뚱한 카메라에 붙는다 —
        틀리게 복원하느니 복원 안 하는 게 낫다.
        """
        key = saved.get("key") or ""
        if key:
            for cam in self.cameras.values():
                if cam.profile_key == key:
                    return cam
        cam_id = saved.get("id") or ""
        if cam_id in self.cameras:
            return self.cameras[cam_id]
        name = saved.get("name") or ""
        if name:
            hits = [c for c in self.cameras.values() if c.name == name]
            if len(hits) == 1:
                return hits[0]
        return None

    def restore_session(self) -> bool:
        """세션 파일에서 카메라 상태 복원."""
        import json
        if not self.CAMERA_SESSION_PATH.exists():
            return False
        try:
            data = json.loads(self.CAMERA_SESSION_PATH.read_text())
        except Exception:
            return False
        if not data:
            return False

        logger.info("Restoring camera session (%d cameras)...", len(data))

        # 먼저 스캔
        self.scan()

        restored = 0
        pairs: list[tuple] = []
        for cam_data in data:
            cam = self.match_saved(cam_data)
            if cam is None:
                logger.warning("  Session camera %s(%s) not found in scan, skipping",
                               cam_data.get("id", ""), cam_data.get("key", ""))
                continue
            cam.cam_type = cam_data.get("cam_type", "opencv")
            # 옛 세션 파일에는 이 키가 없다 — 없으면 켜 둔다(기본이 감시다)
            cam.light_alarm = bool(cam_data.get("light_alarm", True))
            # 옛 세션 파일에는 없다 — 끔(0)이 기본이다. 사람이 켠 옵션만 남는다.
            cam.idle_stop_s = _idle_seconds(cam_data.get("idle_stop_s"))
            cam.capture = parse_capture(cam_data.get("capture"))
            cam.output = parse_capture(cam_data.get("output"))
            cam.update_config(cam_data.get("config", {}))
            cam.ready = True
            restored += 1
            pairs.append((cam, cam_data))

        # 이름은 **모두 읽은 뒤에** 정한다 — 한 대의 기본 이름이 아직 처리 안 된 다른
        # 카메라의 저장된 이름을 가로채면 안 된다. 저장된 이름이 먼저, 마이그레이션이 나중이다.
        migrated = False
        for cam, cam_data in pairs:
            self._restore_saved_name(cam, cam_data)
        for cam, cam_data in pairs:
            if not cam.stream:
                migrated = self._migrate_name(cam, cam_data) or migrated
            logger.info("  Restored camera %s (%s)", cam.id, cam.stream or cam.name)

        # 마이그레이션이 있었으면 곧바로 새 형식으로 쓴다 — 안 쓰면 재시작 때마다
        # 같은 변환과 같은 안내를 되풀이한다.
        if migrated:
            self.save_session()

        # **복원한 이름을 데몬에 알린다.** 데몬은 게이트웨이보다 오래 산다 — 게이트웨이만
        # 재시작하면 데몬은 카메라를 쥔 채 옛(규칙) 이름으로 발행 중이고, 이쪽은 방금 복원한
        # 새 이름을 찾는다. 알리면 발행 중인 세그먼트가 그 이름으로 옮겨진다. 데몬이 없거나
        # 옛 코드면 규칙 이름 그대로 두고 넘어간다(연결 때 다시 시도한다).
        for cam, _ in pairs:
            cam.push_stream()

        logger.info("Camera session restored: %d/%d cameras", restored, len(data))
        return restored > 0

    def _restore_saved_name(self, cam, saved: dict) -> bool:
        """새 형식(`stream`)으로 저장된 이름을 쓸 수 있으면 적용하고 `True`."""
        stream = saved.get("stream")
        if not stream:
            return False
        try:
            name = camera_streams.validate(stream)
        except camera_streams.StreamNameError:
            return False
        if name.lower() in camera_streams.names_in_use(self.cameras.values(), except_id=cam.id):
            return False
        cam.stream = name
        cam.aliases = []
        for alias in saved.get("aliases") or []:
            try:
                cam.aliases.append(camera_streams.validate(alias))
            except camera_streams.StreamNameError:
                continue            # 손으로 고친 파일의 쓰레기 — 이력일 뿐이라 버린다
        del cam.aliases[:-camera_streams.MAX_ALIASES]
        return True

    def _migrate_name(self, cam, saved: dict) -> bool:
        """이름이 없는 등록 카메라에 이름을 준다 — 옛 별칭을 이어받거나 기본 이름.

        옛 별칭이 영문으로 쓸 수 있으면(`Top View` → `Top_View`) 그대로 이름이 된다.
        못 쓰는 별칭(`탑뷰`)은 기본 이름을 받고 **그 사실을 안내로 남긴다** — 사람이 정한
        값이 흔적 없이 바뀌면 안 된다. 안내는 카메라 페이지가 보여 주고 사람이 지운다.
        """
        old = str(saved.get("stream") or saved.get("label") or "").strip()
        wanted = camera_streams.migrate_label(old) if old else None
        taken = camera_streams.names_in_use(self.cameras.values(), except_id=cam.id)
        if wanted and wanted.lower() not in taken:
            cam.stream, cam.aliases = wanted, []
            return True
        cam.stream = camera_streams.default_name(
            cam.name, camera_streams.reserved_names(self.cameras.values()))
        cam.aliases = []
        if old:
            device, new_name = cam.name, cam.stream
            self._add_notice(
                f"카메라 '{device}' 의 이름 '{old}' 을(를) 그대로 쓸 수 없어 '{new_name}' 로 "
                "정했습니다 (영문·숫자·_·- 만 가능하고 다른 카메라와 겹치면 안 됩니다). "
                "카메라 페이지에서 고칠 수 있습니다."
            )
        return True

    # ── 안내 (마이그레이션이 사람이 정한 값을 바꿨을 때) ──
    #
    # 시작할 때 일어나는 일이라 그 순간엔 브라우저가 없다 — 보낼 곳이 없으므로 파일에
    # 남기고, 카메라 페이지가 열릴 때 보여 주고 사람이 지운다.

    def _notices_path(self):
        return self.CAMERA_SESSION_PATH.with_name("camera_notices.json")

    def notices(self) -> list[str]:
        import json
        try:
            data = json.loads(self._notices_path().read_text())
        except Exception:
            return []
        return [s for s in data if isinstance(s, str)] if isinstance(data, list) else []

    def _add_notice(self, text: str) -> None:
        import json
        try:
            items = self.notices()
            if text not in items:
                items.append(text)
            path = self._notices_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items, ensure_ascii=False, indent=2))
        except Exception as exc:        # 안내가 안 남아도 복원은 계속한다
            logger.warning("카메라 안내를 저장하지 못했습니다: %s", exc)

    def clear_notices(self) -> None:
        self._notices_path().unlink(missing_ok=True)


camera_manager = CameraManager()
