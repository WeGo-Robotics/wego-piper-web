"""데이터셋 썸네일 — 데이터셋을 고르는 창(에피소드 페이지)의 카드 그림.

ID(`wego-mink/singlearm_apple_random_v2`)만으로는 무엇을 모았는지 안 읽힌다. 한 장이면
사과인지 볼트인지, 한 팔인지 두 팔인지가 보인다.

⚠ **브라우저에 mp4 를 51개 열게 하지 않는다.** 오리진당 연결이 6개라 영상 메타데이터를
  받는 동안 다른 요청이 줄을 선다(버튼이 조용히 안 먹던 그 문제). 여기서 작은 JPEG 한 장을
  만들어 캐시하고, 화면은 그것만 받는다.
⚠ **데이터셋 폴더에는 쓰지 않는다.** `meta/` 는 허브 업로드에 동반되고, 이건 그 데이터의
  일부가 아니다. 게이트웨이 데이터 폴더(`thumbnails/`)에 둔다.
"""

import hashlib
import json
import logging
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

MAX_W = 320


def _camera(info: dict) -> str | None:
    """영상으로 저장된 카메라 중 하나. 위에서 내려다보는 카메라가 장면을 가장 잘 보여준다."""
    keys = [k for k, f in (info.get("features") or {}).items()
            if k.startswith("observation.images.") and (f or {}).get("dtype") == "video"]
    return next((k for k in keys if "top" in k), keys[0] if keys else None)


def thumbnail(ds_path: Path) -> Path | None:
    """캐시된 JPEG 경로. 영상이 없거나 못 읽으면 None.

    캐시 키는 영상 파일의 경로·크기·수정 시각 — 다시 녹화하면 새로 만든다.
    """
    try:
        info = json.loads((ds_path / "meta" / "info.json").read_text())
    except (OSError, ValueError):
        return None
    key = _camera(info)
    videos = sorted((ds_path / "videos" / key).glob("**/*.mp4")) if key else []
    if not videos:
        return None
    video = videos[0]
    st = video.stat()
    tag = hashlib.sha1(f"{video}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
    out = settings.log_dir / "thumbnails" / f"{tag}.jpg"
    if out.exists():
        return out

    import cv2

    # 첫 에피소드의 가운데 — 시작 자세보다 무엇을 하는 데이터인지 보인다.
    # 첫 파일은 에피소드 0 으로 시작한다(LeRobot v3 `file-000`)
    fps = info.get("fps") or 30
    eps = info.get("total_episodes") or 1
    frames = info.get("total_frames") or 0
    at_ms = (frames / eps / fps / 2 * 1000) if frames else 1000
    cap = cv2.VideoCapture(str(video))
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, at_ms)
        ok, img = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_MSEC, 0)
            ok, img = cap.read()
    finally:
        cap.release()
    if not ok or img is None:
        logger.warning("썸네일 프레임을 못 읽음: %s", video)
        return None
    h, w = img.shape[:2]
    if w > MAX_W:
        img = cv2.resize(img, (MAX_W, int(h * MAX_W / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(buf.tobytes())
    tmp.replace(out)
    return out
