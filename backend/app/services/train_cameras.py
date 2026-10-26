"""체크포인트가 어떤 카메라로 학습됐나 — 해상도·기종 (feature/camera-resolution.md).

정책은 학습 때 본 화면 구도를 외운다. 같은 카메라라도 해상도가 바뀌면 비율·화각이 바뀌어 물체가
다른 자리·크기로 들어가고, 정책은 에러 없이 **엇나간 자리**로 손을 뻗는다 (2026-10-26: 640×480 으로
학습한 SmolVLA 가 데몬 재시작으로 기본값이 된 1280×720·848×480 영상을 받아 사과를 못 집었다).

모델 config 의 입력 크기는 근거가 아니다 — SmolVLA 처럼 베이스 모델에서 시작하면 베이스의 값(256×256)이
그대로 남는다. 그래서 학습 데이터셋을 본다. 판정 순서:

1. 체크포인트의 표식 `piper_train_cameras.json` — 추론을 시작할 때 남긴다. 한 번 남으면 학습 데이터셋이
   사라져도 남는다 (`action_space` 의 표식과 같은 규칙).
2. `train_config.json` 의 데이터셋 → `meta/info.json`(크기 — 모든 LeRobot 데이터셋에 있다)
   + `meta/piper_cameras.json`(fps·기종·캡처 크기 — 우리가 녹화한 것만).
3. 둘 다 없으면 모른다 — 대조하지 않는다.

카메라 이름은 **데이터셋 쪽**(top/hand)이다 — 추론 매핑의 키와 같은 쪽. `rename_map` 이 붙인 모델 안
이름(camera1)이 아니다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CKPT_MARKER = "piper_train_cameras.json"
SIDECAR = "piper_cameras.json"                 # = camera_config.SIDECAR_NAME
_IMAGE_PREFIX = "observation.images."
# 같은 비율로 보는 차이. D405 848×480(1.767)과 1280×720(1.778)은 같은 16:9 다 — 화소를 맞추느라
# 생긴 차이를 "비율이 다르다"로 막으면 실행할 수 있는 것까지 막는다. 4:3 과 16:9 는 25% 차이다.
ASPECT_TOLERANCE = 0.01

OK, SIZE, ASPECT, UNKNOWN, NO_LIVE = "ok", "size", "aspect", "unknown", "no_live"


def _size_of(feat: dict) -> tuple[int, int] | None:
    """이미지 피처의 `(w, h)`. LeRobot 은 `[H, W, C]` 에 `names` 를 붙인다 — 이름이 있으면 그걸 따른다."""
    shape = feat.get("shape") or []
    names = list(feat.get("names") or [])
    if "height" in names and "width" in names and len(shape) == len(names):
        return int(shape[names.index("width")]), int(shape[names.index("height")])
    if len(shape) >= 2:
        return int(shape[1]), int(shape[0])
    return None


def from_dataset(ds_path: Path) -> dict[str, dict]:
    """데이터셋의 카메라별 `{width, height, fps, model?, capture_width?, capture_height?, id?, stream?}`."""
    try:
        info = json.loads((ds_path / "meta" / "info.json").read_text())
    except (OSError, ValueError):
        return {}
    cams: dict[str, dict] = {}
    for key, feat in (info.get("features") or {}).items():
        if not key.startswith(_IMAGE_PREFIX) or not isinstance(feat, dict):
            continue
        size = _size_of(feat)
        if size is None:
            continue
        entry: dict = {"width": size[0], "height": size[1]}
        fps = (feat.get("info") or {}).get("video.fps") or info.get("fps")
        if fps:
            entry["fps"] = int(fps)
        cams[key[len(_IMAGE_PREFIX):]] = entry
    try:
        side = json.loads((ds_path / "meta" / SIDECAR).read_text()).get("cameras") or {}
    except (OSError, ValueError):
        side = {}
    for name, extra in side.items():
        if name not in cams or not isinstance(extra, dict):
            continue
        # 크기는 비디오(info.json)가 진실이다. 사이드카는 비디오에 없는 것만 보탠다 — fps 는 카메라가
        # 낸 값이 녹화 루프 fps 와 다를 수 있어 사이드카 쪽이 맞다
        for k in ("fps", "capture_width", "capture_height", "model", "id", "stream"):
            if extra.get(k):
                cams[name][k] = extra[k]
    _fill_models(cams)
    return cams


def _fill_models(cams: dict[str, dict]) -> None:
    """기종을 남기기 전에 녹화한 데이터셋 — RealSense 는 id 가 시리얼이라 지금 등록된 같은 장치에서 읽는다.

    v4l2 의 `/dev/videoN` 은 재열거로 다른 장치를 가리킬 수 있어 짐작하지 않는다.
    """
    missing = [c for c in cams.values() if not c.get("model") and str(c.get("id", "")).startswith("rs:")]
    if not missing:
        return
    from app.services.camera_manager import camera_manager

    for c in missing:
        cam = camera_manager.cameras.get(c["id"])
        if cam is not None and cam.name:
            c["model"] = cam.name


def resolve(ckpt: Path) -> dict:
    """`{"cameras": {이름: {...}}, "dataset", "source"}`. source 는 marker / dataset / none."""
    try:
        marked = json.loads((ckpt / CKPT_MARKER).read_text())
        return {"cameras": dict(marked.get("cameras") or {}), "dataset": marked.get("dataset"),
                "source": "marker"}
    except (OSError, ValueError):
        pass
    from app.services.action_space import train_dataset

    repo_id, ds_path = train_dataset(ckpt)
    cams = from_dataset(ds_path) if ds_path is not None else {}
    if cams:
        return {"cameras": cams, "dataset": repo_id, "source": "dataset"}
    return {"cameras": {}, "dataset": repo_id, "source": "none"}


def remember(ckpt: Path, info: dict) -> bool:
    """데이터셋에서 읽은 학습 카메라를 체크포인트에 남긴다 — 이후로는 데이터셋이 없어도 안다.

    추론 **시작**에서만 부른다. HF 캐시 스냅샷에는 쓰지 않는다 — 공유 캐시를 오염시킨다
    (`action_space.remember` 와 같은 이유). 남겼으면 True.
    """
    if info.get("source") != "dataset" or not info.get("cameras"):
        return False
    if "snapshots" in ckpt.parts:
        return False
    try:
        (ckpt / CKPT_MARKER).write_text(json.dumps(
            {"dataset": info.get("dataset"), "cameras": info["cameras"]},
            indent=2, ensure_ascii=False) + "\n")
        return True
    except OSError as e:
        logger.warning("학습 카메라 표식을 남기지 못했다 (%s): %s", ckpt, e)
        return False


# ── 지금 카메라 ──

def live_cameras(camera_mapping: dict[str, str]) -> dict[str, dict]:
    """매핑된 카메라가 **지금 발행 중인** `{width, height, model}`. 요청값이 아니라 세그먼트 크기다."""
    from app.services.camera_config import normalize_mapping
    from app.services.camera_manager import camera_manager, live_size

    out: dict[str, dict] = {}
    for name, cam_id in normalize_mapping(camera_mapping).items():
        cam = camera_manager.cameras.get(cam_id) if cam_id else None
        if cam is None:
            continue
        row: dict = {"model": cam.name}
        size = live_size(cam)
        if size:
            row["width"], row["height"] = size
        out[name] = row
    return out


def live_from_cameras_arg(cameras_json: str) -> dict[str, dict]:
    """손으로 고친 명령의 `--cameras` 에서 — 세그먼트 헤더를 직접 읽는다 (shm 만)."""
    try:
        cfg = json.loads(cameras_json)
    except ValueError:
        return {}
    from piper_shm import Subscriber

    out: dict[str, dict] = {}
    for name, c in (cfg or {}).items():
        seg = (c or {}).get("segment") if isinstance(c, dict) else None
        if not seg:
            continue
        try:
            sub = Subscriber(seg, observer=True)
            try:
                h, w, _ = sub.shape
            finally:
                sub.close()
        except Exception:
            continue
        out[name] = {"width": w, "height": h}
    return out


# ── 대조 ──

def _ratio(c: dict) -> float:
    return c["width"] / c["height"]


def compare(train: dict[str, dict], live: dict[str, dict]) -> dict[str, dict]:
    """카메라별 `{"train", "live", "status", "model_differs"}`.

    status — ok: 크기가 같다 (fps 는 보지 않는다 — 녹화·제어 루프가 정한다) ·
    size: 비율은 같고 크기만 다르다 (경고, 실행된다) · aspect: 비율이 다르다 (시작을 막는다) ·
    unknown: 학습 쪽을 모른다 · no_live: 지금 크기를 못 읽었다.
    """
    rows: dict[str, dict] = {}
    for name in sorted(set(train) | set(live)):
        t, lv = train.get(name), live.get(name)
        if not t or not t.get("width") or not t.get("height"):
            status = UNKNOWN
        elif not lv or not lv.get("width") or not lv.get("height"):
            status = NO_LIVE
        elif (t["width"], t["height"]) == (lv["width"], lv["height"]):
            status = OK
        elif abs(_ratio(lv) - _ratio(t)) <= ASPECT_TOLERANCE * _ratio(t):
            status = SIZE
        else:
            status = ASPECT
        tm, lm = (t or {}).get("model"), (lv or {}).get("model")
        rows[name] = {"train": t, "live": lv, "status": status,
                      "model_differs": bool(tm and lm and tm != lm)}
    return rows


def messages(rows: dict[str, dict]) -> tuple[list[str], list[str]]:
    """`(errors, warnings)` — errors 가 있으면 추론을 시작하지 않는다."""
    errors: list[str] = []
    warnings: list[str] = []
    for name, r in rows.items():
        t, lv = r["train"], r["live"]
        if r["status"] == ASPECT:
            errors.append(
                f"카메라 '{name}' 화면 비율이 학습과 다릅니다 — 학습 {t['width']}x{t['height']}, "
                f"지금 {lv['width']}x{lv['height']}. 같은 장면이 다른 자리로 들어가 정책이 엇나갑니다. "
                f"학습 해상도로 맞추세요")
        elif r["status"] == SIZE:
            warnings.append(
                f"카메라 '{name}' 해상도가 학습과 다릅니다 — 학습 {t['width']}x{t['height']}, "
                f"지금 {lv['width']}x{lv['height']} (비율이 같아 실행은 됩니다)")
        if r["model_differs"]:
            warnings.append(f"카메라 '{name}' 기종이 학습 때와 다릅니다 — 학습 {t['model']}, 지금 {lv['model']}")
    return errors, warnings


def match_plan(train_cam: dict) -> tuple[tuple[int, int, int], list[int] | None, list[int] | None]:
    """학습 때와 같게 열려면 — `(요청 w,h,fps, 캡처, 출력)`.

    학습 때 넓게 받아(캡처) 줄였다면(출력) 그대로 재현한다 — 같은 640×480 이라도 센서를 잘라 연 것과
    화각이 다르다. 캡처를 모르면(옛 데이터셋) 그 크기로 바로 연다 — 그때는 그게 기본이었다.
    """
    w, h = int(train_cam["width"]), int(train_cam["height"])
    fps = int(train_cam.get("fps") or 30)
    cw, ch = train_cam.get("capture_width"), train_cam.get("capture_height")
    if cw and ch and (int(cw), int(ch)) != (w, h):
        return (w, h, fps), [int(cw), int(ch)], [w, h]
    return (w, h, fps), None, None
