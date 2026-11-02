"""에피소드마다 집은 사과의 위 카메라 위치 → 목표 표 (feature/target-conditioning.md).

    python plugins/apple-sort/tools/targets.py wego-hansu/dualarm_sort_apple <targets.json> [--sheet <폴더>]
    python -m lerobot_policy_piper.target wego-hansu/dualarm_sort_apple --targets <targets.json>

**목표 = 시작 프레임의 그 종류 사과 중 끝 프레임에서 사라진 것.** 계획(`meta/piper_scenario.json`)은 어느 칸을
집을지 말하지만 칸은 화면 좌표가 아니고 사람이 놓은 자리는 칸에서 몇 mm 씩 어긋난다. 그래서 위치는 검출에서
얻고, 계획은 두 가지에만 쓴다 — 집은 **종류**(지시문이 정한 것)와 **확인**(고른 사과가 계획 칸에서 얼마나 먼가).

- 검출·판 위 판정은 플러그인과 같은 코드다(`main.board_apples`, `AppleSort._one_per_apple`, 같은 모델·conf·ROI).
  추론에서 플러그인이 보내는 목표와 학습 목표가 같은 정의여야 한다 — 상자 중심의 치우침까지 같아진다.
- 시작·끝 각각 몇 장(`--offsets`)을 보고 사과마다 중심의 중앙값을 낸다(점수가 낮은 모델의 흔들림).
- 사라진 것 찾기: 시작 n 개, 끝 n−1 개면 나머지끼리 가장 가깝게 짝지었을 때 남는 하나. 나머지가 `--stay-px`
  넘게 움직였으면(팔이 건드림) 못 믿는다.
- 칸 → 화면: 종류가 판에 하나뿐인 에피소드의 (계획 칸, 검출 중심) 짝으로 호모그래피를 맞춘다(RANSAC). 이것으로
  mm/px 도 잰다 — 변환기가 커널 폭 ℓ(mm)을 화면 단위로 바꿀 때 쓴다.
- 못 찾으면: 계획 칸에 가장 가까운 그 종류(`plan_nearest`, `--max-plan-mm` 안) → 그것도 없으면 계획 칸의 화면
  자리(`plan_cell`). 목표 없는 에피소드(`empty_target` — 계획의 pick 에 칸이 없다)는 `valid: false`.
- 에피소드 ↔ 계획은 `meta/piper_scenario_log.jsonl` 의 `trial` 줄이다. 같은 파일의 제외 결정(`decision:
  exclude`)은 표에 `excluded` 로만 남긴다 — 학습에서 뺄지는 학습 명령(`--dataset.episodes`)이 정한다.

출력 표의 `source` 분포와 `plan_err_mm` 가 품질이다. `--sheet` 를 주면 고른 목표(자홍 원)·계획 칸(십자)을 그린
접촉 인화를 남긴다 — 사라진 것으로 못 정한 것 전부 + 정한 것 표본.
"""

from __future__ import annotations

import argparse
import collections
import glob
import itertools
import json
import math
import os
import random
import statistics
import sys
from pathlib import Path

import av
import cv2
import numpy as np
import pandas as pd

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
import main as P  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("dataset", help="org/name (LeRobot 캐시) 또는 경로")
ap.add_argument("out", help="쓸 목표 표 (.json)")
ap.add_argument("--cam", default="left_top", help="observation.images.<cam> — 위 카메라")
ap.add_argument("--model", default=None, help="검출 모델 (기본: plugin.yaml)")
ap.add_argument("--conf", type=float, default=None, help="검출 신뢰도 (기본: plugin.yaml)")
ap.add_argument("--roi", default=None, help="판 ROI x1,y1,x2,y2 (기본: plugin.yaml)")
ap.add_argument("--classes", default="red=red_apple,yellow=yellow_apple,small=small_apple",
                help="계획의 물체 id = 검출 클래스")
ap.add_argument("--offsets", default="1,3,5", help="에피소드 시작·끝에서 몇 프레임 안쪽을 볼지")
ap.add_argument("--stay-px", type=float, default=40.0, help="남은 사과가 이보다 움직였으면 사라진 것 찾기를 안 믿는다")
ap.add_argument("--max-plan-mm", type=float, default=40.0, help="계획 칸에서 이보다 멀면 표시한다")
# 20px ≈ 반 칸 — 사람이 놓은 자리가 칸 중심에서 중앙값 8mm 어긋나 8px 로는 짝의 절반이 바깥값이 된다(실측 2026-11-01)
ap.add_argument("--ransac-px", type=float, default=20.0, help="호모그래피 RANSAC 허용 오차(px)")
ap.add_argument("--device", default="cuda:0")
ap.add_argument("--sheet", default="", help="접촉 인화를 남길 폴더")
args = ap.parse_args()

