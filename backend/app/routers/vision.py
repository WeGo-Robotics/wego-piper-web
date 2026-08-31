"""YOLO 검출 · LLM 판단 테스트 API — 분리수거 파이프라인의 앞 두 칸을 화면에서 굴린다.

yolod 는 정책 서버와 같은 "필요할 때 켜는 유닛"이다 — `make_process` 가 소유자를
정하므로 게이트웨이를 재시작해도 산다. 판단은 [llm_client](../services/llm_client.py)
를 **직접 호출**한다 (자기 HTTP 호출 금지 — episode-orchestrator §2 와 같은 규칙).
"""

import json
import logging
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services import llm_client
from app.services.llm_client import LLMJudgeError
from app.services.systemd_process import make_process

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/vision", tags=["vision"])

# yolod 소유자 — 유닛이면 게이트웨이 재시작에도 산다 (기동 재부착은 사소해서 생략:
# 테스트 도구라 화면에서 다시 켜면 그만이고, 유닛 자체는 계속 돈다)
_yolod_pm = make_process("piper-yolod")

_YOLOD_SCRIPT = Path(__file__).resolve().parents[3] / "daemons" / "yolod.py"

# 규칙·슬롯의 정본은 오케스트레이터 서비스 — 테스트 페이지와 루프가 같은 판단을 쓴다
from app.services.orchestrator import DEFAULT_RULES, JudgeSlots  # noqa: E402


# ── yolod 제어 ──

# 시작 UI 에 보여줄 표준 검출 모델 (COCO 80 클래스). 로컬에 없어도 목록에 나온다 —
# ultralytics 가 없는 가중치를 첫 로드 때 자동 다운로드하므로 전부 쓸 수 있고,
# `downloaded` 는 "첫 시작이 다운로드만큼 느릴지"를 미리 알려주는 표시일 뿐이다.
# ⚠ **ultralytics 를 걷어냈다.** AGPL-3.0 이라 배포물 전체를 물들이고, 비상업
# 조건을 붙이는 것도 막는다(AGPL §7). RT-DETR 은 아키텍처가 Apache-2.0 이고
# `transformers` 에 구현이 있어, 가중치까지 원 배포본(PekingU/*, Apache-2.0)으로
# 받으면 카피레프트가 남지 않는다.
#
# ⚠ **아키텍처가 Apache 인 것과 가중치가 Apache 인 것은 다르다.** 예전 카탈로그의
# `rtdetr-l.pt`·`rtdetr-x.pt` 는 아키텍처만 Apache 이고 가중치는 ultralytics
# 배포본이라 AGPL 이었다. 이름만 바꿔서는 안 바뀐다 — 출처를 바꿔야 한다.
#
# `file` 이라는 키 이름은 그대로 둔다. 이제 파일명이 아니라 **모델 id** 지만,
# 프론트가 이 키로 읽고 있어 이름만 바꾸면 화면이 통째로 빈다.
# `size_mb` 는 배포 URL 에 HEAD 를 쳐서 실측한 값이다. `params_m` 은 확인 안 한
# 수치를 적느니 비워둔다.
_DETECTOR_CATALOG = [
    # v2 — 같은 크기에서 v1 보다 낫다. 기본값은 여기서 고른다.
    {"family": "RT-DETRv2", "file": "PekingU/rtdetr_v2_r18vd",  "label": "r18",  "params_m": None, "size_mb": 80.9},
    {"family": "RT-DETRv2", "file": "PekingU/rtdetr_v2_r34vd",  "label": "r34",  "params_m": None, "size_mb": 126.0},
    {"family": "RT-DETRv2", "file": "PekingU/rtdetr_v2_r50vd",  "label": "r50",  "params_m": None, "size_mb": 172.2},
    {"family": "RT-DETRv2", "file": "PekingU/rtdetr_v2_r101vd", "label": "r101", "params_m": None, "size_mb": 307.3},
    # v1 — 기존 자료·비교 기준이 이쪽인 경우가 있다
    {"family": "RT-DETR",   "file": "PekingU/rtdetr_r18vd",     "label": "r18",  "params_m": None, "size_mb": 80.9},
    {"family": "RT-DETR",   "file": "PekingU/rtdetr_r50vd",     "label": "r50",  "params_m": None, "size_mb": 172.2},
    {"family": "RT-DETR",   "file": "PekingU/rtdetr_r101vd",    "label": "r101", "params_m": None, "size_mb": 307.3},
]

DEFAULT_DETECTOR = "PekingU/rtdetr_v2_r18vd"

_STANDARD_FILES = {m["file"] for m in _DETECTOR_CATALOG}


