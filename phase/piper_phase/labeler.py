"""페이즈 라벨 분석 배치 + 사이드카 저장.

`piper_phase.fsm` 의 라벨러를 데이터셋 전체에 돌리고 결과를 **원본을 건드리지 않고**
사이드카에 저장한다 (feature/01-phase-annotation.md §3.3, §5).

## 왜 백엔드가 아니라 패키지에 있나

에피소드 에디터/뷰어(§4)와 **별개로, 분류기는 단독 실행 가능해야 한다** —
게이트웨이 없는 머신(학습 머신·컨테이너)에서도 데이터셋 품질 검사가 돼야 한다.
`python -m piper_phase <dataset_path>` 가 그 진입점이고, 백엔드 API 는
같은 코드를 import 해서 쓴다. 원래 `backend/app/services/phase_labeler.py` 였다.

pandas/pyarrow 가 필요하다: `pip install -e "phase/[labeler]"`.
(인과 코어 `fsm.py` 는 numpy 만 쓴다 — 온라인 추정기 쪽에 무게를 더하지 않는다.)

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

from piper_phase.fsm import PHASE_NAMES, Params, count_cycles, label_episode, segments

logger = logging.getLogger(__name__)

LABELS_FILE = "phase_labels.json"
SIGNALS_FILE = "phase_signals.parquet"
# ⚠ 파생 신호의 **계산이 바뀌면 올린다.** 예전에는 쓰기만 하고 아무도 안 읽는
# 죽은 필드였다 — 그래서 말단 속도 미분 방식을 바꿨을 때, 이미 만들어둔
# 사이드카가 옛 값을 그대로 내보내고 아무도 그걸 몰랐다.
#   1 → 2: 말단 속도를 중심차분으로
#   2 → 3: 말단 속도를 Savitzky–Golay 도함수로 (kinematics._SG_WINDOW)
SIDECAR_VERSION = 3


def tip_speed(state, fps: float):
    """말단 속도(m/s). URDF 를 못 읽으면 `None` — 페이즈 분석은 계속돼야 한다."""
    from . import kinematics as K

    if not K.available():
        return None
    try:
        return K.endpoint_speed(state, fps)
    except Exception as exc:      # 축 수가 안 맞는 팔 등
        logger.warning("말단 속도 계산 실패: %s", exc)
        return None


def sidecar_paths(ds_path: Path) -> tuple[Path, Path]:
    meta = ds_path / "meta"
    return meta / LABELS_FILE, meta / SIGNALS_FILE


def _info_json(ds_path: Path) -> Path | None:
    """`meta/info.json`(v2+) → 평평한 `info.json`(구버전) 폴백.

    백엔드 `app.core.hf_layout.resolve_info_json` 과 같은 규칙이다 — 저쪽은
    게이트웨이 전용(스캔·삭제 레이아웃)이라 여기서 import 할 수 없어 규칙만 공유한다.
    바꿀 일이 생기면 **둘 다** 바꾼다.
    """
    for candidate in (ds_path / "meta" / "info.json", ds_path / "info.json"):
        if candidate.exists():
            return candidate
    return None


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
    info_path = _info_json(ds_path)
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
        row = {
            "episode_index": np.int32(ep),
            "frame_index": np.arange(len(phases), dtype=np.int32),
            "speed": sig.speed.astype(np.float32),
            "gripper_gap": sig.gripper_gap.astype(np.float32),
            "phase": phases.astype(np.int8),
        }
        # 말단 속도(m/s). ⚠ `speed` 와 **다른 물건**이다 — 저쪽은 관절 공간이라
        # 어깨 1도와 손목 1도를 같게 센다. URDF 서브모듈이 없으면 그냥 빠진다.
        tip = tip_speed(state, p.fps)
        if tip is not None:
            row["tip_speed"] = tip.astype(np.float32)
        # 시작 자세로부터의 거리 — 복귀(PARKING) 판정의 근거를 화면에서도 보게 한다
        if sig.home_dist is not None:
            row["home_dist"] = sig.home_dist.astype(np.float32)
        signal_rows.append(pd.DataFrame(row))

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
    existing = load(ds_path)
    if existing:
        merged = dict(existing.get("episodes", {}))
        for name, cur in result["episodes"].items():
            prev = merged.get(name)
            # 검토 상태(reviewed/note)를 보존한다 — 재분석에 사람 작업이 날아가면 안 된다
            if prev and prev.get("reviewed"):
                cur["reviewed"] = True
                cur["edited_by"] = prev.get("edited_by", "auto")
                cur["note"] = prev.get("note", "")
            merged[name] = cur
        # ⚠ **부분 분석이 전체를 덮어쓰면 안 된다.** 문서가 의도한 "선택 에피소드만
        # 재분석해 파라미터 미리보기"(§3.5)가 나머지 45개를 날리는 사고가 된다.
        result["episodes"] = merged

    labels_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    if signals is not None and len(signals):
        # 신호도 같은 이유로 병합한다 (분석한 에피소드만 갱신)
        if signals_path.exists():
            old = pd.read_parquet(signals_path)
            touched = set(signals["episode_index"].unique())
            signals = pd.concat(
                [old[~old.episode_index.isin(touched)], signals], ignore_index=True
            ).sort_values(["episode_index", "frame_index"], ignore_index=True)
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