from piper_plugin import detector  # noqa: E402
from piper_plugin.manifest import load_manifest  # noqa: E402

defaults = load_manifest(PLUGIN_DIR).defaults()
MODEL = P.resolve_model(args.model or str(defaults["model"]), PLUGIN_DIR)
CONF = float(args.conf if args.conf is not None else defaults["conf"])
ROI = P.parse_roi(args.roi or defaults["roi"])
CLASSES = dict(kv.split("=", 1) for kv in args.classes.split(","))
OFFSETS = [int(x) for x in args.offsets.split(",")]

D = Path(args.dataset).expanduser()
if not D.is_dir():
    D = Path(os.path.expanduser(f"~/.cache/huggingface/lerobot/{args.dataset}"))
CAM = f"observation.images.{args.cam}"
info = json.loads((D / "meta/info.json").read_text())
FPS = float(info["fps"])
H_IMG, W_IMG = info["features"][CAM]["shape"][:2]
scn = json.loads((D / "meta/piper_scenario.json").read_text())
trials = {e["id"]: e for e in scn["episodes"]}
board = scn["board"]
CELL_MM = float(board["cell_mm"])
trial_of: dict[int, str] = {}
excluded: dict[int, str] = {}
for rec in map(json.loads, (D / "meta/piper_scenario_log.jsonl").open()):
    if "trial" in rec:
        trial_of[int(rec["episode_index"])] = rec["trial"]
    elif rec.get("decision") == "exclude":
        excluded[int(rec["episode_index"])] = str(rec.get("reason") or "")
meta = pd.concat(pd.read_parquet(f) for f in sorted(glob.glob(str(D / "meta/episodes/*/*.parquet"))))
cams = json.loads((D / "meta/piper_cameras.json").read_text()).get("cameras", {}) \
    if (D / "meta/piper_cameras.json").is_file() else {}
cam_meta = cams.get(args.cam) or cams.get(args.cam.split("_", 1)[-1]) or {}
LABELS = sorted(set(CLASSES.values()))


def cell_mm(cell: str) -> tuple[float, float]:
    """`E4` → 칸 중심 (mm). 열 A.., 행 1.. — 방향은 호모그래피가 흡수한다."""
    col, row = ord(cell[0].upper()) - ord("A"), int(cell[1:]) - 1
    return (col + 0.5) * CELL_MM, (row + 0.5) * CELL_MM


def px_dist(a, b) -> float:
    return math.hypot((a[0] - b[0]) * W_IMG, (a[1] - b[1]) * H_IMG)


def merge(frames: list[dict]) -> dict[str, list[tuple[float, float, float]]]:
    """여러 장 → 개수가 가장 흔한 장을 기준으로, 사과마다 다른 장의 가까운 같은 종류 중심의 중앙값."""
    keys = [tuple(sorted((k, len(v)) for k, v in f.items())) for f in frames]
    ref = frames[keys.index(collections.Counter(keys).most_common(1)[0][0])]
    out = {}
    for label, apples in ref.items():
        merged = []
        for a in apples:
            pts = [a]
            for f in frames:
                if f is ref:
                    continue
                near = [b for b in f.get(label, []) if px_dist(a, b) < 25]
                if near:
                    pts.append(min(near, key=lambda b: px_dist(a, b)))
            merged.append((statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts),
                           max(p[2] for p in pts)))
        out[label] = merged
    return out


def vanished(start: list, end: list) -> int | None:
    """시작에 있고 끝에 없는 하나의 번호. 개수가 하나 줄지 않았거나 나머지가 움직였으면 None."""
    if len(start) != len(end) + 1:
        return None
    best = None
    for i in range(len(start)):
        rest = start[:i] + start[i + 1:]
        cost = min((max((px_dist(a, b) for a, b in zip(rest, perm)), default=0.0)
                    for perm in itertools.permutations(end)), default=0.0)
        if best is None or cost < best[0]:
            best = (cost, i)
    return best[1] if best[0] <= args.stay_px else None


# ── 1. 프레임 → 검출 ──
want: dict[tuple[int, int], dict[int, tuple[int, str, int]]] = collections.defaultdict(dict)
for _, row in meta.iterrows():
    ep = int(row["episode_index"])
    key = (int(row[f"videos/{CAM}/chunk_index"]), int(row[f"videos/{CAM}/file_index"]))
    f0 = round(float(row[f"videos/{CAM}/from_timestamp"]) * FPS)
    n = int(row["length"])
    for o in OFFSETS:
        want[key][f0 + min(o, n - 1)] = (ep, "start", o)
        want[key][f0 + max(0, n - 1 - o)] = (ep, "end", o)

