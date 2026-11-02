"""목표 위치 입력 — 집을 물체의 위 카메라 좌표를 `observation.state` 뒤에 붙인 사본 (feature/target-conditioning.md).

    python -m lerobot_policy_piper.target wego-hansu/dualarm_sort_apple --targets targets.json
    → ~/.cache/huggingface/lerobot/wego-hansu/dualarm_sort_apple_target

## 왜

지시문은 종류만 말한다. 같은 종류가 판에 둘이면 어느 것인지 정책이 화면만 보고 정해야 하고, 시연은 40mm 격자
점에만 있어 그 사이 자리는 이미지 특징 공간에서 보간해야 한다. 집을 자리를 저차원 입력으로 직접 주면 둘 다
풀린다. 정책 코드·학습 명령은 그대로다 — state 가 길어질 뿐이다(SmolVLA 는 `max_state_dim` 32 까지 0 패딩).

## 인코딩 `rff_v1` — 가까운 자리는 비슷한 벡터

좌표 `(u, v)` 는 위 카메라 프레임의 폭·높이로 나눈 0~1 이다(플러그인의 검출 중심과 같은 정의). 등방성을 위해
`p = (u, v·h/w)` 로 두고, 가우시안 커널 `exp(−|p−q|²/2ℓ²)` 를 푸리에 특징으로 펼친다:

    γ(p) = [cos 2π b_k·p, sin 2π b_k·p]_{k<m},   ⟨γ(p), γ(q)⟩/m ≈ exp(−|p−q|²/2ℓ²)

ℓ 하나가 "몇 mm 떨어지면 다른 자리인가" 를 정한다. 기본 28mm — 격자 반 칸(20mm) 떨어진 자리가 이웃 칸과
0.77 비슷해 칸 사이를 보간하고, 사과 하나 거리(80mm)면 0.02 로 갈린다. 주파수는 무작위로 뽑지 않는다 — 반지름은
레일리 분위수, 방향은 황금각으로 고르게 깐다(준몬테카를로). m=7 에서 무작위 200벌의 중앙값보다 커널 오차가
작다(RMS 0.169 vs 0.200, 먼 거리 유사도 p99 0.47 vs 0.58). 원좌표 `u, v` 도 같이 둔다 — 7개 코사인의 먼 거리
되풀이(aliasing)를 원좌표가 가르고, 팔 고르기(`u < 0.52`)는 원좌표의 선형 경계다.

    [valid, u, v, cos…(m), sin…(m)]        목표 없음 = 전부 0

`valid` 는 목표 없는 에피소드(사과 분류의 `empty_target`)가 있을 때만 둔다. 그런 에피소드가 없으면 그 칸은
늘 1 이라 표준편차가 0 이다 — LeRobot 의 MEAN_STD 분모가 `std + 1e-8` 이라 추론에서 0 이 들어오는 순간 1e8 배로
터진다. 그래서 빼고, 그 체크포인트는 목표가 올 때까지 추론하지 않는다(wrapper/target_input.py).

## 목표는 에피소드마다 하나, 끝까지 고정

목표는 라벨러가 정한다(사과 분류는 `plugins/apple-sort/tools/targets.py` — 시작 프레임 검출 중 끝 프레임에서
사라진 그 종류). 이 변환기는 과제를 모른다 — 에피소드 → `(valid, u, v)` 표만 받는다. 쥔 뒤에도 바꾸지 않는다:
플러그인도 지시문을 정하는 순간에 한 번 보내므로 학습과 추론에서 목표가 바뀌는 때가 같다.

## stats 를 다시 쓴다

LeRobot 은 정규화 통계를 `meta/stats.json` 에서 읽기만 한다. 앞쪽(로봇 state) 통계는 원본 그대로 두고 — 기준
학습과 정규화가 비트 단위로 같아야 A/B 가 깨끗하다 — 새 칸만 전 프레임으로 정확히 센다(에피소드 안에서 상수라
에피소드 통계를 모아 분위수를 근사하면 틀린다). 새 칸 중 하나라도 표준편차가 0 이면 거절한다.

## 근거 메타 `meta/piper_target.json`

인코딩 사양(이름·주파수·종횡비·ℓ·카메라) 전체 + 에피소드별 목표. 학습한 체크포인트는 `train_config.json` 의
데이터셋을 따라 이 파일을 찾고, 추론 시작이 사양을 wrapper 에 싣는다(backend `app/services/target_input.py`).
받은 목표 표는 `meta/piper_target_source.json` 으로 그대로 옮겨 둔다 — 라벨러의 근거(출처·계획 칸 거리)가
데이터셋과 같이 다닌다.

## 순서: 목표 → 굽기

구운 사본(`_stage`)은 받지 않는다 — 굽기가 `meta/` 를 통째로 옮기므로 이 표식도 따라가 `<name>_target_stage` 도
목표 입력으로 읽힌다. 델타와는 순서가 상관없다(델타는 액션, 이것은 state, 채널은 이름으로 찾는다).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .bake import LEGACY_META_FILES as BAKE_LEGACY_META_FILES
from .bake import META_FILE as BAKE_META_FILE
from .bake import STATS_KEYS, _link_or_copy, default_output, repo_id_of, resolve_dataset

ENCODING = "rff_v1"
META_FILE = "piper_target.json"
SOURCE_FILE = "piper_target_source.json"        # 라벨러가 쓴 목표 표 사본 — 근거(출처·계획 칸 거리)를 데이터셋과 같이 둔다
SUFFIX = "_target"
VERSION = 1
STATE = "observation.state"
PREFIX = "target."
VALID = PREFIX + "valid"
GOLDEN = (math.sqrt(5.0) - 1.0) / 2.0          # 황금비의 역 — 방향을 고르게 까는 수열
DEFAULT_FREQS = 7                               # 14(양팔) + 17 = 31 ≤ SmolVLA max_state_dim 32
DEFAULT_LENGTH_MM = 28.0
MAX_STATE_DIM = 32                              # SmolVLA·π0 계열의 0 패딩 한도 — 넘으면 경고만 (ACT 는 한도가 없다)


# ── 인코딩 — wrapper/target_input.py 가 같은 식을 다시 적는다 (테스트가 대조) ──

def frequencies(m: int, length: float) -> np.ndarray:
    """`(m, 2)` 주파수 — `p` 단위 길이당 주기. 커널 폭 `length` 도 `p` 단위(프레임 폭 = 1)."""
    if m <= 0:
        return np.zeros((0, 2))
    if length <= 0:
        raise ValueError(f"커널 폭은 양수여야 한다: {length}")
    sigma = 1.0 / (2.0 * math.pi * length)
    k = np.arange(m)
    r = sigma * np.sqrt(-2.0 * np.log(1.0 - (k + 0.5) / m))
    th = math.pi * ((k * GOLDEN) % 1.0)       # cos 는 짝함수라 반원이면 된다
    return np.c_[r * np.cos(th), r * np.sin(th)]


def feature_names(m: int, with_valid: bool) -> list[str]:
    return ([VALID] if with_valid else []) + [PREFIX + "u", PREFIX + "v"] + \
        [f"{PREFIX}cos{k}" for k in range(m)] + [f"{PREFIX}sin{k}" for k in range(m)]


def encode(target: tuple[float, float] | None, spec: dict) -> np.ndarray:
    """목표 `(u, v)` 하나 → 사양의 이름 순서 벡터 (float32). `None` = 목표 없음 = 전부 0."""
    names = spec["names"]
    out = np.zeros(len(names), dtype=np.float32)
    if target is None:
        if names[0] != VALID:
            raise ValueError("이 사양은 목표 없음을 모른다 (학습 데이터에 목표 없는 에피소드가 없었다)")
        return out
    u, v = float(target[0]), float(target[1])
    b = np.asarray(spec["freqs"], dtype=np.float64).reshape(-1, 2)
    ph = 2.0 * math.pi * (b[:, 0] * u + b[:, 1] * v * float(spec["aspect"]))
    vals = np.concatenate([[u, v], np.cos(ph), np.sin(ph)])
    if names[0] == VALID:
        vals = np.concatenate([[1.0], vals])
    out[:] = vals
    return out


def make_spec(*, freqs: int, length_mm: float, mm_per_px: float, frame: tuple[int, int], with_valid: bool,
              state_names: list[str], camera: str = "", camera_id: str = "") -> dict:
    w, h = int(frame[0]), int(frame[1])
    if w <= 0 or h <= 0:
        raise ValueError(f"프레임 크기가 이상하다: {frame}")
    if not mm_per_px or mm_per_px <= 0:
        raise ValueError(f"mm/px 가 이상하다: {mm_per_px}")
    length = length_mm / mm_per_px / w           # p 단위 (프레임 폭 = 1)
    return {
        "encoding": ENCODING,
        "names": feature_names(freqs, with_valid),
        "freqs": np.round(frequencies(freqs, length), 6).tolist(),
        "aspect": round(h / w, 6),
        "length": round(length, 6),
        "length_mm": float(length_mm),
        "mm_per_px": round(float(mm_per_px), 4),
        "frame": [w, h],
        "camera": camera,
        "camera_id": camera_id,
        "state_names": list(state_names),
    }


# ── 목표 표 ──

def load_targets(path: Path) -> dict:
    """라벨러가 쓴 표 — `{"frame_size": [w, h], "mm_per_px", "camera", "episodes": {ep: {valid, u, v}}}`."""
    doc = json.loads(Path(path).read_text())
    eps = doc.get("episodes")
    if not isinstance(eps, dict) or not eps:
        raise ValueError(f"{path}: episodes 가 없다")
    out = {}
    for k, rec in eps.items():
        valid = bool(rec.get("valid"))
        if valid:
            u, v = float(rec["u"]), float(rec["v"])
            if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0) or not all(map(math.isfinite, (u, v))):
                raise ValueError(f"{path}: 에피소드 {k} 의 목표 ({u}, {v}) 가 0~1 밖이다")
            out[int(k)] = (u, v)
        else:
            out[int(k)] = None
    if not doc.get("frame_size") or len(doc["frame_size"]) != 2:
        raise ValueError(f"{path}: frame_size [w, h] 가 없다")
    return {**doc, "episodes": out}


# ── 변환 ──

def _column(table: pa.Table, name: str) -> np.ndarray:
    col = table.column(name).combine_chunks()
    width = col.type.list_size if pa.types.is_fixed_size_list(col.type) else len(col[0])
    return np.asarray(col.flatten().to_numpy(zero_copy_only=False)).reshape(len(table), width)


def _set_state(table: pa.Table, values: np.ndarray) -> pa.Table:
    """state 컬럼을 새 폭으로 바꿔 끼우고 huggingface 스키마 메타데이터의 길이도 고친다.

    ⚠ 메타데이터를 안 고치면 로더가 옛 feature(길이 14)로 읽는다 (bake.py 머리말)."""
    i = table.schema.get_field_index(STATE)
    field = table.schema.field(i)
    width = values.shape[1]
    if pa.types.is_fixed_size_list(field.type):
        typ = pa.list_(field.type.value_type, width)
    else:
        typ = field.type
    flat = pa.array(values.astype(np.float32).reshape(-1), type=pa.float32())
    arr = pa.FixedSizeListArray.from_arrays(flat, width) if pa.types.is_fixed_size_list(typ) else \
        pa.array(values.astype(np.float32).tolist(), type=typ)
    table = table.set_column(i, pa.field(STATE, typ, nullable=field.nullable), arr)
    meta = dict(table.schema.metadata or {})
    hf = json.loads(meta.get(b"huggingface", b"{}").decode() or "{}")
    feat = hf.get("info", {}).get("features", {}).get(STATE)
    if isinstance(feat, dict) and "length" in feat:
        feat["length"] = width
        meta[b"huggingface"] = json.dumps(hf).encode()
    return table.replace_schema_metadata(meta)


def _stats(values: np.ndarray) -> dict[str, np.ndarray]:
    from lerobot.datasets.compute_stats import compute_episode_stats

    feats = {STATE: {"dtype": "float32", "shape": (values.shape[1],)}}
    return compute_episode_stats({STATE: values.astype(np.float32)}, feats)[STATE]


def convert(src: Path, dst: Path, targets_path: Path, *, freqs: int = DEFAULT_FREQS,
            length_mm: float = DEFAULT_LENGTH_MM, force: bool = False, verify: bool = True, log=print) -> dict:
    meta = src / "meta"
    if (meta / META_FILE).is_file():
        raise ValueError(f"이미 목표 입력 데이터셋이다: {src}")
    if any((meta / n).is_file() for n in (BAKE_META_FILE, *BAKE_LEGACY_META_FILES)):
        raise ValueError("구운 사본(_stage)이다 — 원본에 목표를 붙인 뒤 그 사본을 굽는다")
    if dst.exists():
        if not force:
            raise FileExistsError(f"{dst} 가 이미 있다 — 덮어쓰려면 --force")
        if not (dst / "meta" / META_FILE).is_file():
            raise FileExistsError(f"{dst} 는 목표 입력 변환 결과물이 아니다 ({META_FILE} 없음) — 손으로 확인한다")

    info = json.loads((meta / "info.json").read_text())
    feat = info["features"].get(STATE)
    if not feat:
        raise ValueError(f"{STATE} 가 없는 데이터셋이다")
    state_names = list(feat.get("names") or [])
    if not state_names or any(n.startswith(PREFIX) for n in state_names):
        raise ValueError(f"state 이름이 없거나 이미 {PREFIX}* 가 있다: {state_names[:4]}…")
    width = int(feat["shape"][0])

    tg = load_targets(targets_path)
    files = sorted((src / "data").glob("*/*.parquet"))
    if not files:
        raise FileNotFoundError(f"data 에 parquet 가 없다: {src}")
    tables = [pq.read_table(f) for f in files]
    ep = np.concatenate([t.column("episode_index").to_numpy() for t in tables])
    episodes = sorted(int(e) for e in np.unique(ep))
    missing = [e for e in episodes if e not in tg["episodes"]]
    if missing:
        raise ValueError(f"목표 표에 없는 에피소드 {len(missing)}개: {missing[:10]}… — 라벨러를 이 데이터셋으로 다시 돌린다")
    invalid = [e for e in episodes if tg["episodes"][e] is None]
    spec = make_spec(freqs=freqs, length_mm=length_mm, mm_per_px=float(tg.get("mm_per_px") or 0),
                     frame=tuple(tg["frame_size"]), with_valid=bool(invalid), state_names=state_names,
                     camera=str(tg.get("camera") or ""), camera_id=str(tg.get("camera_id") or ""))
    k = len(spec["names"])
    if width + k > MAX_STATE_DIM:
        log(f"  ⚠ state {width}+{k}={width + k} 가 SmolVLA·π0 의 max_state_dim {MAX_STATE_DIM} 을 넘는다 — "
            f"ACT 계열만 학습할 수 있다 (--freqs 를 줄이면 들어간다)")

    enc = {e: encode(tg["episodes"][e], spec) for e in episodes}
    new_cols = [np.stack([enc[int(e)] for e in t.column("episode_index").to_numpy()]) for t in tables]
    added = np.concatenate(new_cols)
    std = added.std(0)
    flat = [n for n, s in zip(spec["names"], std) if s < 1e-6]
    if flat:
        raise ValueError(f"목표 칸 {flat} 가 데이터셋 전체에서 상수다 — 정규화 분모가 0 이 된다 (목표가 한 자리뿐인가?)")

    if dst.exists():
        shutil.rmtree(dst)          # 위에서 우리 결과물인지 확인했다. 검사가 다 통과한 뒤에 지운다
    try:
        rows_of = _write(src, dst, files, tables, new_cols, info, spec, width)
        sidecar = {
            "version": VERSION,
            "input": ENCODING,
            "source": str(src),
            "source_repo_id": repo_id_of(src),
            "targets_file": str(Path(targets_path).resolve()),
            "targets_sha256": hashlib.sha256(Path(targets_path).read_bytes()).hexdigest(),
            "spec": spec,
            "episodes": len(episodes),
            "invalid_episodes": invalid,
            "frames": int(len(ep)),
            "added_std": {n: round(float(s), 4) for n, s in zip(spec["names"], std)},
            "targets": {str(e): (None if tg["episodes"][e] is None else [round(c, 5) for c in tg["episodes"][e]])
                        for e in episodes},
        }
        (dst / "meta" / META_FILE).write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n")
        shutil.copyfile(targets_path, dst / "meta" / SOURCE_FILE)
        if verify:
            _verify(dst, enc, rows_of, width, log)
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise

    log(f"목표 입력을 붙였다 → {dst}")
    log(f"  state {width} → {width + k}  ({k}칸: {spec['names'][0]} … {spec['names'][-1]})")
    log(f"  ℓ = {length_mm:g}mm = {spec['length'] * spec['frame'][0]:.1f}px  (mm/px {spec['mm_per_px']})  "
        f"주파수 {freqs}개  목표 없음 {len(invalid)}개")
    return sidecar


def _write(src: Path, dst: Path, files, tables, new_cols, info: dict, spec: dict, width: int) -> dict[int, np.ndarray]:
    from lerobot.datasets.utils import serialize_dict

    k = len(spec["names"])
    rows_of: dict[int, list[np.ndarray]] = {}
    states, off = [], 0
    for f, t, add in zip(files, tables, new_cols):
        st = np.concatenate([_column(t, STATE).astype(np.float32), add], axis=1)
        out = dst / "data" / f.relative_to(src / "data")
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(_set_state(t, st), out)
        for e in np.unique(t.column("episode_index").to_numpy()):
            rows_of.setdefault(int(e), []).append(off + np.where(t.column("episode_index").to_numpy() == e)[0])
        states.append(st)
        off += len(t)
    allst = np.concatenate(states)
    rows = {e: np.concatenate(r) for e, r in rows_of.items()}

    shutil.copytree(src / "meta", dst / "meta", ignore=shutil.ignore_patterns("episodes"))
    info = json.loads(json.dumps(info))
    info["features"][STATE]["shape"] = [width + k]
    info["features"][STATE]["names"] = list(info["features"][STATE]["names"]) + spec["names"]
    (dst / "meta" / "info.json").write_text(json.dumps(info, indent=4, ensure_ascii=False) + "\n")

    # 에피소드 통계 — 앞쪽은 원본 값 그대로 이어 붙인다
    for f in sorted((src / "meta" / "episodes").glob("*/*.parquet")):
        t = pq.read_table(f)
        order = [int(i) for i in t.column("episode_index").to_pylist()]
        ep_new = {e: _stats(allst[rows[e], width:]) for e in order if e in rows}
        for key in STATS_KEYS:
            col = f"stats/{STATE}/{key}"
            if col not in t.column_names or key == "count":
                continue
            i = t.schema.get_field_index(col)
            old = t.column(col).to_pylist()
            vals = [list(o) + ep_new[e][key].tolist() for o, e in zip(old, order)]
            t = t.set_column(i, t.schema.field(i), pa.array(vals, type=t.schema.field(i).type))
        out = dst / "meta" / "episodes" / f.relative_to(src / "meta" / "episodes")
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(t, out)

    # 전역 통계 — 앞쪽은 원본 그대로, 새 칸은 전 프레임으로 정확히
    stats = json.loads((src / "meta" / "stats.json").read_text())
    new = serialize_dict({STATE: _stats(allst[:, width:])})[STATE]
    for key, val in stats[STATE].items():
        if key == "count":
            continue
        stats[STATE][key] = list(val) + list(new[key])
    (dst / "meta" / "stats.json").write_text(json.dumps(stats, indent=4) + "\n")

    for f in sorted((src / "videos").rglob("*")) if (src / "videos").is_dir() else []:
        if f.is_file():
            _link_or_copy(f, dst / "videos" / f.relative_to(src / "videos"))
    return rows


def _verify(dst: Path, enc: dict[int, np.ndarray], rows_of: dict[int, np.ndarray], width: int, log=print) -> None:
    """LeRobot 로더로 실제 연다 — 여기서 목표가 안 나오면 학습도 목표 없이 배운다."""
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    ds = LeRobotDataset(f"{dst.parent.name}/{dst.name}", root=dst)
    k = len(next(iter(enc.values())))
    if list(ds.meta.features[STATE]["shape"]) != [width + k]:
        raise RuntimeError(f"로더의 state 길이가 {ds.meta.features[STATE]['shape']} 다 (기대 {width + k})")
    for e in sorted(rows_of)[:: max(1, len(rows_of) // 5)]:
        i = int(rows_of[e][len(rows_of[e]) // 2])
        item = ds.hf_dataset[i]
        got = np.asarray(item[STATE], dtype=np.float32)
        if int(item["episode_index"]) != e or not np.allclose(got[width:], enc[e], atol=1e-6):
            raise RuntimeError(f"로더가 에피소드 {e} 의 목표를 안 돌려준다 (행 {i})")
    mean = np.asarray(ds.meta.stats[STATE]["mean"], dtype=np.float64)
    if mean.shape != (width + k,):
        raise RuntimeError(f"로더가 읽는 state 통계 길이가 {mean.shape} 다 — 정규화가 어긋난다")
    log(f"  verify: {len(ds)} 프레임, state {width + k}, 목표 칸 평균 {np.round(mean[width:width + 3], 3).tolist()} …")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dataset", help="경로 또는 org/name (LeRobot 캐시)")
    ap.add_argument("--targets", required=True, help="에피소드별 목표 표 (예: plugins/apple-sort/tools/targets.py 가 쓴 것)")
    ap.add_argument("--freqs", type=int, default=DEFAULT_FREQS, help=f"푸리에 주파수 수 m (기본 {DEFAULT_FREQS}, 0 = 원좌표만)")
    ap.add_argument("--length-mm", type=float, default=DEFAULT_LENGTH_MM,
                    help=f"커널 폭 ℓ mm — 이만큼 떨어지면 유사도 e^-0.5 (기본 {DEFAULT_LENGTH_MM:g})")
    ap.add_argument("--out", help=f"출력 디렉터리. 기본 LeRobot 캐시의 <org>/<이름>{SUFFIX}")
    ap.add_argument("--force", action="store_true", help="이전 변환 결과물이면 지우고 다시 만든다")
    ap.add_argument("--no-verify", action="store_true", help="LeRobot 로더 검증 생략")
    args = ap.parse_args(argv)

    src = resolve_dataset(args.dataset)
    dst = Path(args.out).expanduser().resolve() if args.out else default_output(src, SUFFIX)
    try:
        convert(src, dst, Path(args.targets), freqs=args.freqs, length_mm=args.length_mm,
                force=args.force, verify=not args.no_verify)
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError, KeyError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
