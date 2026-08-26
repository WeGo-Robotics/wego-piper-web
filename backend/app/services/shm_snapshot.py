"""shm 세그먼트의 최신 프레임을 JPEG 로 — 스냅샷·캡처가 공유하는 한 조각.

읽기는 memcpy 한 번이라 이벤트 루프에서 바로 해도 된다
(realsense_manager.get_jpeg 와 같은 판단).
"""

import logging

logger = logging.getLogger(__name__)


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
        import cv2

        # ⚠ 여기서 `frame[:, :, ::-1]` 로 채널을 뒤집었었다 — "세그먼트는 RGB" 라는
        #   주석과 함께. **세그먼트는 BGR 이다.** rsd 는 `to_bgr` 를 거친 프레임을,
        #   camerad 는 OpenCV 프레임을 그대로 발행한다. 그래서 이 경로로 나가는
        #   화면만 노란 물체가 파랗게 나왔다(비전 페이지, YOLO 캡처). 같은
        #   세그먼트를 읽는 다른 두 곳은 안 뒤집는데 여기만 달랐다.
        ok, buf = cv2.imencode(".jpg", got[0], [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None
    except Exception as exc:
        logger.warning("세그먼트 스냅샷 실패 (%s): %s", name, exc)
        return None
    finally:
        sub.close()
