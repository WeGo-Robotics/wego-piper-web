"""굽기 — `meta/phase_labels.json` 세그먼트를 LeRobot **subtask** 로 (feature/act-aux.md §4).

    python -m lerobot_policy_act_aux.bake wego-hansu/min_cube_071410
    → ~/.cache/huggingface/lerobot/wego-hansu/min_cube_071410_stage

[01-phase-annotation §5](../../feature/01-phase-annotation.md) 굽기의 **경량 변형**이다:
`observation.state` 는 건드리지 않고 `subtask_index` int64 컬럼 + `meta/subtasks.parquet` 만
보탠다. 관측 차원이 안 바뀌고 추론 측에 온라인 추정기도 필요 없다.

## 왜 별도 컬럼(`task_stage`)이 아니라 subtask 인가

LeRobot 전처리 파이프라인은 배치를 transition 으로 바꿀 때 **화이트리스트**만 남긴다
(`lerobot/processor/converters.py:_extract_complementary_data` — `*_is_pad`, `task`, `subtask`,
`index`, `task_index`, `episode_index`). 임의 컬럼은 로더까지는 살아도 정책 forward 에는
안 온다 — 실제로 그렇게 한 번 죽었다. `subtask` 는 v3 의 정식 프레임별 하위작업 개념이라
화이트리스트를 통과하고, `dataset_to_policy_features` 가 건너뛰어 정책 입력 feature 에도 안 섞인다.
로더가 `subtask_index` → `meta/subtasks.parquet` 이름 → 배치 `subtask`(문자열) 로 바꿔준다.

## 원본은 절대 in-place 로 고치지 않는다

새 디렉터리에 쓴다. 비디오는 내용이 같으니 하드링크(→ 심링크 → 복사 순 폴백).
`images/`(녹화 캐시)는 데이터셋의 일부가 아니라 건너뛴다.

## 왜 pyarrow 로 직접 쓰나

LeRobot 로더는 `Dataset.from_parquet(features=...)` 로 읽고, parquet 의 `huggingface`
스키마 메타데이터를 본다. 컬럼만 붙이고 메타데이터를 안 고치면 로더가 옛 feature 목록으로
읽어 새 컬럼이 조용히 사라진다. 그래서 테이블·메타데이터를 같이 고친다.

## 라벨 없는 프레임은 `_unlabeled`

세그먼트 밖 프레임(정상이면 없다)과 `--reviewed-only` 로 제외한 에피소드는 `_unlabeled`
subtask 로 쓴다. -1 을 쓰면 안 된다 — 로더가 `subtasks.iloc[-1]` 로 **마지막 subtask 이름을**
조용히 돌려준다. 정책은 `stage_names` 에 없는 이름을 -1 로 바꿔 손실에서 뺀다.
에피소드를 **지우지 않는** 이유: 번호를 당기면 비디오 타임스탬프·사이드카 전부를 재매핑해야
한다 (piper_phase.sidecar 가 그 사고를 다룬다). 홀드아웃은 학습 쪽 `--dataset.episodes=[...]` 로.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

SUBTASK_INDEX_KEY = "subtask_index"
SUBTASKS_FILE = "subtasks.parquet"
UNLABELED = "_unlabeled"
META_FILE = "act_aux.json"
BAKE_VERSION = 1
STATS_KEYS = ("min", "max", "mean", "std", "count", "q01", "q10", "q50", "q90", "q99")


def _lerobot_home() -> Path:
    default = Path.home() / ".cache" / "huggingface" / "lerobot"
    return Path(os.getenv("HF_LEROBOT_HOME", default)).expanduser()


def resolve_dataset(arg: str) -> Path:
    """경로면 그대로, `org/name` 이면 LeRobot 캐시에서."""
    p = Path(arg).expanduser()
    if p.is_dir():
        return p.resolve()
    cand = _lerobot_home() / arg
    if cand.is_dir():
        return cand.resolve()
    raise FileNotFoundError(f"데이터셋을 못 찾았다: {arg} (경로도, {cand} 도 아님)")


def _link_or_copy(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        try:
            os.symlink(src, dst)
            return "symlink"
        except OSError:
            shutil.copy2(src, dst)
            return "copy"


def frame_labels(segments: list[list[int]], n_frames: int) -> np.ndarray:
    """`[[start, end(포함), code], ...]` → 길이 n_frames 의 int64. 빈 곳은 -1."""
    out = np.full(n_frames, -1, dtype=np.int64)
    for start, end, code in segments:
        s, e = max(0, int(start)), min(n_frames - 1, int(end))
        if e >= s:
            out[s:e + 1] = int(code)
    return out


def recommended_weights(counts: np.ndarray) -> list[float]:
    """√역빈도, 평균 1. 모델의 `stage_balance=True` 가 학습 중 계산하는 것과 같은 식."""
    c = np.maximum(counts.astype(np.float64), 1.0)
    w = np.sqrt(c.sum() / c)
    return [round(float(x), 4) for x in (w / w.mean())]


def _feature_stats(values: np.ndarray) -> dict[str, np.ndarray]:
    """LeRobot `get_feature_stats` 와 같은 모양 — 1-D 데이터는 각 통계가 shape (1,)."""
    from lerobot.datasets.compute_stats import compute_episode_stats

    feats = {SUBTASK_INDEX_KEY: {"dtype": "int64", "shape": (1,)}}
    return compute_episode_stats({SUBTASK_INDEX_KEY: values}, feats)[SUBTASK_INDEX_KEY]


def bake(src: Path, dst: Path, *, reviewed_only: bool = False, force: bool = False,
         verify: bool = True, log=print) -> dict:
    labels_path = src / "meta" / "phase_labels.json"
    if not labels_path.is_file():
        raise FileNotFoundError(
            f"{labels_path} 가 없다 — 먼저 `python -m piper_phase {src}` 로 페이즈를 분석한다")
    labels = json.loads(labels_path.read_text())
    stage_names: list[str] = list(labels["phases"])
    episodes: dict[str, dict] = labels["episodes"]

    if dst.exists():
        if not force:
            raise FileExistsError(f"{dst} 가 이미 있다 — 덮어쓰려면 --force")
        if not (dst / "meta" / META_FILE).is_file():
            # 우리가 구운 게 아닌 디렉터리는 --force 로도 안 지운다
            raise FileExistsError(f"{dst} 는 bake 결과물이 아니다 ({META_FILE} 없음) — 손으로 확인한다")
        shutil.rmtree(dst)

    info = json.loads((src / "meta" / "info.json").read_text())
    if SUBTASK_INDEX_KEY in info["features"] or (src / "meta" / SUBTASKS_FILE).exists():
        raise ValueError(f"원본에 이미 subtask 가 있다: {src}")
    if UNLABELED in stage_names:
        raise ValueError(f"phases 에 예약된 이름 {UNLABELED!r} 이 있다")
    unlabeled_idx = len(stage_names)          # subtasks.parquet 의 마지막 행

    # ── 1. 에피소드 길이 (meta/episodes) ──
    ep_files = sorted((src / "meta" / "episodes").glob("*/*.parquet"))
    if not ep_files:
        raise FileNotFoundError(f"meta/episodes 에 parquet 가 없다: {src}")
    ep_tables = [pq.read_table(f) for f in ep_files]
    ep_len: dict[int, int] = {}
    for t in ep_tables:
        for idx, n in zip(t.column("episode_index").to_pylist(), t.column("length").to_pylist()):
            ep_len[int(idx)] = int(n)

    # ── 2. 에피소드별 프레임 라벨 ──
    per_ep: dict[int, np.ndarray] = {}
    skipped: list[int] = []
    for ep, n in ep_len.items():
        rec = episodes.get(str(ep))
        if rec is None or (reviewed_only and not rec.get("reviewed", False)):
            per_ep[ep] = np.full(n, -1, dtype=np.int64)
            skipped.append(ep)
            continue
        arr = frame_labels(rec["segments"], n)
        if rec.get("frames") not in (None, n):
            log(f"  ⚠ ep{ep}: 사이드카 frames={rec.get('frames')} ≠ 데이터셋 length={n} — 사이드카가 낡았다")
        per_ep[ep] = arr

    # ── 3. data parquet — 컬럼 + huggingface 메타데이터 ──
    counts = np.zeros(len(stage_names), dtype=np.int64)
    unlabeled = 0
    data_files = sorted((src / "data").glob("*/*.parquet"))
    if not data_files:
        raise FileNotFoundError(f"data 에 parquet 가 없다: {src}")
    for f in data_files:
        t = pq.read_table(f)
        eps = np.asarray(t.column("episode_index").to_numpy(), dtype=np.int64)
        frs = np.asarray(t.column("frame_index").to_numpy(), dtype=np.int64)
        col = np.full(len(t), -1, dtype=np.int64)
        for ep in np.unique(eps):
            m = eps == ep
            arr = per_ep.get(int(ep))
            if arr is None:
                continue
            fi = frs[m]
            ok = fi < len(arr)
            vals = np.full(m.sum(), -1, dtype=np.int64)
            vals[ok] = arr[fi[ok]]
            col[m] = vals
        if col.max() >= len(stage_names):
            raise ValueError(f"{f.name}: 페이즈 코드 {col.max()} ≥ phases 길이 {len(stage_names)}")
        counts += np.bincount(col[col >= 0], minlength=len(stage_names))
        unlabeled += int((col < 0).sum())
        col = np.where(col < 0, unlabeled_idx, col)      # -1 은 쓰지 않는다 (머리말)

        t = t.append_column(SUBTASK_INDEX_KEY, pa.array(col, type=pa.int64()))
        meta = dict(t.schema.metadata or {})
        hf = json.loads(meta.get(b"huggingface", b"{}").decode() or "{}")
        hf.setdefault("info", {}).setdefault("features", {})[SUBTASK_INDEX_KEY] = {"dtype": "int64", "_type": "Value"}
        meta[b"huggingface"] = json.dumps(hf).encode()
        out = dst / "data" / f.relative_to(src / "data")
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(t.replace_schema_metadata(meta), out)

    # ── 4. meta — 통째로 복사한 뒤 셋만 고친다 ──
    shutil.copytree(src / "meta", dst / "meta", ignore=shutil.ignore_patterns("episodes"))

    info["features"][SUBTASK_INDEX_KEY] = {"dtype": "int64", "shape": [1], "names": None}
    (dst / "meta" / "info.json").write_text(json.dumps(info, indent=4, ensure_ascii=False) + "\n")

    # meta/subtasks.parquet — tasks.parquet 와 같은 꼴: index=이름, 컬럼=subtask_index
    import pandas as pd
    names = stage_names + [UNLABELED]
    pd.DataFrame({SUBTASK_INDEX_KEY: np.arange(len(names), dtype=np.int64)},
                 index=pd.Index(names, name="subtask")).to_parquet(dst / "meta" / SUBTASKS_FILE)

    # 에피소드 stats 컬럼 + 전역 stats. 정규화엔 안 쓰이지만 features 와 stats 키가 어긋난
    # 데이터셋을 남기지 않는다 — 에피소드 추가(append) 시 aggregate_stats 가 키를 대조한다.
    from lerobot.datasets.compute_stats import aggregate_stats
    from lerobot.datasets.utils import serialize_dict

    ep_stats: dict[int, dict[str, np.ndarray]] = {
        ep: _feature_stats(np.where(per_ep[ep] < 0, unlabeled_idx, per_ep[ep])) for ep in ep_len}
    for f, t in zip(ep_files, ep_tables):
        idxs = [int(i) for i in t.column("episode_index").to_pylist()]
        for k in STATS_KEYS:
            t = t.append_column(f"stats/{SUBTASK_INDEX_KEY}/{k}",
                                pa.array([ep_stats[i][k].tolist() for i in idxs]))
        out = dst / "meta" / "episodes" / f.relative_to(src / "meta" / "episodes")
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(t, out)

    stats = json.loads((src / "meta" / "stats.json").read_text())
    stats[SUBTASK_INDEX_KEY] = serialize_dict(aggregate_stats([{SUBTASK_INDEX_KEY: s} for s in ep_stats.values()]))[SUBTASK_INDEX_KEY]
    (dst / "meta" / "stats.json").write_text(json.dumps(stats, indent=4) + "\n")

    # ── 5. 비디오 — 링크 ──
    link_modes: dict[str, int] = {}
    for f in sorted((src / "videos").rglob("*")):
        if f.is_file():
            mode = _link_or_copy(f, dst / "videos" / f.relative_to(src / "videos"))
            link_modes[mode] = link_modes.get(mode, 0) + 1

    # ── 6. 근거 메타 ──
    summary = {
        "version": BAKE_VERSION,
        "source": str(src),
        "source_repo_id": f"{src.parent.name}/{src.name}",
        # 원본 사이드카가 bake 뒤에 바뀌었는지 스캐너가 이 해시로 안다 (feature/act-aux.md §4.5)
        "source_labels_sha256": hashlib.sha256(labels_path.read_bytes()).hexdigest(),
        "subtask_index_key": SUBTASK_INDEX_KEY,
        "stage_names": stage_names,
        "unlabeled_name": UNLABELED,
        "class_counts": {name: int(c) for name, c in zip(stage_names, counts)},
        "unlabeled_frames": unlabeled,
        "reviewed_only": reviewed_only,
        "skipped_episodes": skipped,
        "recommended_class_weights": recommended_weights(counts),
        "videos": link_modes,
    }
    (dst / "meta" / META_FILE).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

    log(f"구웠다 → {dst}")
    log(f"  stages: {stage_names}")
    log(f"  counts: {summary['class_counts']}  unlabeled={unlabeled}  skipped={skipped}")
    log(f"  videos: {link_modes}")

    if verify:
        _verify(dst, log)
    return summary


def _verify(dst: Path, log=print) -> None:
    """LeRobot 로더로 실제 열어 본다 — 여기서 안 열리면 학습에서 안 열린다."""
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    repo_id = f"{dst.parent.name}/{dst.name}"
    ds = LeRobotDataset(repo_id, root=dst)
    if ds.meta.subtasks is None:
        raise RuntimeError("로더가 meta/subtasks.parquet 를 못 읽는다")
    item, last = ds[0], ds[len(ds) - 1]
    for it in (item, last):
        if SUBTASK_INDEX_KEY not in it or "subtask" not in it:
            raise RuntimeError(f"로더가 subtask 를 안 돌려준다 — features/메타데이터를 확인한다 (keys={sorted(it)})")
    log(f"  verify: {len(ds)} 프레임, subtask[0]={item['subtask']!r} … subtask[-1]={last['subtask']!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dataset", help="경로 또는 org/name (LeRobot 캐시)")
    ap.add_argument("--out", help="출력 디렉터리. 기본 <원본>_stage")
    ap.add_argument("--reviewed-only", action="store_true",
                    help="reviewed=false 에피소드는 라벨을 -1 로 (학습에서 무시)")
    ap.add_argument("--force", action="store_true", help="이전 bake 결과물이면 지우고 다시 굽는다")
    ap.add_argument("--no-verify", action="store_true", help="LeRobot 로더 검증 생략")
    args = ap.parse_args(argv)

    src = resolve_dataset(args.dataset)
    dst = Path(args.out).expanduser().resolve() if args.out else src.with_name(src.name + "_stage")
    try:
        bake(src, dst, reviewed_only=args.reviewed_only, force=args.force, verify=not args.no_verify)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