def _in_hf_cache(model_id: str) -> bool:
    """이미 받아둔 모델인가. HF 는 `models--<org>--<name>` 으로 캐시한다.

    ⚠ 예전에는 홈·저장소 루트에서 `.pt` 파일을 찾았다. 이제 가중치는 HF 캐시로
    가므로 그 자리를 봐야 한다 — 안 고치면 전부 "안 받음"으로 떠서 매번 다시
    받는 줄 알게 된다.
    """
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    return (hub / f"models--{model_id.replace('/', '--')}").is_dir()


def _custom_models() -> list[dict]:
    """학습된 커스텀 가중치.

    ⚠ **디렉토리다, 파일이 아니다.** HF 형식은 config+safetensors+전처리기가 한
    디렉토리에 들어간다. `.pt` 단일 파일은 ultralytics 형식이라 더 이상 안 쓴다.

    ⚠ **`piper_meta.json` 이 있는 것만 센다.** 학습이 중간에 죽으면 최고 에폭까지
    저장된 반쪽 디렉토리가 남는데, 그것도 모델처럼 보여서 목록에 뜨면 사용자가
    고르고 나서야 이상하다는 걸 안다. 메타는 **완료 시점에만** 쓰이므로 그 존재가
    곧 "끝까지 돈 학습"의 표시다.
    """
    d = settings.yolo_models_dir
    if not d.is_dir():
        return []
    out = []
    for p in sorted(x for x in d.iterdir() if x.is_dir()):
        meta_f = p / "piper_meta.json"
        if not meta_f.is_file():
            continue
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        entry = {
            "family": "커스텀",
            "file": p.name,
            "label": time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime)),
            "size_mb": round(size / 1e6, 1),
            "downloaded": True,
        }
        try:
            meta = json.loads(meta_f.read_text())
            entry.update({
                "map50": meta.get("map50"),
                "classes_n": len(meta.get("classes", [])) or None,
                "trained_on": meta.get("dataset"),
            })
        except ValueError:
            pass
        out.append(entry)
    return out


def _resolve_model(name: str) -> str:
    """모델 이름 → 데몬에 넘길 값. **클라이언트가 보낸 값을 경로로 쓰지 않는다.**

    ⚠ 예전에는 "`/` 가 들어 있으면 거절" 이었다. 그런데 HF 모델 id 는
    `PekingU/rtdetr_v2_r18vd` 처럼 **`/` 를 포함한다** — 그대로 두면 카탈로그의
    모든 모델이 거절된다. 그래서 **화이트리스트**로 바꾼다: 카탈로그에 있거나
    커스텀 디렉토리에 있는 것만 통과한다. 막는 쪽이 아니라 허용하는 쪽을 적는
    편이 안전하다 — 새 우회 문자를 뒤늦게 알아채는 일이 없다.
    """
    if name in _STANDARD_FILES:
        return name
    p = settings.yolo_models_dir / Path(name).name
    if p.is_dir() and (p / "piper_meta.json").is_file():
        return str(p)
    raise HTTPException(400, f"모르는 모델입니다: {name}")


@router.get("/models")
async def list_yolo_models():
    """표준 카탈로그 + 업로드된 커스텀 가중치 (시작 UI 의 선택지)."""
    return {
        "models": [
            {**m, "downloaded": _in_hf_cache(m["file"])}
            for m in _DETECTOR_CATALOG
        ] + _custom_models()
    }


@router.delete("/models/{name}")
async def delete_yolo_model(name: str):
    """커스텀 가중치 삭제. 표준 카탈로그는 대상이 아니다 (어차피 여기 없다)."""
    # ⚠ 이제 디렉토리다. `Path(name).name` 으로 경로 성분을 먼저 자른다 —
    #   그게 없으면 `../..` 같은 이름으로 남의 디렉토리를 지울 수 있다.
    p = settings.yolo_models_dir / Path(name).name
    if not p.is_dir():
        raise HTTPException(404, "그런 커스텀 모델이 없습니다")
    shutil.rmtree(p)
    return {"deleted": p.name}


@router.get("/segments")
async def list_camera_segments():
    """살아 있는 카메라 세그먼트 — yolod 가 구독할 수 있는 것들."""
    from piper_shm import list_segments

    return {"segments": list_segments()}


