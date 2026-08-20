"""YOLO 학습 데이터셋 — 파일시스템이 곧 정본 (feature/yolo-training.md).

ultralytics 형식(images/ + labels/*.txt + classes)을 그대로 쓴다 — 자체
스키마→YOLO 변환 계층을 만들면 그게 새 중복원이 된다. 이 모듈은 그 형식
위의 얇은 손잡이일 뿐이다: 경로 검증, 출처 기록(sources.jsonl), 카운트.

LeRobot 데이터셋(dataset_scanner)과는 완전히 다른 물건이다 — 이름이 비슷해도
섞지 않는다.
"""

import json
import re
import shutil
import time
import uuid
from pathlib import Path

from app.core.config import settings

# 데이터셋 이름·이미지 파일명 규칙. 경로 문자를 원천 차단한다 —
# 이름이 URL 과 파일시스템을 오가므로 슬러그 밖은 전부 거절.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_IMAGE_RE = re.compile(r"^[a-f0-9]{32}\.jpg$")


class YoloDatasetError(ValueError):
    """호출부(라우터)가 HTTP 로 옮기는 검증 실패. status 가 그 지위다."""

    def __init__(self, msg: str, status: int = 400) -> None:
        super().__init__(msg)
        self.status = status


def _root() -> Path:
    return settings.yolo_datasets_dir


def dataset_path(name: str) -> Path:
    if not _NAME_RE.match(name):
        raise YoloDatasetError(f"데이터셋 이름 형식이 아닙니다: {name!r} (영숫자/_/-)")
    return _root() / name


def _existing(name: str) -> Path:
    p = dataset_path(name)
    if not (p / "classes.json").is_file():
        raise YoloDatasetError(f"데이터셋이 없습니다: {name}", status=404)
    return p


# ── 데이터셋 CRUD ──


def create_dataset(name: str, classes: list[str]) -> dict:
    p = dataset_path(name)
    if p.exists():
        raise YoloDatasetError(f"이미 있습니다: {name}")
    classes = [c.strip() for c in classes if c.strip()]
    if not classes:
        raise YoloDatasetError("클래스가 최소 1개 필요합니다")
    if len(set(classes)) != len(classes):
        raise YoloDatasetError("클래스 이름이 중복됩니다")
    (p / "images").mkdir(parents=True)
    (p / "labels").mkdir()
    (p / "classes.json").write_text(json.dumps(classes, ensure_ascii=False))
    return summarize(name)


def add_classes(name: str, new: list[str]) -> list[str]:
    """클래스는 **추가만** 허용 — 삭제·순서 변경은 기존 라벨 txt 의 id 를
    전부 어긋나게 한다 (에피소드 sidecar 어긋남과 같은 버그 클래스)."""
    p = _existing(name)
    classes = read_classes(name)
    for c in (c.strip() for c in new):
        if c and c not in classes:
            classes.append(c)
    (p / "classes.json").write_text(json.dumps(classes, ensure_ascii=False))
    return classes


def read_classes(name: str) -> list[str]:
    return json.loads((_existing(name) / "classes.json").read_text())


def list_datasets() -> list[dict]:
    root = _root()
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if (p / "classes.json").is_file():
            out.append(summarize(p.name))
    return out


def summarize(name: str) -> dict:
    p = _existing(name)
    images = sorted(f.name for f in (p / "images").glob("*.jpg"))
    labeled = sum(1 for f in images if (p / "labels" / f).with_suffix(".txt").exists())
    return {
        "name": name,
        "classes": read_classes(name),
        "images": len(images),
        "labeled": labeled,
    }


def delete_dataset(name: str) -> None:
    shutil.rmtree(_existing(name))


# ── 이미지 ──


def add_image(name: str, data: bytes, source: dict) -> str:
    """JPEG 바이트를 저장하고 출처를 sources.jsonl 에 적는다. 파일명을 돌려준다."""
    if not data.startswith(b"\xff\xd8"):
        raise YoloDatasetError("JPEG 가 아닙니다")
    p = _existing(name)
    fname = f"{uuid.uuid4().hex}.jpg"
    (p / "images" / fname).write_bytes(data)
    with (p / "sources.jsonl").open("a") as f:
        f.write(json.dumps({"file": fname, "at": round(time.time(), 3), **source},
                           ensure_ascii=False) + "\n")
    return fname


def read_sources(name: str) -> dict[str, dict]:
    """{파일명: 출처}. 없는 파일(삭제됨)의 행은 그냥 남는다 — append 전용."""
    p = _existing(name) / "sources.jsonl"
    out: dict[str, dict] = {}
    if not p.is_file():
        return out
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
            out[rec["file"]] = rec
        except (ValueError, KeyError):
            continue
    return out


def list_images(name: str) -> list[dict]:
    p = _existing(name)
    sources = read_sources(name)
    out = []
    for f in sorted((p / "images").glob("*.jpg")):
        out.append({
            "file": f.name,
            "labeled": (p / "labels" / f.name).with_suffix(".txt").exists(),
            "source": sources.get(f.name),
        })
    return out


def image_path(name: str, fname: str) -> Path:
    if not _IMAGE_RE.match(fname):
        raise YoloDatasetError(f"이미지 파일명 형식이 아닙니다: {fname!r}")
    p = _existing(name) / "images" / fname
    if not p.is_file():
        raise YoloDatasetError(f"이미지가 없습니다: {fname}", status=404)
    return p


def delete_image(name: str, fname: str) -> None:
    p = image_path(name, fname)
    p.unlink()
    (p.parent.parent / "labels" / fname).with_suffix(".txt").unlink(missing_ok=True)


# ── 라벨 (YOLO txt ↔ 박스 JSON) ──
#
# txt 가 정본이다. JSON 은 화면 왕복용 표현일 뿐 저장하지 않는다.
# 박스: {cls: int, cx, cy, w, h} — 전부 0~1 정규화 (YOLO 형식 그대로).


def read_label(name: str, fname: str) -> list[dict] | None:
    """None = 미라벨, [] = 라벨됨(박스 0개 — 배경 샘플로 유효)."""
    image_path(name, fname)  # 존재·이름 검증
    txt = (_existing(name) / "labels" / fname).with_suffix(".txt")
    if not txt.is_file():
        return None
    boxes = []
    for line in txt.read_text().splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        boxes.append({
            "cls": int(parts[0]),
            "cx": float(parts[1]), "cy": float(parts[2]),
            "w": float(parts[3]), "h": float(parts[4]),
        })
    return boxes


def write_label(name: str, fname: str, boxes: list[dict]) -> None:
    image_path(name, fname)
    n_classes = len(read_classes(name))
    lines = []
    for b in boxes:
        cls = int(b["cls"])
        if not 0 <= cls < n_classes:
            raise YoloDatasetError(f"클래스 id 범위 밖: {cls}")
        vals = [float(b[k]) for k in ("cx", "cy", "w", "h")]
        if not all(0.0 <= v <= 1.0 for v in vals):
            raise YoloDatasetError("좌표는 0~1 정규화여야 합니다")
        lines.append(f"{cls} " + " ".join(f"{v:.6f}" for v in vals))
    txt = (_existing(name) / "labels" / fname).with_suffix(".txt")
    txt.write_text("\n".join(lines) + ("\n" if lines else ""))


def clear_label(name: str, fname: str) -> None:
    """라벨 파일 삭제 = 미라벨로 되돌림 ([] 저장과 다르다)."""
    image_path(name, fname)
    (_existing(name) / "labels" / fname).with_suffix(".txt").unlink(missing_ok=True)
