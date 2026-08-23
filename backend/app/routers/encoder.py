"""이미지 엔코더 프로브 — 추론과 무관한 오프라인 진단.

이미지 한 장을 정책의 이미지 엔코더에만 통과시켜 패치 특징을 뽑고, 그 위에서
PCA / 클릭 코사인 유사도 / k-means 를 계산해 돌려준다. 로봇을 움직이지 않으므로
E-stop 이나 추론 루프와 무관하다.

모델 실행은 wrapper/encoder_probe.py subprocess 가 담당하고(백엔드는 torch 미사용),
이후 상호작용은 캐시된 특징에서 numpy 로 즉시 계산된다.
"""

import asyncio
import base64
import binascii
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.services.camera_manager import camera_manager
from app.services.encoder_probe import (
    MAX_BATCH,
    MAX_SESSIONS,
    cosine_map,
    encoder_probe_manager,
    kmeans_labels,
    patch_matrix,
    pca_rgb,
)
from app.core.policies import encoder_probe_policies
from app.services.exclusivity import Activity, blocked_reason
from app.services.model_scanner import scan_models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/encoder", tags=["encoder"])

# 엔코더 실행은 GPU를 잡으므로 한 번에 하나씩. 다만 겹친 요청을 곧바로 거절하면
# 슬롯 A/B 를 연달아 누르는 정상적인 사용이 실패하므로, 짧게 줄을 세운다.
# (1회 4초 안팎이라 대기가 자연스럽다. 무한정 쌓이는 것만 막는다.)
_run_lock = asyncio.Lock()
_QUEUE_LIMIT = 3
_waiting = 0

# 정책 레지스트리에서 파생 — 정책을 추가할 때 여기를 따로 고칠 필요가 없다
SUPPORTED = encoder_probe_policies()


def _gpu_busy() -> str:
    """학습/추론이 GPU를 쓰고 있으면 사유를 반환. 막지는 않고 CPU 폴백에 쓴다."""
    return blocked_reason(Activity.ENCODER_PROBE) or ""


@router.get("/models")
async def list_encoder_models():
    """프로브가 지원하는 정책의 체크포인트 목록."""
    models = [m for m in scan_models() if m.get("policy_type") in SUPPORTED]
    models.sort(key=lambda m: m.get("modified") or "", reverse=True)  # 최신 체크포인트가 위로
    return [
        {
            "id": m["id"],
            "path": m["path"],
            "policy_type": m["policy_type"],
            "modified": m.get("modified"),
            "cameras": [c.get("name") for c in m.get("requirements", {}).get("required_cameras", [])],
        }
        for m in models
    ]


@router.get("/sessions")
async def list_sessions():
    return {"sessions": encoder_probe_manager.list(), "gpu_busy": _gpu_busy()}


class EncodeRequest(BaseModel):
    policy_type: str
    source: str = "camera"  # camera | upload
    camera_id: str = ""
    image_b64: str = ""  # data URL 또는 순수 base64
    checkpoint_path: str = ""
    image_key: str = ""
    tap: str = "siglip"
    device: str = ""  # 비우면 GPU 사용 여부를 자동 판단


def _decode_upload(raw: str) -> bytes:
    payload = raw.split(",", 1)[1] if raw.startswith("data:") else raw
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "이미지 디코딩에 실패했습니다")


async def _run_batch(images: list[bytes], policy_type: str, checkpoint: str, image_key: str,
                     tap: str, device: str) -> tuple[list, str, str]:
    """줄 세우기 + 락 + 실행. (결과 목록, gpu_busy, device_note) 를 돌려준다."""
    if policy_type not in SUPPORTED:
        raise HTTPException(400, f"지원하지 않는 정책: {policy_type}")
    busy = _gpu_busy()
    chosen = device or ("cpu" if busy else "cuda")

    global _waiting
    if _waiting >= _QUEUE_LIMIT:
        raise HTTPException(429, f"엔코딩 요청이 밀려 있습니다 (대기 {_waiting}건). 잠시 후 다시 시도하세요")
    _waiting += 1
    try:
        async with _run_lock:
            results = await asyncio.to_thread(
                encoder_probe_manager.run_many,
                images, policy_type, checkpoint, image_key, tap, chosen,
            )
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    finally:
        _waiting -= 1
    note = f"{busy} 중이라 CPU로 실행했습니다" if busy and not device else ""
    return results, busy, note