model = detector.load(MODEL, device=args.device)
dets: dict[int, dict[str, list[dict]]] = collections.defaultdict(lambda: {"start": [], "end": []})
thumbs: dict[int, np.ndarray] = {}
for (chunk, fidx), wanted in sorted(want.items()):
    # cv2 는 AV1(이 데이터셋의 file-000)을 못 연다 — PyAV 로 읽는다 (autolabel.py 와 같다)
    with av.open(str(D / f"videos/{CAM}/chunk-{chunk:03d}/file-{fidx:03d}.mp4")) as cont:
        last = max(wanted)
        for i, fr in enumerate(cont.decode(video=0)):
            if i in wanted:
                ep, when, o = wanted[i]
                frame = fr.to_ndarray(format="bgr24")
                r = model.predict(frame, conf=CONF, device=args.device, verbose=False)[0]
                r.boxes = P.AppleSort._one_per_apple(r.boxes)
                dets[ep][when].append(P.board_apples(r, frame.shape[:2], ROI, LABELS))
                if when == "start" and o == OFFSETS[len(OFFSETS) // 2]:
                    thumbs[ep] = cv2.resize(frame, (W_IMG // 2, H_IMG // 2))
            if i >= last:
                break
    print(f"  file {chunk}/{fidx}: {len(wanted)} 프레임", file=sys.stderr)

episodes = sorted(int(e) for e in meta["episode_index"])
start = {e: merge(dets[e]["start"]) for e in episodes}
end = {e: merge(dets[e]["end"]) for e in episodes}

# ── 2. 칸 → 화면 호모그래피 (종류가 판에 하나뿐인 짝으로) ──
mm_pts, px_pts = [], []
for e in episodes:
    plan = trials.get(trial_of.get(e, ""))
    if plan is None:
        continue
    per = collections.Counter(item["obj"] for item in plan["layout"])
    for obj, n in per.items():
        lab = CLASSES.get(obj)
        if n == 1 and lab and len(start[e].get(lab, [])) == 1:
            cell = next(item["cell"] for item in plan["layout"] if item["obj"] == obj)
            u, v, _ = start[e][lab][0]
            mm_pts.append(cell_mm(cell))
            px_pts.append((u * W_IMG, v * H_IMG))
if len(mm_pts) < 8:
    sys.exit(f"칸↔화면 짝이 {len(mm_pts)}개뿐이다 — 호모그래피를 못 맞춘다")
Hm, inl = cv2.findHomography(np.float32(mm_pts), np.float32(px_pts), cv2.RANSAC, args.ransac_px)
inl = inl.ravel().astype(bool)
proj = cv2.perspectiveTransform(np.float32(mm_pts).reshape(-1, 1, 2), Hm).reshape(-1, 2)
res = np.linalg.norm(proj - np.float32(px_pts), axis=1)
rmse = float(np.sqrt((res[inl] ** 2).mean()))
Hinv = np.linalg.inv(Hm)
centre = np.float32([[[W_IMG * (ROI[0] + ROI[2]) / 2, H_IMG * (ROI[1] + ROI[3]) / 2]]])
pts = np.float32([[centre[0, 0] + d] for d in ([0, 0], [10, 0], [0, 10])])
mm = cv2.perspectiveTransform(pts, Hinv).reshape(-1, 2)
MM_PER_PX = float((np.linalg.norm(mm[1] - mm[0]) + np.linalg.norm(mm[2] - mm[0])) / 2 / 10)
print(f"호모그래피: 짝 {len(mm_pts)}개 중 inlier {inl.sum()}, RMSE {rmse:.1f}px, 판 가운데 {MM_PER_PX:.3f} mm/px",
      file=sys.stderr)


def plan_uv(cell: str) -> tuple[float, float]:
    x, y = cv2.perspectiveTransform(np.float32([[cell_mm(cell)]]), Hm).reshape(2)
    return float(x) / W_IMG, float(y) / H_IMG


# ── 3. 에피소드마다 목표 ──
out_eps, flagged = {}, []
for e in episodes:
    plan = trials.get(trial_of.get(e, ""))
    if plan is None:
        sys.exit(f"에피소드 {e} 의 계획(trial)이 로그에 없다 — 목표를 정할 근거가 없다")
    pick = plan["picks"][0]
    rec = {"obj": pick["obj"], "cell": pick.get("cell"), "variant": plan.get("variant"),
           "excluded": excluded.get(e, "") or False}
    lab = CLASSES[pick["obj"]]
    S, E = start[e].get(lab, []), end[e].get(lab, [])
    rec.update(n_start=len(S), n_end=len(E))
    if not pick.get("cell"):
        rec.update(valid=False, source="empty_target")
        if S:
            rec["note"] = "계획엔 그 종류가 없는데 시작 프레임에 보인다"
            flagged.append(e)
        out_eps[str(e)] = rec
        continue
    puv = plan_uv(pick["cell"])
    i = vanished(S, E)
    if i is not None:
        u, v, _ = S[i]
        source = "vanished"
    else:
        near = [a for a in S if px_dist(a, puv) * MM_PER_PX <= args.max_plan_mm]
        if near:
            u, v, _ = min(near, key=lambda a: px_dist(a, puv))
            source = "plan_nearest"
        else:
            u, v = puv
            source = "plan_cell"
    err = px_dist((u, v), puv) * MM_PER_PX
    rec.update(valid=True, u=round(u, 5), v=round(v, 5), source=source, plan_err_mm=round(err, 1),
               plan_uv=[round(puv[0], 5), round(puv[1], 5)])
    if source != "vanished" or err > args.max_plan_mm:
        flagged.append(e)
    out_eps[str(e)] = rec

src = collections.Counter(r["source"] for r in out_eps.values())
errs = [r["plan_err_mm"] for r in out_eps.values() if r.get("source") == "vanished"]
doc = {
    "version": 1,
    "tool": "plugins/apple-sort/tools/targets.py",
    "dataset": str(D),
    "camera": CAM,
    "camera_id": cam_meta.get("id", ""),
    "frame_size": [int(W_IMG), int(H_IMG)],
    "mm_per_px": round(MM_PER_PX, 4),
    "board": {"homography_mm_to_px": np.round(Hm, 8).tolist(), "cell_mm": CELL_MM, "pairs": len(mm_pts),
              "inliers": int(inl.sum()), "rmse_px": round(rmse, 2)},
    "detector": {"model": Path(MODEL).name, "conf": CONF, "roi": list(ROI), "offsets": OFFSETS,
                 "classes": CLASSES},
    "summary": {"sources": dict(src), "flagged": flagged,
                "vanished_plan_err_mm": ({"median": round(statistics.median(errs), 1),
                                          "p95": round(float(np.percentile(errs, 95)), 1),
                                          "max": round(max(errs), 1)} if errs else None)},
    "episodes": out_eps,
}
Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
print(f"목표 표 → {args.out}", file=sys.stderr)
print(f"  출처: {dict(src)}", file=sys.stderr)
print(f"  사라진 것으로 정한 목표의 계획 칸 거리(mm): {doc['summary']['vanished_plan_err_mm']}", file=sys.stderr)
print(f"  표시한 에피소드 {len(flagged)}개: {flagged[:20]}{' …' if len(flagged) > 20 else ''}", file=sys.stderr)

# ── 4. 접촉 인화 ──
if args.sheet:
    sheet = Path(args.sheet)
    sheet.mkdir(parents=True, exist_ok=True)
    colors = {"red_apple": (40, 40, 230), "yellow_apple": (0, 200, 230), "small_apple": (230, 120, 30)}
    ok = [e for e in episodes if e not in flagged and out_eps[str(e)]["valid"]]
    random.Random(0).shuffle(ok)
    for name, sel in (("flagged", flagged), ("sample", sorted(ok[:48]))):
        tiles = []
        for e in sel:
            img = thumbs.get(e)
            if img is None:
                continue
            img = img.copy()
            r = out_eps[str(e)]
            for lab, apples in start[e].items():
                for u, v, _ in apples:
                    cv2.circle(img, (int(u * W_IMG / 2), int(v * H_IMG / 2)), 4, colors.get(lab, (255, 255, 255)), -1)
            if r.get("valid"):
                cv2.circle(img, (int(r["u"] * W_IMG / 2), int(r["v"] * H_IMG / 2)), 12, (255, 0, 255), 2)
                pu, pv = r["plan_uv"]
                cv2.drawMarker(img, (int(pu * W_IMG / 2), int(pv * H_IMG / 2)), (0, 255, 0), cv2.MARKER_CROSS, 12, 2)
            text = f"ep{e} {r['obj']} {r.get('source')} {r.get('plan_err_mm', '')}"
            cv2.putText(img, text, (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(img, text, (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
            tiles.append(img)
        for s in range(0, len(tiles), 24):
            part = tiles[s:s + 24]
            part += [np.zeros_like(part[0])] * (-len(part) % 6)
            grid = np.vstack([np.hstack(part[k:k + 6]) for k in range(0, len(part), 6)])
            cv2.imwrite(str(sheet / f"targets_{name}_{s // 24}.jpg"), grid, [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(f"  접촉 인화 → {sheet}", file=sys.stderr)
