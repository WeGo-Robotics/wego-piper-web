"""J6 캘리브레이션 교체(-100000,130000 → -120000,120000)에 맞춰 기록된 데이터셋의
정규화 값을 다시 매긴다. 2026-09-04 에 25 개 데이터셋 339,845 프레임에 적용했다.

## 왜 데이터까지 건드리나

캘리브레이션은 raw 엔코더 ↔ 정규화 값의 **환율**이다. 환율을 바꾸면 이미 적어 둔
숫자의 뜻이 바뀐다 — 코드만 고치고 데이터를 두면 학습된 정책이 j6 에서 10~20°
어긋난 곳을 가리킨다. raw 를 거치면 변환은 정확히 선형이다:

    n_new = n_old · (230000/240000) + 12.5

## ⚠ 통계도 같이 간다

`meta/stats.json` 과 에피소드별 `stats/*` 가 학습 정규화에 그대로 쓰인다. 데이터만
바꾸고 두면 조용히 어긋난다. min·max·mean·분위수는 같은 선형식, std 는 기울기만
(평행이동은 산포를 안 바꾼다), count 는 그대로다.

## ⚠ 열 타입이 두 가지다

데이터 파일은 `fixed_size_list<float>`, 에피소드 통계는 `list<double>` 이다.
처음엔 앞의 것만 처리해서 **통계가 조용히 안 바뀌었다** — 백업과 대조하지
않았으면 못 봤다. 두 경로를 다 탄다.

## ⚠ 멱등이 아니다

두 번 돌리면 두 번 변환된다. `--from-backup` 이 원본을 백업에서 읽는 이유다.

    python tools/migrate_j6_calibration.py <루트> [--apply] [--from-backup <경로>]
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

OLD = (-100000, 130000)
NEW = (-120000, 120000)
#: 옛 정규화 → 새 정규화. raw 를 거쳐 유도한 값이다.
SLOPE = (OLD[1] - OLD[0]) / (NEW[1] - NEW[0])
OFFSET = ((OLD[0] - NEW[0]) / (NEW[1] - NEW[0])) * 200 - 100 + 100 * SLOPE

#: 평행이동까지 받는 통계. `std` 는 기울기만, `count` 는 그대로다.
LINEAR = ("min", "max", "mean", "q01", "q10", "q50", "q90", "q99")
FEATURES = ("observation.state", "action")


def convert(v, stat: str | None = None):
    return v * SLOPE if stat == "std" else v * SLOPE + OFFSET


def j6_indices(info: dict, col: str) -> list[int]:
    """j6 이 몇 번째 칸인가. **이름으로 찾는다** — 양팔이면 5 와 12 로 둘이다."""
    names = (info.get("features", {}).get(col) or {}).get("names") or []
    return [i for i, n in enumerate(names) if "joint6" in str(n)]


def _fixed_list(table, col, idx, stat=None):
    arr = table.column(col).combine_chunks()
    size = arr.type.list_size
    flat = np.asarray(arr.flatten().to_numpy(zero_copy_only=False),
                      dtype=np.float64).reshape(-1, size)
    flat[:, idx] = convert(flat[:, idx], stat)
    child = pa.array(flat.reshape(-1), type=arr.type.value_type)
    return pa.FixedSizeListArray.from_arrays(child, size)


def _var_list(table, col, idx, stat=None):
    """`list<double>` — 길이가 고정이 아니라 행마다 푼다."""
    out = []
    for row in table.column(col).combine_chunks().to_pylist():
        if row is None:
            out.append(row)
            continue
        r = list(row)
        for i in idx:
            if i < len(r) and r[i] is not None:
                r[i] = convert(r[i], stat)
        out.append(r)
    return pa.array(out, type=table.schema.field(col).type)


def fix_parquet(src: str, dst: str, idx_by_col: dict[str, list[int]], dry: bool) -> int:
    table = pq.read_table(src)
    cols = list(table.column_names)
    n = 0
    for col in cols:
        parts = col.split("/")
        if parts[0] == "stats" and len(parts) == 3:
            feat, stat = parts[1], parts[2]
            if stat == "count" or (stat not in LINEAR and stat != "std"):
                continue
        else:
            feat, stat = col, None
        idx = idx_by_col.get(feat)
        if not idx:
            continue
        field = table.schema.field(col)
        if pa.types.is_fixed_size_list(field.type):
            new = _fixed_list(table, col, idx, stat)
        elif pa.types.is_list(field.type):
            new = _var_list(table, col, idx, stat)
        else:
            continue
        table = table.set_column(cols.index(col), field, new)
        n += 1
    if not dry:
        pq.write_table(table, dst, compression="snappy")
    return n


def fix_stats(src: str, dst: str, idx_by_col: dict[str, list[int]], dry: bool) -> None:
    st = json.load(open(src))
    for feat, idx in idx_by_col.items():
        if feat not in st or not idx:
            continue
        for stat, vals in st[feat].items():
            if not isinstance(vals, list) or stat == "count":
                continue
            a = np.asarray(vals, dtype=np.float64)
            if a.ndim != 1 or a.size <= max(idx):
                continue
            if stat in LINEAR or stat == "std":
                a[idx] = convert(a[idx], stat)
            st[feat][stat] = a.tolist()
    if not dry:
        json.dump(st, open(dst, "w"), indent=4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--from-backup", default=None,
                    help="원본을 여기서 읽는다. 멱등이 아니라 재실행 때 필요하다")
    args = ap.parse_args()
    dry, root, bk = not args.apply, args.root, args.from_backup
    print(f"  기울기 {SLOPE:.9f}  절편 {OFFSET:.4f}  ({'드라이런' if dry else '적용'})\n")

    done, broken = 0, []
    for inf in sorted(glob.glob(f"{root}/*/*/meta/info.json")):
        ds = inf.split("/meta/")[0]
        rel = os.path.relpath(ds, root)
        info = json.load(open(inf))
        idx_by_col = {c: j6_indices(info, c) for c in FEATURES}
        if not any(idx_by_col.values()):
            continue
        targets = sorted(glob.glob(f"{ds}/data/**/*.parquet", recursive=True)) + \
            sorted(glob.glob(f"{ds}/meta/episodes/**/*.parquet", recursive=True))
        srcs = [(os.path.join(bk, os.path.relpath(p, root)) if bk else p, p) for p in targets]
        # ⚠ **먼저 다 읽어 본다.** 반쯤 변환된 데이터셋은 원본보다 나쁘다
        if any(not os.path.exists(s) for s, _ in srcs):
            print(f"  {rel:38s} ⚠ 백업 없음 — 건너뜀")
            continue
        try:
            for s, _ in srcs:
                pq.read_metadata(s)
        except pa.ArrowInvalid:
            broken.append(rel)
            print(f"  {rel:38s} ⚠ 손상 — 통째로 건너뜀")
            continue
        for s, d in srcs:
            fix_parquet(s, d, idx_by_col, dry)
        sj = f"{ds}/meta/stats.json"
        if os.path.exists(sj):
            fix_stats(os.path.join(bk, os.path.relpath(sj, root)) if bk else sj,
                      sj, idx_by_col, dry)
        done += 1
        print(f"  {rel:38s} 변환")
    print(f"\n  데이터셋 {done} 개")
    if broken:
        print(f"  ⚠ 손상되어 건너뛴 것 {len(broken)} 개: {', '.join(broken)}")


if __name__ == "__main__":
    main()