@router.post("/encode")
async def encode(body: EncodeRequest):
    # ⚠ ACT 도 체크포인트 없이 돌 수 있다. 백본이 **무작위에서 시작하지 않고**
    # ImageNet 사전학습 ResNet-18 로 초기화되기 때문이다(`ACTConfig` 기본값).
    # 그 시작점을 못 보면 "학습이 엔코더를 좋게 만들었나"를 잴 기준이 없다.

    if body.source == "camera":
        if not body.camera_id:
            raise HTTPException(400, "카메라를 선택하세요")
        image = camera_manager.get_preview(body.camera_id)
        if image is None:
            raise HTTPException(404, f"카메라 프레임을 가져올 수 없습니다: {body.camera_id}")
    elif body.source == "upload":
        if not body.image_b64:
            raise HTTPException(400, "이미지가 비어 있습니다")
        image = _decode_upload(body.image_b64)
    else:
        raise HTTPException(400, f"알 수 없는 소스: {body.source}")

    results, busy, note = await _run_batch(
        [image], body.policy_type, body.checkpoint_path, body.image_key, body.tap, body.device)
    sess = results[0]
    if isinstance(sess, str):
        raise HTTPException(500, sess)

    return {"sid": sess.sid, "meta": sess.meta, "gpu_busy": busy, "device_note": note}


class EncodeBatchRequest(BaseModel):
    policy_type: str
    images: list[str]  # data URL 또는 순수 base64, 여러 장
    checkpoint_path: str = ""
    image_key: str = ""
    tap: str = "siglip"
    device: str = ""


@router.post("/encode_batch")
async def encode_batch(body: EncodeBatchRequest):
    """여러 장을 **모델 로드 한 번**으로 인코딩한다.

    결과는 입력 순서대로이고, 한 장만 실패하면 그 자리에 `{"error": ...}` 가 온다.
    카메라 프레임은 브라우저가 `/api/cameras/{id}/preview` 로 먼저 받아 목록에
    넣어 두므로 여기서는 전부 업로드로 취급한다.
    """
    if not body.images:
        raise HTTPException(400, "이미지가 비어 있습니다")
    if len(body.images) > MAX_BATCH:
        raise HTTPException(400, f"한 번에 {MAX_BATCH}장까지 인코딩할 수 있습니다 ({len(body.images)}장 요청)")
    images = [_decode_upload(raw) for raw in body.images]

    results, busy, note = await _run_batch(
        images, body.policy_type, body.checkpoint_path, body.image_key, body.tap, body.device)
    return {
        "results": [
            {"error": r} if isinstance(r, str) else {"sid": r.sid, "meta": r.meta}
            for r in results
        ],
        "gpu_busy": busy,
        "device_note": note,
        "max_sessions": MAX_SESSIONS,
    }


def _session(sid: str):
    sess = encoder_probe_manager.get(sid)
    if not sess:
        raise HTTPException(404, "세션을 찾을 수 없습니다 (서버 재시작 또는 만료)")
    return sess


def _ref(sid: str, ref: str | None):
    """비교 기준 세션. 지정이 없으면 자기 자신."""
    target = _session(sid)
    reference = _session(ref) if ref and ref != sid else target
    if reference.features().shape[1] != target.features().shape[1]:
        raise HTTPException(400, "특징 차원이 달라 비교할 수 없습니다 (같은 모델/탭으로 인코딩하세요)")
    return target, reference


@router.get("/patch_matrix")
async def patch_matrix_route(patch: int, sids: str):
    """`sids`(쉼표 구분) 이미지들의 같은 패치 위치 특징끼리 코사인 행렬.

    만료된 세션은 404 가 아니라 빈 행·열이다 — 여러 장 중 하나가 밀려났다고
    표 전체가 사라지면 안 된다.
    """
    ids = [x for x in sids.split(",") if x]
    if not ids:
        raise HTTPException(400, "sids 가 비어 있습니다")
    if len(ids) > MAX_SESSIONS:
        raise HTTPException(400, f"한 번에 {MAX_SESSIONS}장까지 비교할 수 있습니다")
    sessions = [encoder_probe_manager.get(x) for x in ids]
    out = patch_matrix(sessions, patch)
    out["sids"] = ids
    return out


@router.get("/{sid}/input.jpg")
async def input_image(sid: str):
    sess = _session(sid)
    path = sess.path / "input.jpg"
    if not path.exists():
        raise HTTPException(404, "입력 이미지가 없습니다")
    return Response(content=path.read_bytes(), media_type="image/jpeg")


@router.get("/{sid}/source.jpg")
async def source_image(sid: str):
    sess = _session(sid)
    path = sess.path / "source.jpg"
    if not path.exists():
        raise HTTPException(404, "원본 이미지가 없습니다")
    return Response(content=path.read_bytes(), media_type="image/jpeg")


@router.get("/{sid}/pca")
async def pca(sid: str, ref: str | None = None):
    target, reference = _ref(sid, ref)
    return pca_rgb(target, reference)


@router.get("/{sid}/similarity")
async def similarity(sid: str, patch: int, ref: str | None = None):
    target, reference = _ref(sid, ref)
    try:
        return cosine_map(target, reference, patch)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/{sid}/kmeans")
async def kmeans(sid: str, k: int = 6):
    return kmeans_labels(_session(sid), k)


@router.delete("/{sid}")
async def delete_session(sid: str):
    if not encoder_probe_manager.delete(sid):
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    return {"status": "ok"}
