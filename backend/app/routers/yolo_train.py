"""YOLO 학습 데이터셋 API — 캡처·가져오기·갤러리 (feature/yolo-training.md 1단계).

vision.py(yolod 제어·판단)와 접두사를 나눈 이유: 저쪽은 "돌리는" 화면,
여기는 "만드는" 화면이라 커지는 방향이 다르고, 라우터 접두사 유일 불변식
(test_router_registration)이 같은 접두사 공유를 금지한다.
"""

import functools
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services import yolo_dataset as yd
from app.services.yolo_dataset import YoloDatasetError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/yolo", tags=["yolo-train"])

# 캡처 이미지 상한 — 카메라 프레임 JPEG 는 수백 KB, 외부 사진도 수 MB 면 충분
_IMAGE_LIMIT_MB = 20


def _wrap(fn):
    """YoloDatasetError(status) → HTTPException. 라우트마다 try 를 반복하지 않는다."""

    @functools.wraps(fn)
    async def inner(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except YoloDatasetError as e:
            raise HTTPException(e.status, str(e))

    return inner


# ── 데이터셋 CRUD ──


class CreateRequest(BaseModel):
    name: str
    classes: list[str]


@router.get("/datasets")
async def list_datasets():
    return {"datasets": yd.list_datasets()}


@router.post("/datasets")
@_wrap
async def create_dataset(body: CreateRequest):
    return yd.create_dataset(body.name, body.classes)


@router.delete("/datasets/{name}")
@_wrap
async def delete_dataset(name: str):
    yd.delete_dataset(name)
    return {"deleted": name}


class ClassesRequest(BaseModel):
    classes: list[str]


@router.post("/datasets/{name}/classes")
@_wrap
async def add_classes(name: str, body: ClassesRequest):
    """추가만 — 삭제·순서 변경은 기존 라벨 id 를 어긋나게 한다 (service 주석)."""
    return {"classes": yd.add_classes(name, body.classes)}


# ── 캡처 (라이브 세그먼트) ──


class CaptureRequest(BaseModel):
    cam: str    # 세그먼트 이름 (rs_..._color)


@router.post("/datasets/{name}/capture")
@_wrap
async def capture_live(name: str, body: CaptureRequest):
    from app.services.shm_snapshot import segment_jpeg

    data = segment_jpeg(body.cam)
    if data is None:
        raise HTTPException(404, "세그먼트 또는 프레임 없음 — 카메라가 살아 있습니까?")
    fname = yd.add_image(name, data, {"type": "live", "cam": body.cam})
    return {"file": fname}


# ── 에피소드 가져오기 (디코딩 캐시 → 파일 복사) ──


class ImportEpisodeRequest(BaseModel):
    dataset_id: str
    episode: int = Field(ge=0)
    cam: str                      # observation.images.<cam> 의 <cam>
    stride: int = Field(default=30, ge=1)   # 30fps 기준 1초 1장
    indices: list[int] | None = None        # 주면 stride 무시, 낱장 지정


def _episode_cache_dir(dataset_id: str, episode: int, cam: str):
    from app.services.dataset_scanner import find_dataset_path

    ds_path = find_dataset_path(dataset_id)
    if not ds_path:
        raise HTTPException(404, "LeRobot 데이터셋을 찾을 수 없습니다")
    key = cam if cam.startswith("observation.images.") else f"observation.images.{cam}"
    ep_dir = ds_path / "images" / key / f"episode-{episode:06d}"
    if not ep_dir.is_dir():
        raise HTTPException(
            404, "디코딩 캐시에 이 에피소드가 없습니다 — decode-cache 를 먼저 생성하세요")
    return ep_dir


def _frame_jpeg(path) -> bytes:
    """캐시 프레임 → JPEG 바이트. png 폴백 캐시는 여기서 재인코딩한다."""
    if path.suffix == ".jpg":
        return path.read_bytes()
    import cv2

    img = cv2.imread(str(path))
    if img is None:
        raise HTTPException(500, f"프레임을 읽지 못했습니다: {path.name}")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise HTTPException(500, "재인코딩 실패")
    return buf.tobytes()


@router.post("/datasets/{name}/import-episode")
@_wrap
async def import_episode(name: str, body: ImportEpisodeRequest):
    ep_dir = _episode_cache_dir(body.dataset_id, body.episode, body.cam)
    frames = sorted(ep_dir.glob("frame-*.jpg")) or sorted(ep_dir.glob("frame-*.png"))
    if not frames:
        raise HTTPException(404, "캐시 디렉토리가 비어 있습니다")

    if body.indices is not None:
        picked = [frames[i] for i in body.indices if 0 <= i < len(frames)]
    else:
        picked = frames[::body.stride]

    added = []
    for f in picked:
        frame_no = int(f.stem.split("-")[1])
        fname = yd.add_image(name, _frame_jpeg(f), {
            "type": "episode", "dataset": body.dataset_id,
            "episode": body.episode, "cam": body.cam, "frame": frame_no,
        })
        added.append(fname)
    logger.info("에피소드 가져오기: %s ep%d/%s → %s (%d장/%d장 중)",
                body.dataset_id, body.episode, body.cam, name, len(added), len(frames))
    return {"added": len(added), "total_frames": len(frames), "files": added}


# ── 범용 이미지 업로드 (뷰어 동영상 캡처 · 외부 사진) ──


@router.post("/datasets/{name}/images")
@_wrap
async def upload_image(
    name: str,
    request: Request,
    type: str = "upload",           # live | episode | upload
    cam: str | None = None,
    dataset: str | None = None,
    episode: int | None = None,
    t: float | None = None,         # 동영상 캡처의 재생 시각(초)
):
    """raw JPEG 바디. 가중치 업로드와 같은 방식 — multipart 의존성 없음."""
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > _IMAGE_LIMIT_MB * 1_000_000:
            raise HTTPException(413, f"{_IMAGE_LIMIT_MB}MB 를 넘습니다")
    source = {"type": type}
    for k, v in (("cam", cam), ("dataset", dataset), ("episode", episode), ("t", t)):
        if v is not None:
            source[k] = v
    fname = yd.add_image(name, bytes(data), source)
    return {"file": fname}


# ── 갤러리 ──


@router.get("/datasets/{name}/images")
@_wrap
async def list_images(name: str):
    # summarize 의 images 는 카운트 — 목록과 키가 겹치므로 dataset 아래로
    return {"images": yd.list_images(name), "dataset": yd.summarize(name)}


@router.get("/datasets/{name}/images/{fname}")
@_wrap
async def get_image(name: str, fname: str):
    return FileResponse(yd.image_path(name, fname),
                        # 파일명이 uuid 라 내용이 바뀔 일이 없다
                        headers={"Cache-Control": "public, max-age=86400, immutable"})


@router.delete("/datasets/{name}/images/{fname}")
@_wrap
async def delete_image(name: str, fname: str):
    yd.delete_image(name, fname)
    return {"deleted": fname}
