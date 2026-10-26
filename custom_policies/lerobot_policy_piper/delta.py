"""델타 변환 — 액션을 절대 관절 위치에서 **지령의 차분**으로 바꾼 사본 (feature/act-delta.md, F3).

    python -m lerobot_policy_piper.delta wego-hansu/min_cube_071410
    → ~/.cache/huggingface/lerobot/wego-hansu/min_cube_071410_delta

## 무엇을 바꾸나

`action` 의 관절 채널만: `d[t] = a[t] − a[t−1]`, 에피소드 첫 프레임은 `a[0] − s[0]`(같은 프레임 피드백).
그리퍼는 절대값 그대로 — 개도는 절대량이고 누적하면 오차만 쌓인다. `observation.state` 는 안 바꾼다.

정책은 아무것도 모른다 — ACT 는 `action` 컬럼의 숫자를 정규화해 회귀할 뿐이다. 그래서 정책 코드·학습 명령은
그대로이고, 추론에서 wrapper 가 절대로 되돌린다 (wrapper/delta_action.py):
절대 = 그 추론에 넣은 관측 state + 청크 안 누적합.

## stats 를 다시 쓴다

LeRobot 은 정규화 통계를 **`meta/stats.json` 에서 읽는다** — 학습 때 다시 세지 않는다. 그대로 두면 델타(0 근처,
몇 도)를 절대 위치의 평균·표준편차(수십 도)로 정규화해 학습이 망가진다. 그래서 액션 통계를 에피소드별
(`meta/episodes`)·전체(`stats.json`) 둘 다 새로 계산한다.

## 변환이 스스로 검증한다

쓴 델타(float32)를 누적해 절대를 되만들고 원본과 에피소드 전수 `RECON_TOL` 이내인지 본다. 어긋나면 사본을
지우고 실패로 끝낸다 — 에피소드 경계·프레임 순서를 잘못 다루면 여기서 걸린다.

## 근거 메타 `meta/piper_delta.json`

action_space · 델타 채널 · 스텝당 상한(clamp) · 추종 지연 ε 통계. 학습한 체크포인트는 `train_config.json` 의
데이터셋을 따라 이 파일을 찾는다 — 추론이 델타로 되돌릴지가 여기서 정해진다
(backend `app/services/action_space.py`). LeRobot 의 `info.json` 에는 아무것도 심지 않는다.

## 순서: 델타 → 굽기

구운 사본(`_stage`)은 받지 않는다. 원본을 델타로 바꾼 뒤 그 사본을 굽는다 — 굽기가 `meta/` 를 통째로 옮기므로
이 표식도 따라가 `<name>_delta_stage` 도 델타로 읽힌다. 페이즈 분석은 액션의 그리퍼 채널만 읽고 그리퍼는
절대 그대로라, 델타 사본에서도 분석·굽기가 맞다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .bake import LEGACY_META_FILES as BAKE_LEGACY_META_FILES
from .bake import META_FILE as BAKE_META_FILE
from .bake import STATS_KEYS, _link_or_copy, default_output, repo_id_of, resolve_dataset

ACTION_SPACE = "delta_v1"
META_FILE = "piper_delta.json"
SUFFIX = "_delta"
VERSION = 1
RECON_TOL = 1e-3          # 도(°). float32 델타를 수천 프레임 누적해도 1e-4 안쪽이다
ACTION, STATE = "action", "observation.state"


def delta_channels(names: list[str]) -> list[str]:
    """델타로 바꿀 채널 — 그리퍼를 뺀 전부 (양팔이면 14 중 12)."""
    return [n for n in names if "gripper" not in n]


def to_delta(action: np.ndarray, state0: np.ndarray, idx: list[int]) -> np.ndarray:
    """에피소드 하나, 프레임 순 `(T, D)` 절대 → 델타. `idx` 채널만 바꾼다. float64 로 계산한다."""
    out = np.asarray(action, dtype=np.float64).copy()
    a = out[:, idx].copy()
    out[0, idx] = a[0] - state0
    out[1:, idx] = np.diff(a, axis=0)
    return out


def from_delta(delta: np.ndarray, state0: np.ndarray, idx: list[int]) -> np.ndarray:
    """`to_delta` 의 역. 추론의 복원(wrapper)과 같은 식이다 — 기준은 첫 프레임의 state."""
    out = np.asarray(delta, dtype=np.float64).copy()
    out[:, idx] = state0 + np.cumsum(out[:, idx], axis=0)
    return out


def _column(table: pa.Table, name: str) -> np.ndarray:
    """fixed_size_list 컬럼 → `(N, D)`."""
    col = table.column(name).combine_chunks()
    width = col.type.list_size if pa.types.is_fixed_size_list(col.type) else len(col[0])
    return np.asarray(col.flatten().to_numpy(zero_copy_only=False)).reshape(len(table), width)


def _replace(table: pa.Table, name: str, values) -> pa.Table:
    """같은 arrow 타입으로 컬럼을 바꿔 끼운다 — 타입이 바뀌면 로더가 옛 feature 로 읽는다."""
    i = table.schema.get_field_index(name)
    field = table.schema.field(i)
    return table.set_column(i, field, pa.array(values, type=field.type))


def _ep_stats(values: np.ndarray, width: int) -> dict[str, np.ndarray]:
    from lerobot.datasets.compute_stats import compute_episode_stats

    feats = {ACTION: {"dtype": "float32", "shape": (width,)}}
    return compute_episode_stats({ACTION: values.astype(np.float32)}, feats)[ACTION]


def convert(src: Path, dst: Path, *, force: bool = False, verify: bool = True, log=print) -> dict:
    meta = src / "meta"
    if (meta / META_FILE).is_file():
        raise ValueError(f"이미 델타 데이터셋이다: {src}")
    if any((meta / n).is_file() for n in (BAKE_META_FILE, *BAKE_LEGACY_META_FILES)):
        raise ValueError("구운 사본(_stage)이다 — 원본을 델타로 바꾼 뒤 그 사본을 굽는다")
    if dst.exists():
        if not force:
            raise FileExistsError(f"{dst} 가 이미 있다 — 덮어쓰려면 --force")
        if not (dst / "meta" / META_FILE).is_file():
            # 우리가 만든 게 아닌 디렉터리는 --force 로도 안 지운다
            raise FileExistsError(f"{dst} 는 델타 변환 결과물이 아니다 ({META_FILE} 없음) — 손으로 확인한다")
        shutil.rmtree(dst)

    info = json.loads((meta / "info.json").read_text())
    feats = info["features"]
    names = list(feats[ACTION].get("names") or [])
    state_names = list(feats[STATE].get("names") or [])
    if not names:
        raise ValueError("action 에 채널 이름(names)이 없다 — 어느 채널이 그리퍼인지 알 수 없다")
    dnames = delta_channels(names)
    missing = [n for n in dnames if n not in state_names]
    if missing:
        raise ValueError(f"state 에 없는 액션 채널: {missing} — 첫 프레임 기준(a[0]−s[0])을 못 만든다")
    idx = [names.index(n) for n in dnames]
    sidx = [state_names.index(n) for n in dnames]
    width = len(names)

    # ── 1. 전 프레임을 읽는다 — 에피소드가 파일 경계를 넘어도 맞게 ──
    files = sorted((src / "data").glob("*/*.parquet"))
    if not files:
        raise FileNotFoundError(f"data 에 parquet 가 없다: {src}")
    tables = [pq.read_table(f) for f in files]
    act = np.concatenate([_column(t, ACTION) for t in tables]).astype(np.float64)
    st = np.concatenate([_column(t, STATE) for t in tables]).astype(np.float64)
    ep = np.concatenate([t.column("episode_index").to_numpy() for t in tables])
    fr = np.concatenate([t.column("frame_index").to_numpy() for t in tables])

    # ── 2. 에피소드마다 프레임 순으로 차분 + 되만들기 검증 ──
    new = act.copy()
    rows_of: dict[int, np.ndarray] = {}
    step_d, first_d, lag = [], [], []
    worst = 0.0
    for e in np.unique(ep):
        rows = np.where(ep == e)[0]
        rows = rows[np.argsort(fr[rows], kind="stable")]
        rows_of[int(e)] = rows
        s0 = st[rows[0], sidx]
        d = to_delta(act[rows], s0, idx).astype(np.float32)        # 저장되는 그대로
        back = from_delta(d, s0, idx)
        worst = max(worst, float(np.abs(back[:, idx] - act[rows][:, idx]).max()))
        new[rows] = d
        first_d.append(np.abs(d[0, idx]))
        if len(rows) > 1:
            step_d.append(np.abs(d[1:, idx]))
            # 추종 지연 ε: 학습 델타의 기준(a[t−1])과 추론 기준(s[t])의 차이
            lag.append(np.abs(act[rows][:-1][:, idx] - st[rows][1:][:, sidx]))
    if worst > RECON_TOL:
        raise ValueError(f"되만든 절대가 원본과 {worst:.2e}° 어긋난다 (허용 {RECON_TOL}) — 사본을 남기지 않는다")
    step_d = np.concatenate(step_d) if step_d else np.zeros((0, len(idx)))
    lag = np.concatenate(lag) if lag else np.zeros((0, len(idx)))

    try:
        _write(src, dst, files, tables, new, rows_of, width)
        summary = {
            "version": VERSION,
            "action_space": ACTION_SPACE,
            "source": str(src),
            "source_repo_id": repo_id_of(src),
            "names": names,
            "delta_names": dnames,
            "absolute_names": [n for n in names if n not in dnames],
            # 스텝당 상한 — 데모에서 실제로 나온 가장 큰 한 스텝(첫 프레임 제외). 그보다 빠른 예측은 데모 밖이라
            # 추론이 자른다. 그 이하는 하나도 안 자른다 (p99.9 로 자르면 실제로 있던 빠른 동작이 느려진다).
            "clamp": {n: round(float(step_d[:, j].max()), 4) if len(step_d) else None for j, n in enumerate(dnames)},
            "step_p999": {n: round(float(np.percentile(step_d[:, j], 99.9)), 4) if len(step_d) else None
                          for j, n in enumerate(dnames)},
            "first_frame_max": {n: round(float(np.max([f[j] for f in first_d])), 4) for j, n in enumerate(dnames)},
            "lag": {n: {"mean": round(float(lag[:, j].mean()), 4), "p95": round(float(np.percentile(lag[:, j], 95)), 4)}
                    for j, n in enumerate(dnames)} if len(lag) else {},
            "reconstruction_max_err": float(f"{worst:.3e}"),
            "episodes": len(rows_of),
            "frames": int(len(ep)),
        }
        (dst / "meta" / META_FILE).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        if verify:
            _verify(dst, new, log)
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise

    log(f"델타로 바꿨다 → {dst}")
    log(f"  델타 채널: {dnames}  (절대 유지: {summary['absolute_names']})")
    log(f"  되만들기 최대 오차: {worst:.2e}°   스텝당 상한: {summary['clamp']}")
    log(f"  추종 지연 ε 평균: { {n: v['mean'] for n, v in summary['lag'].items()} }")
    return summary


def _write(src: Path, dst: Path, files, tables, new: np.ndarray, rows_of: dict[int, np.ndarray], width: int) -> None:
    from lerobot.datasets.compute_stats import aggregate_stats
    from lerobot.datasets.utils import serialize_dict

    # data — 액션 컬럼만 바꾼다. 스키마 메타데이터는 set_column 이 그대로 둔다 (feature 가 안 바뀌었다)
    off = 0
    for f, t in zip(files, tables):
        n = len(t)
        out = dst / "data" / f.relative_to(src / "data")
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(_replace(t, ACTION, new[off:off + n].astype(np.float32).tolist()), out)
        off += n

    # meta — 통째로 복사한 뒤 액션 통계만 고친다. 에피소드 표는 아래에서 다시 쓴다
    shutil.copytree(src / "meta", dst / "meta", ignore=shutil.ignore_patterns("episodes"))
    ep_stats = {e: _ep_stats(new[rows], width) for e, rows in rows_of.items()}
    for f in sorted((src / "meta" / "episodes").glob("*/*.parquet")):
        t = pq.read_table(f)
        order = [int(i) for i in t.column("episode_index").to_pylist()]
        for k in STATS_KEYS:
            col = f"stats/{ACTION}/{k}"
            if col in t.column_names:
                t = _replace(t, col, [ep_stats[i][k].tolist() for i in order])
        out = dst / "meta" / "episodes" / f.relative_to(src / "meta" / "episodes")
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(t, out)

    stats = json.loads((src / "meta" / "stats.json").read_text())
    stats[ACTION] = serialize_dict(aggregate_stats([{ACTION: s} for s in ep_stats.values()]))[ACTION]
    (dst / "meta" / "stats.json").write_text(json.dumps(stats, indent=4) + "\n")

    # 비디오 — 내용이 같으니 링크. images/(녹화 캐시)는 데이터셋의 일부가 아니다
    for f in sorted((src / "videos").rglob("*")) if (src / "videos").is_dir() else []:
        if f.is_file():
            _link_or_copy(f, dst / "videos" / f.relative_to(src / "videos"))


def _verify(dst: Path, new: np.ndarray, log=print) -> None:
    """LeRobot 로더로 실제 연다 — 여기서 델타가 안 나오면 학습도 절대로 배운다."""
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    ds = LeRobotDataset(f"{dst.parent.name}/{dst.name}", root=dst)
    # 로더의 행 순서 = 데이터 파일을 이름순으로 이어 붙인 순서 = `new` 의 순서
    for i in (0, len(ds) // 2, len(ds) - 1):
        got = np.asarray(ds.hf_dataset[i][ACTION], dtype=np.float64)
        if not np.allclose(got, new[i], atol=1e-5):
            raise RuntimeError(f"로더가 델타가 아닌 액션을 돌려준다 (행 {i})")
    mean = np.asarray(ds.meta.stats[ACTION]["mean"], dtype=np.float64)
    if not np.allclose(mean, new.mean(0), atol=1e-3):
        raise RuntimeError("로더가 읽는 액션 통계가 델타의 것이 아니다 — 정규화가 절대 기준으로 된다")
    log(f"  verify: {len(ds)} 프레임, 액션 평균 {np.round(mean, 3).tolist()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dataset", help="경로 또는 org/name (LeRobot 캐시)")
    ap.add_argument("--out", help=f"출력 디렉터리. 기본 LeRobot 캐시의 <org>/<이름>{SUFFIX}")
    ap.add_argument("--force", action="store_true", help="이전 변환 결과물이면 지우고 다시 만든다")
    ap.add_argument("--no-verify", action="store_true", help="LeRobot 로더 검증 생략")
    args = ap.parse_args(argv)

    src = resolve_dataset(args.dataset)
    dst = Path(args.out).expanduser().resolve() if args.out else default_output(src, SUFFIX)
    try:
        convert(src, dst, force=args.force, verify=not args.no_verify)
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
