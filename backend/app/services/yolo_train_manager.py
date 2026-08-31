"""YOLO 커스텀 학습 상태 — 유닛 소유자 + 파일 기반 진행 조회.

feature/yolo-training.md 3단계. 학습 자체는 `piper-yolotrain` 유닛
(daemons/yolo_traind.py)이 하고, 여기는 게이트웨이 쪽 손잡이다:

- `yolo_train_pm`: 유닛 소유자. exclusivity 의 상태 제공자이기도 하다.
- 진행 상태는 **파일이 정본**이다 — 스크립트가 `_training.json` 을 쓰고
  results.csv 를 남기므로, 게이트웨이가 재시작해도 다시 읽으면 그만이다
  (학습 job 레지스트리와 같은 설계 이유).
"""

import csv
import json
import logging
from pathlib import Path

from app.core.config import settings
from app.services.systemd_process import make_process

logger = logging.getLogger(__name__)

yolo_train_pm = make_process("piper-yolotrain")


def status_path() -> Path:
    """스크립트가 쓰는 상태 파일 — 데이터셋 루트에 하나 (동시 학습은 없다)."""
    return settings.yolo_datasets_dir / "_training.json"


def read_status() -> dict | None:
    p = status_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def read_progress(dataset: str, run_name: str) -> list[dict]:
    """results.csv → [{epoch, box_loss, map50, map50_95}].

    파일이 아직 없으면(1에폭 전) 빈 목록.

    ⚠ 예전 열 이름은 **ultralytics 형식**('train/box_loss', 'metrics/mAP50(B)')
    이었다. 학습을 직접 하게 되면서 우리가 쓰는 이름('train_loss', 'map50')으로
    바뀌었다 — 파서를 안 고치면 진행 그래프가 **아무 말 없이 빈 채로** 뜬다.
    `except KeyError: continue` 가 모든 행을 조용히 버리기 때문이다.
    """
    csv_path = settings.yolo_datasets_dir / dataset / "runs" / run_name / "results.csv"
    if not csv_path.is_file():
        return []
    out = []
    try:
        with csv_path.open() as f:
            for row in csv.DictReader(f):
                row = {k.strip(): v for k, v in row.items() if k}
                try:
                    out.append({
                        "epoch": int(float(row["epoch"])),
                        "box_loss": round(float(row["train_loss"]), 4),
                        "map50": round(float(row["map50"]), 4),
                        "map50_95": round(float(row["map50_95"]), 4),
                    })
                except (KeyError, ValueError):
                    continue
    except OSError as e:
        logger.warning("results.csv 읽기 실패: %s", e)
    return out