@router.get("/segments/{name}/snapshot")
async def segment_snapshot(name: str):
    """세그먼트의 최신 프레임 JPEG — 시작 UI 가 "이 카메라가 뭘 보나"를 보여준다.

    yolod 를 켜기 전이라 어노테이트 프리뷰(버스)가 없을 때 쓴다.
    YOLO 학습 캡처(yolo_train 라우터)와 같은 읽기를 쓴다.
    """
    from app.services.shm_snapshot import segment_jpeg

    data = segment_jpeg(name, quality=70)
    if data is None:
        raise HTTPException(404, "세그먼트 또는 프레임 없음")
    return Response(content=data, media_type="image/jpeg")


class StartRequest(BaseModel):
    cams: dict[str, str]          # alias → 세그먼트 cam_id
    model: str = "PekingU/rtdetr_v2_r18vd"
    fps: float = 5.0
    conf: float = 0.25
    # 추론 입력 크기(긴 변). ultralytics 가 32 배수로 맞춘다 — 상하한만 막는다
    imgsz: int = Field(default=640, ge=160, le=1920)


@router.post("/start")
async def start_yolod(body: StartRequest):
    if not body.cams:
        raise HTTPException(400, "구독할 카메라가 없습니다")
    args = [settings.grpc_python, "-u", str(_YOLOD_SCRIPT)]
    for alias, cam_id in body.cams.items():
        args += ["--cam", f"{alias}={cam_id}"]
    args += ["--model", _resolve_model(body.model),
             "--fps", str(body.fps), "--conf", str(body.conf),
             "--imgsz", str(body.imgsz)]
    await _yolod_pm.start(args)
    return {"status": "started", "cams": body.cams}


@router.post("/stop")
async def stop_yolod():
    await _yolod_pm.stop()
    return {"status": "stopped"}


@router.get("/status")
async def yolod_status():
    from piper_bus.client import Bus

    try:
        bus = Bus()
        names = bus.detection_names()
        meta = bus.get_yolo_meta()
    except Exception:
        names, meta = [], None
    # model: yolod 가 버스에 발행하는 자기소개 (모델·디바이스·클래스 수 등).
    # 프로세스는 도는데 아직 None 이면 모델 로드 중이라는 뜻이다.
    # ⚠ **죽었으면 왜 죽었는지 같이 준다.** 유닛이 `--collect` 로 뜨므로 실패한
    #   종료도 흔적 없이 사라지고, 화면에는 "꺼졌다"만 남는다 — 모델 이름이
    #   틀렸는지 GPU 가 없는지 사용자가 알 길이 없었다. 저널 꼬리가 유일한 단서다.
    state = _yolod_pm.state
    out = {"state": state.value, "pid": _yolod_pm.pid, "cams": names, "model": meta}
    if not _yolod_pm.is_running:
        tail = _yolod_pm.recent_log()
        if tail:
            out["log"] = tail[-20:]
    return out


# ── 검출 조회 ──


@router.get("/detections")
async def get_detections():
    """살아 있는(TTL 안 지난) 검출 전부. `text` 필드가 곧 LLM 프롬프트 재료다."""
    from piper_bus.client import Bus

    bus = Bus()
    return {name: bus.get_detections(name) for name in bus.detection_names()}


@router.get("/preview/{name}")
async def get_annotated_preview(name: str):
    """yolod 의 어노테이트 프리뷰 (버스 `yolo_<name>` 키)."""
    from piper_bus.client import Bus

    data = Bus().get_preview(f"yolo_{name}")
    if data is None:
        raise HTTPException(404, "프리뷰 없음 — yolod 가 돌고 있습니까?")
    return Response(content=data, media_type="image/jpeg")


# ── LLM 판단 ──


@router.get("/judge/defaults")
async def judge_defaults():
    return {
        "rules": DEFAULT_RULES,
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "stats": llm_client.stats,
    }


class JudgeRequest(BaseModel):
    user: str                      # 검출 텍스트 등 가변 입력
    system: str = DEFAULT_RULES
    provider: str | None = None    # 기본: 설정 (openai_compat/Ollama)
    model: str | None = None
    timeout_s: float = 60.0


@router.post("/judge")
async def run_judge(body: JudgeRequest):
    t0 = time.monotonic()
    try:
        slots = await llm_client.judge(
            body.system, body.user, JudgeSlots,
            timeout_s=body.timeout_s, provider=body.provider, model=body.model,
        )
    except LLMJudgeError as e:
        # reason 을 앞에 실어 넘긴다 — 화면이 "폴백을 정하는 호출자" 역할을 한다
        raise HTTPException(502, f"{e.reason}: {e.detail}" if e.detail else e.reason)
    return {
        "slots": slots.model_dump(),
        "ms": round((time.monotonic() - t0) * 1000),
        "provider": body.provider or settings.llm_provider,
        "model": body.model or settings.llm_model,
    }
