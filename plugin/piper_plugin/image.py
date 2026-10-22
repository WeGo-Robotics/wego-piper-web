"""BGR 프레임 → JPEG. **세그먼트를 읽어 그림으로 내보내는 모든 경로가 이 함수를 지난다.**

예전에 이 인코딩이 세 벌로 복사돼 있었고 한 벌만 채널을 뒤집어서 노란 물체가 파랗게
나왔다(backend `shm_snapshot.py` 의 사고 기록). 게이트웨이도 플러그인도 여기 하나를 쓴다.
세그먼트는 **BGR** 이다 — 뒤집지 않는다.
"""

from __future__ import annotations


def encode_bgr(frame, quality: int = 80) -> bytes | None:
    import cv2

    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    return buf.tobytes() if ok else None
