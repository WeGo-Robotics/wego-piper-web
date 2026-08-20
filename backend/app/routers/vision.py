"""YOLO 검출 · LLM 판단 테스트 API — 분리수거 파이프라인의 앞 두 칸을 화면에서 굴린다.

yolod 는 정책 서버와 같은 "필요할 때 켜는 유닛"이다 — `make_process` 가 소유자를
정하므로 게이트웨이를 재시작해도 산다. 판단은 [llm_client](../services/llm_client.py)
를 **직접 호출**한다 (자기 HTTP 호출 금지 — episode-orchestrator §2 와 같은 규칙).
"""

import json
import logging
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
_YOLO_CATALOG = [
    # 현행 세대 (2024)
    {"family": "YOLO11",  "file": "yolo11n.pt",  "label": "nano",   "params_m": 2.6,  "size_mb": 5.4},
    {"family": "YOLO11",  "file": "yolo11s.pt",  "label": "small",  "params_m": 9.4,  "size_mb": 18.4},
    {"family": "YOLO11",  "file": "yolo11m.pt",  "label": "medium", "params_m": 20.1, "size_mb": 38.8},
    {"family": "YOLO11",  "file": "yolo11l.pt",  "label": "large",  "params_m": 25.3, "size_mb": 49.0},
    {"family": "YOLO11",  "file": "yolo11x.pt",  "label": "xlarge", "params_m": 56.9, "size_mb": 109.3},
    # 가장 널리 쓰인 세대 (2023) — 기존 자료·비교 기준이 대부분 이쪽이다
    {"family": "YOLOv8",  "file": "yolov8n.pt",  "label": "nano",   "params_m": 3.2,  "size_mb": 6.2},
    {"family": "YOLOv8",  "file": "yolov8s.pt",  "label": "small",  "params_m": 11.2, "size_mb": 21.5},
    {"family": "YOLOv8",  "file": "yolov8m.pt",  "label": "medium", "params_m": 25.9, "size_mb": 49.7},
    {"family": "YOLOv8",  "file": "yolov8l.pt",  "label": "large",  "params_m": 43.7, "size_mb": 83.7},
    {"family": "YOLOv8",  "file": "yolov8x.pt",  "label": "xlarge", "params_m": 68.2, "size_mb": 130.5},
    # v5 아키텍처의 ultralytics 재릴리스(u) — 구형 대비·저사양 확인용
    {"family": "YOLOv5u", "file": "yolov5nu.pt", "label": "nano",   "params_m": 2.6,  "size_mb": 5.3},
    {"family": "YOLOv5u", "file": "yolov5su.pt", "label": "small",  "params_m": 9.1,  "size_mb": 17.7},
    {"family": "YOLOv5u", "file": "yolov5mu.pt", "label": "medium", "params_m": 25.1, "size_mb": 48.2},
]

# 가중치가 떨어져 있을 만한 곳: yolod 유닛의 작업 디렉토리(홈 — systemd-run 이
# WorkingDirectory 를 안 정하므로), 개발 실행이 받아둔 저장소 루트·backend.
_WEIGHT_DIRS = [Path.home(), Path(__file__).resolve().parents[3], Path.cwd()]

_STANDARD_FILES = {m["file"] for m in _YOLO_CATALOG}

# 업로드 상한. 표준 최대(yolo11x, ~110MB)의 몇 배면 커스텀도 넉넉하다 —
# 무제한이면 잘못 올린 파일이 디스크를 채운다.
_UPLOAD_LIMIT_MB = 500


def _custom_models() -> list[dict]:
    """업로드·학습된 커스텀 가중치.

    학습 유닛(yolo_traind)이 남긴 곁 JSON(<stem>.json)이 있으면 지표를 싣는다 —
    드롭다운에서 "어느 데이터셋으로 얼마나 나온 가중치인지"가 보인다.
    없으면(직접 업로드) 수정일만.
    """
    d = settings.yolo_models_dir
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.pt")):
        st = p.stat()
        entry = {
            "family": "커스텀",
            "file": p.name,
            "label": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime)),
            "size_mb": round(st.st_size / 1e6, 1),
            "downloaded": True,
        }
        sidecar = p.with_suffix(".json")
        if sidecar.is_file():
            try:
                meta = json.loads(sidecar.read_text())
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
    """모델 이름 → yolod 에 넘길 값. **클라이언트가 보낸 값을 경로로 쓰지 않는다.**

    커스텀 디렉토리에 있으면 그 절대경로, 아니면 이름 그대로 — ultralytics 가
    표준 에셋 이름이면 자동 다운로드하고 모르는 이름이면 로드에서 실패한다.
    경로 문자가 섞인 이름만 여기서 자른다 (임의 파일 로드 방지).
    """
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "모델 이름에 경로를 쓸 수 없습니다")
    p = settings.yolo_models_dir / name
    return str(p) if p.is_file() else name


@router.get("/models")
async def list_yolo_models():
    """표준 카탈로그 + 업로드된 커스텀 가중치 (시작 UI 의 선택지)."""
    return {
        "models": [
            {**m, "downloaded": any((d / m["file"]).exists() for d in _WEIGHT_DIRS)}
            for m in _YOLO_CATALOG
        ] + _custom_models()
    }


@router.put("/models/{name}")
async def upload_yolo_model(name: str, request: Request):
    """커스텀 가중치 업로드 — raw 바디 (.pt). multipart 의존성 없이 스트리밍으로 받는다."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "파일명에 경로를 쓸 수 없습니다")
    if not name.endswith(".pt"):
        raise HTTPException(400, ".pt 파일만 받습니다")
    if name in _STANDARD_FILES:
        raise HTTPException(400, "표준 모델과 같은 이름입니다 — 파일명을 바꿔서 올리세요")

    settings.yolo_models_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.yolo_models_dir / name
    tmp = dest.with_suffix(".pt.part")  # 반쯤 올라간 파일이 목록에 뜨면 안 된다
    size = 0
    try:
        with tmp.open("wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > _UPLOAD_LIMIT_MB * 1_000_000:
                    raise HTTPException(413, f"{_UPLOAD_LIMIT_MB}MB 를 넘습니다")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "빈 파일입니다")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"file": name, "size_mb": round(size / 1e6, 1)}


@router.delete("/models/{name}")
async def delete_yolo_model(name: str):
    """커스텀 가중치 삭제. 표준 카탈로그는 대상이 아니다 (어차피 여기 없다)."""
    p = settings.yolo_models_dir / Path(name).name
    if not p.is_file():
        raise HTTPException(404, "그런 커스텀 모델이 없습니다")
    p.unlink()
    p.with_suffix(".json").unlink(missing_ok=True)  # 학습 곁 JSON 도 같이
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
    model: str = "yolo11n.pt"
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
    return {"state": _yolod_pm.state.value, "pid": _yolod_pm.pid, "cams": names,
            "model": meta}


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
