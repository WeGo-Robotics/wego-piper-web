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

        frame = got[0]
        if frame.ndim == 3 and frame.shape[2] == 3:
            frame = frame[:, :, ::-1]  # 세그먼트는 RGB, imencode 는 BGR
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None
    except Exception as exc:
        logger.warning("세그먼트 스냅샷 실패 (%s): %s", name, exc)
        return None
    finally:
        sub.close()
