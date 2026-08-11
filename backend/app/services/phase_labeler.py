"""페이즈 라벨 분석 배치 + 사이드카 저장.

`piper_phase` 의 라벨러를 데이터셋 전체에 돌리고 결과를 **원본을 건드리지 않고**
사이드카에 저장한다 (feature/01-phase-annotation.md §3.3, §5).

## 원본은 절대 in-place 로 고치지 않는다

라벨은 `meta/phase_labels.json` 에, 최종 결과는 새 데이터셋에.
잘못 구운 라벨로 50 에피소드를 날리는 사고를 원천 차단한다.

## 1~2단계만으로 쓸모가 있다

전체 파이프라인(UI·굽기·추론)을 안 만들어도 **에피소드 품질 검사**가 된다 —
사이클 수가 중앙값과 다른 에피소드를 찾아주면 50개를 다 볼 필요가 없다.
"""

import json
import logging
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from piper_phase import PHASE_NAMES, Params, count_cycles, label_episode, segments

from app.core.hf_layout import resolve_info_json

logger = logging.getLogger(__name__)

LABELS_FILE = "phase_labels.json"
SIGNALS_FILE = "phase_signals.parquet"
SIDECAR_VERSION = 1


def sidecar_paths(ds_path: Path) -> tuple[Path, Path]:
    meta = ds_path / "meta"
    return meta / LABELS_FILE, meta / SIGNALS_FILE


def load_frames(ds_path: Path) -> pd.DataFrame:
    """데이터셋의 모든 data parquet 를 합쳐 읽는다.

    ⚠ chunk 가 여러 파일로 나뉘는 데이터셋이 있다 — 첫 파일만 읽으면
    에피소드 절반이 조용히 빠진다.
    """
    files = sorted((ds_path / "data").rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"data parquet 가 없습니다: {ds_path}")
    return pd.concat([pq.read_table(f).to_pandas() for f in files], ignore_index=True)


def dataset_fps(ds_path: Path, default: float = 15.0) -> float:
    info_path = resolve_info_json(ds_path)
    if not info_path:
        return default
    try:
        return float(json.loads(info_path.read_text()).get("fps", default))
    except Exception:
        return default


def analyze(
    ds_path: Path,
    params: Params | None = None,
    episodes: list[int] | None = None,
) -> dict:
    """전 에피소드 라벨링. 사이드카에 저장하지는 않는다 (호출부가 결정)."""
    p = params or Params(fps=dataset_fps(ds_path))
    df = load_frames(ds_path)
    targets = episodes if episodes is not None else sorted(df.episode_index.unique())

    out: dict[int, dict] = {}
    signal_rows = []
    for ep in targets:
        e = df[df.episode_index == ep]
        if e.empty:
            continue
        state = np.stack(e["observation.state"].to_numpy())
        action = np.stack(e["action"].to_numpy())
        phases, sig = label_episode(state, action, p)

        out[int(ep)] = {
            "segments": [[s, t, v] for s, t, v in segments(phases)],
            "cycles": count_cycles(phases),
            "frames": len(phases),
            "reviewed": False,
            "edited_by": "auto",
            "note": "",
        }
        signal_rows.append(pd.DataFrame({
            "episode_index": np.int32(ep),
            "frame_index": np.arange(len(phases), dtype=np.int32),
            "speed": sig.speed.astype(np.float32),
            "gripper_gap": sig.gripper_gap.astype(np.float32),
            "phase": phases.astype(np.int8),
        }))

    return {
        "version": SIDECAR_VERSION,
        "phases": list(PHASE_NAMES),
        "params": asdict(p),
        "episodes": {str(k): v for k, v in out.items()},
        "_signals": pd.concat(signal_rows, ignore_index=True) if signal_rows else None,
    }


def flag_outliers(result: dict) -> list[dict]:
    """이상 에피소드 — **이 툴의 핵심 가치다.**

    50개를 다 볼 필요 없이 이상한 것만 고치면 된다.
    """
    eps = result["episodes"]
    if not eps:
        return []
    cycles = [v["cycles"] for v in eps.values()]
    median = int(np.median(cycles))
    flags = []
    for name, v in eps.items():
        reasons = []
        if v["cycles"] != median:
            reasons.append(f"사이클 {v['cycles']} (중앙값 {median})")
        if v["cycles"] == 0:
            reasons.append("집기 미검출")
        if v["segments"] and v["segments"][-1][2] != PHASE_NAMES.index("DONE"):
            reasons.append("DONE 없음")
        if len(v["segments"]) < 4:
            reasons.append(f"구간 {len(v['segments'])}개")
        if reasons:
            flags.append({"episode": int(name), "cycles": v["cycles"], "reasons": reasons})
    return sorted(flags, key=lambda f: -len(f["reasons"]))


def save(ds_path: Path, result: dict) -> tuple[Path, Path | None]:
    """사이드카 저장. **원본 parquet 와 info.json 은 건드리지 않는다.**"""
    labels_path, signals_path = sidecar_paths(ds_path)
    labels_path.parent.mkdir(parents=True, exist_ok=True)

    signals = result.pop("_signals", None)
    # 기존 사이드카가 있으면 검토 상태(reviewed/note)를 보존한다 — 재분석에 사람 작업이 날아가면 안 된다
    existing = load(ds_path)
    if existing:
        for name, prev in existing.get("episodes", {}).items():
            cur = result["episodes"].get(name)
            if cur and prev.get("reviewed"):
                cur["reviewed"] = True
                cur["edited_by"] = prev.get("edited_by", "auto")
                cur["note"] = prev.get("note", "")

    labels_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    if signals is not None and len(signals):
        signals.to_parquet(signals_path, index=False)
        logger.info("사이드카 저장: %s (+ 신호 %d행)", labels_path, len(signals))
        return labels_path, signals_path
    logger.info("사이드카 저장: %s", labels_path)
    return labels_path, None


def load(ds_path: Path) -> dict | None:
    labels_path, _ = sidecar_paths(ds_path)
    if not labels_path.exists():
        return None
    try:
        return json.loads(labels_path.read_text())
    except Exception:
        logger.warning("사이드카 파싱 실패: %s", labels_path)
        return None


def summary(result: dict) -> dict:
    eps = result["episodes"]
    cycles = [v["cycles"] for v in eps.values()]
    return {
        "episodes": len(eps),
        "cycle_distribution": {
            str(int(c)): int(n) for c, n in zip(*np.unique(cycles, return_counts=True))
        } if cycles else {},
        "median_cycles": int(np.median(cycles)) if cycles else 0,
        "outliers": flag_outliers(result),
    }
