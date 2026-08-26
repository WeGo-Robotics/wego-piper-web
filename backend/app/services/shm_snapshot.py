"""shm 세그먼트의 최신 프레임을 JPEG 로 — 스냅샷·캡처가 공유하는 한 조각.

읽기는 memcpy 한 번이라 이벤트 루프에서 바로 해도 된다
(realsense_manager.get_jpeg 와 같은 판단).
"""

import contextlib
import logging

logger = logging.getLogger(__name__)


def encode_bgr(frame, quality: int = 80) -> bytes | None:
    """BGR 배열 → JPEG. **세그먼트를 읽는 모든 경로가 이 함수를 지난다.**

    예전에 이 인코딩이 세 벌로 복사돼 있었고 한 벌만 채널을 뒤집어서 노란 물체가
    파랗게 나왔다. 스트림이 네 번째 사본이 되면 안 된다.
    """
    import cv2

    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def segment_reader(name: str, quality: int = 80):
    """세그먼트를 **한 번 열고** 계속 읽는 함수를 돌려준다.

    스트림은 초당 십수 번 읽는다 — 매번 열고 닫으면 그 자체가 부하가 되고,
    `segment_jpeg` 이 하는 일이 정확히 그거라 스트림에는 안 맞는다.

    ⚠ **새 프레임일 때만** 바이트를 돌려준다(`seq` 비교). 같은 프레임을 다시
    보내면 대역폭만 쓰고 화면은 그대로다.
    """
    from piper_shm import Subscriber, segment_for_camera

    seg = segment_for_camera(name)
    # ⚠ **여기서 열지 않는다.** 열다 실패하면 스트림이 시작조차 못 하고, 그러면
    #   아래 재개방 로직에 닿을 일이 영영 없다 — 발행자가 잠깐 내려간 사이
    #   화면을 열면 그대로 깨진 이미지로 남는다.
    state: dict = {"sub": None, "seq": -1}

    def _next() -> bytes | None:
        # ⚠ **세그먼트가 사라져도 스트림을 끝내지 않는다.** camerad/rsd 를 재시작하면
        #   세그먼트가 잠깐 없어지는데, 여기서 포기하면 `<img>` 는 마지막 프레임을
        #   띄운 채 얼어붙고 **스스로 다시 붙지 않는다.** 사용자는 화면이 멈춘 줄
        #   모르고 옛 장면을 본다 — 로봇 화면에서 그건 위험한 종류의 거짓말이다.
        if state["sub"] is None:
            try:
                state["sub"] = Subscriber(seg)
                state["seq"] = -1
            except Exception:
                return None          # 아직 없다 — 다음 주기에 다시
        # ⚠ **예외를 기다리면 안 된다.** 발행자가 재시작해도 이쪽 mmap 은 계속
        #   읽히고 같은 프레임만 나온다 (`Subscriber.orphaned` 주석 참고).
        if state["sub"].orphaned:
            with contextlib.suppress(Exception):
                state["sub"].close()
            state["sub"] = None
            return None
        try:
            got = state["sub"].read()
        except Exception:
            with contextlib.suppress(Exception):
                state["sub"].close()
            state["sub"] = None
            return None
        if got is None or got[1] == state["seq"]:
            return None
        state["seq"] = got[1]
        return encode_bgr(got[0], quality)

    return _next


def segment_jpeg(name: str, quality: int = 80) -> bytes | None:
    """세그먼트 최신 프레임 → JPEG. 세그먼트/프레임이 없으면 None."""
    from piper_shm import SegmentError, Subscriber, segment_for_camera

    try:
        sub = Subscriber(segment_for_camera(name))
    except SegmentError:
        return None
    try:
        got = sub.read()
        if got is None:
            return None
        # ⚠ 여기서 `frame[:, :, ::-1]` 로 채널을 뒤집었었다 — "세그먼트는 RGB" 라는
        #   주석과 함께. **세그먼트는 BGR 이다.** rsd 는 `to_bgr` 를 거친 프레임을,
        #   camerad 는 OpenCV 프레임을 그대로 발행한다. 그래서 이 경로로 나가는
        #   화면만 노란 물체가 파랗게 나왔다(비전 페이지, YOLO 캡처). 같은
        #   세그먼트를 읽는 다른 두 곳은 안 뒤집는데 여기만 달랐다.
        return encode_bgr(got[0], quality)
    except Exception as exc:
        logger.warning("세그먼트 스냅샷 실패 (%s): %s", name, exc)
        return None
    finally:
        sub.close()
