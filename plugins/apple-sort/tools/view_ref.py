"""학습 데이터셋의 위 카메라 프레임에서 판 AprilTag 자리를 잰다 → 화면 보정 기준표 `view_ref.json`.

    python plugins/apple-sort/tools/view_ref.py wego-hansu/dualarm_sort_apple [--cam left_top] [--out <json>]

플러그인은 켤 때 지금 위 카메라에서 같은 태그를 찾아 지금 화면 → 학습 화면 닮음변환을 잰다(`main.view_transform`).
카메라가 녹화 때 자리에서 비켜났어도 목표 좌표가 학습 때와 같은 자리를 가리킨다 — 목표 입력 정책은 좌표를 학습
때 자리로 읽는다(117 2026-11-02: 위 카메라가 녹화 대비 38px 밀려 사과를 거듭 밀었다).

에피소드마다 앞 프레임 몇 장(팔이 원위치 — 태그를 안 가린다)에서 태그마다 모서리 중앙값을 낸다. 검출은 플러그인과
같은 코드(`main.detect_tags`). 새 위치에서 다시 녹화해 학습했으면 그 데이터셋으로 다시 만든다.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import sys
from pathlib import Path

import av
import numpy as np
import pandas as pd

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
import main as P  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("dataset", help="org/name (LeRobot 캐시) 또는 경로")
ap.add_argument("--cam", default="left_top", help="observation.images.<cam> — 위 카메라")
ap.add_argument("--out", default=str(PLUGIN_DIR / "view_ref.json"))
ap.add_argument("--every", type=int, default=6, help="에피소드 몇 개마다 하나씩 볼지")
ap.add_argument("--offsets", default="2,8", help="에피소드 앞에서 볼 프레임")
args = ap.parse_args()

D = Path(args.dataset).expanduser()
if not D.is_dir():
    D = Path(os.path.expanduser(f"~/.cache/huggingface/lerobot/{args.dataset}"))
CAM = f"observation.images.{args.cam}"
info = json.loads((D / "meta/info.json").read_text())
FPS = float(info["fps"])
H_IMG, W_IMG = info["features"][CAM]["shape"][:2]
meta = pd.concat(pd.read_parquet(f) for f in sorted(glob.glob(str(D / "meta/episodes/*/*.parquet"))))
offsets = [int(x) for x in args.offsets.split(",")]

want: dict[tuple[int, int], set[int]] = {}
for _, row in meta.iterrows():
    if int(row["episode_index"]) % args.every:
        continue
    key = (int(row[f"videos/{CAM}/chunk_index"]), int(row[f"videos/{CAM}/file_index"]))
    f0 = round(float(row[f"videos/{CAM}/from_timestamp"]) * FPS)
    want.setdefault(key, set()).update(f0 + min(o, int(row["length"]) - 1) for o in offsets)

seen: dict[int, list] = {}
n_frames = 0
for (chunk, fidx), frames in sorted(want.items()):
    # cv2 는 AV1(이 데이터셋의 file-000)을 못 연다 — PyAV 로 읽는다 (targets.py 와 같다)
    with av.open(str(D / f"videos/{CAM}/chunk-{chunk:03d}/file-{fidx:03d}.mp4")) as cont:
        last = max(frames)
        for i, fr in enumerate(cont.decode(video=0)):
            if i in frames:
                n_frames += 1
                for tid, c in P.detect_tags(fr.to_ndarray(format="bgr24")).items():
                    seen.setdefault(tid, []).append(c)
            if i >= last:
                break

tags, report = {}, {}
for tid, lst in sorted(seen.items()):
    arr = np.stack(lst)
    med = np.median(arr, axis=0)
    spread = np.abs(arr - med).max(axis=(1, 2))
    if len(lst) < max(3, n_frames // 10):                       # 가끔 잡힌 것은 오검출일 수 있다
        continue
    tags[str(tid)] = np.round(med, 2).tolist()
    report[str(tid)] = {"frames": len(lst), "center": [round(float(x), 1) for x in med.mean(0)],
                        "spread_p95_px": round(float(np.percentile(spread, 95)), 2)}
if len(tags) < 2:
    sys.exit(f"태그가 {len(tags)}개만 꾸준히 잡혔다 ({ {k: len(v) for k, v in seen.items()} }) — 닮음변환에 둘이 필요하다")

out = {"version": 1, "tool": "plugins/apple-sort/tools/view_ref.py", "dataset": args.dataset, "camera": CAM,
       "frame": [W_IMG, H_IMG], "family": "apriltag_36h11", "frames_checked": n_frames,
       "made": datetime.date.today().isoformat(), "tags": tags, "report": report}
text = json.dumps(out, ensure_ascii=False, indent=1)
text = re.sub(r"\[\s+(-?[\d.]+),\s+(-?[\d.]+)\s+\]", r"[\1, \2]", text)      # 모서리 [x, y] 는 한 줄로
Path(args.out).write_text(text + "\n")
print(json.dumps(report, ensure_ascii=False))
print(f"→ {args.out}")
