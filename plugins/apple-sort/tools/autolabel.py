"""에피소드 위 카메라 프레임 → 사과 박스 자동 라벨 → 시나리오 계획 개수로 검증 → 검출 데이터셋.

검출 데이터셋 `dualarm_sort_apple`(2026-10-31, 600장 중 587장 라벨)을 이걸로 만들었다. 새 에피소드를
녹화하면 다시 돌려 새 검출 데이터셋을 만들고 검출 학습 화면에서 학습한다.

프레임: 에피소드마다 시작·끝 근처 한 장씩 (팔이 쉬는 장면 — 플러그인이 세는 때와 같다).
박스:   COCO 검출(apple·orange·sports ball — 큰 사과 상자가 정확하다) → 클래스 무관 겹침 정리 →
        붙은 둘을 감싼 큰 상자 버림 → 상자 안 색으로 빨강/노랑, 짧은 변 < `small_px` 이면 작은 사과.
        COCO 는 작은 사과(지름 ~35 px)를 못 잡는다 — 색 덩어리(board_color)로 보탠다.
라벨 규칙: **작업판(ROI) 위 사과만.** 상자 안 사과는 배경이다 — 벽에 가려 절반쯤은 못 잡아서,
        달면 같은 그림이 프레임마다 라벨됐다 안 됐다 한다.
검증:   판 위 개수가 계획(layout − 성공한 picks, `empty_target` 의 pick 은 cell 이 없다)과 같아야
        라벨을 단다. 다르면 라벨 없이(미라벨) 올린다 — 검출 학습 화면에서 사람이 단다.
        2026-10-31: 600 중 587 일치. 불일치 13 은 팔이 사과를 가리거나 든 장면.

    python plugins/apple-sort/tools/autolabel.py <작업 폴더> [--name 검출데이터셋] [--dry]
        [--source wego-hansu/dualarm_sort_apple_stage] [--api http://127.0.0.1:8000]
작업 폴더/frames/*.jpg · labels.jsonl · sheet_*.jpg (눈으로 볼 접촉 인화) 가 남는다.
"""

import argparse
import collections
import glob
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import av
import cv2
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_color as board  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("work_dir")
ap.add_argument("--name", default="", help="만들 검출 데이터셋 이름 (없으면 --dry 와 같다)")
ap.add_argument("--dry", action="store_true", help="올리지 않고 작업 폴더만")
ap.add_argument("--source", default="wego-hansu/dualarm_sort_apple_stage")
ap.add_argument("--cam", default="left_top", help="observation.images.<cam> — 위 카메라")
ap.add_argument("--api", default="http://127.0.0.1:8000")
ap.add_argument("--conf", type=float, default=0.3, help="COCO 검출 신뢰도")
ap.add_argument("--offset", type=int, default=3, help="에피소드 시작·끝에서 몇 프레임 안쪽")
args = ap.parse_args()

D = Path(os.path.expanduser(f"~/.cache/huggingface/lerobot/{args.source}"))
CAM = f"observation.images.{args.cam}"
CLASSES = ["red_apple", "yellow_apple", "small_apple"]
CLS = {"red": 0, "yellow": 1, "small": 2}
FRUIT = {"apple", "orange", "sports ball"}
spec = board.BoardSpec()
work = Path(args.work_dir)
(work / "frames").mkdir(parents=True, exist_ok=True)

scn = json.loads((D / "meta/piper_scenario.json").read_text())
trials = {e["id"]: e for e in scn["episodes"]}
by_ep = {r["episode_index"]: r for r in map(json.loads, (D / "meta/piper_scenario_log.jsonl").open())}
meta = pd.concat(pd.read_parquet(f) for f in sorted(glob.glob(str(D / "meta/episodes/*/*.parquet"))))
fps = float(json.loads((D / "meta/info.json").read_text())["fps"])


def expected(ep: int, when: str) -> dict | None:
    """판 위 개수 정답. 계획에 없는 에피소드면 None."""
    rec = by_ep.get(ep)
    if rec is None or rec["trial"] not in trials:
        return None
    t = trials[rec["trial"]]
    on = collections.Counter(o["obj"] for o in t["layout"])
    if when == "end" and rec["outcome"] == "success":
        for p in t["picks"]:
            if p.get("cell"):
                on[p["obj"]] -= 1
    return {k: on[k] for k in CLS}


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def color_of(hsv, box) -> tuple[str | None, float]:
    """상자 안 타원의 빨강·노랑 픽셀 → (많은 쪽, 비율)."""
    h, w = hsv.shape[:2]
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 3 or y2 - y1 < 3:
        return None, 0.0
    roi = hsv[y1:y2, x1:x2]
    m = np.zeros(roi.shape[:2], np.uint8)
    cv2.ellipse(m, ((x2 - x1) // 2, (y2 - y1) // 2), (max(1, (x2 - x1) * 2 // 5), max(1, (y2 - y1) * 2 // 5)),
                0, 0, 360, 255, -1)
    px = roi[m > 0]
    vivid = (px[:, 1] >= spec.s_weak) & (px[:, 2] >= spec.v_min)
    red = int((vivid & board._hue_mask(px[:, 0], *spec.red_hue)).sum())
    yel = int((vivid & board._hue_mask(px[:, 0], *spec.yellow_hue)).sum())
    return ("red" if red >= yel else "yellow"), max(red, yel) / max(1, len(px))


def on_board(xyxy, w, h) -> bool:
    x1, y1, x2, y2 = spec.roi
    cx, cy = (xyxy[0] + xyxy[2]) / 2 / w, (xyxy[1] + xyxy[3]) / 2 / h
    return x1 <= cx <= x2 and y1 <= cy <= y2


def label_frame(model, frame) -> list[dict]:
    """판 위 사과 박스 `{label, xyxy, conf, src}`."""
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    r = model.predict(frame, conf=args.conf, device="cuda:0", verbose=False)[0]
    cands = sorted(((c, b) for b, c, k in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.int().tolist())
                    if r.names[k] in FRUIT), reverse=True)
    kept: list[tuple[float, list]] = []
    for c, b in cands:                                   # 한 사과의 apple·orange 이중 상자
        if all(iou(b, kb) < 0.5 for _, kb in kept):
            kept.append((c, b))

    def centers_in(b) -> int:
        return sum(1 for _, o in kept if o is not b and b[0] <= (o[0] + o[2]) / 2 <= b[2] and b[1] <= (o[1] + o[3]) / 2 <= b[3])

    boxes = []
    for c, b in kept:
        if centers_in(b) >= 2:                           # 붙은 둘을 감싼 큰 상자
            continue
        color, frac = color_of(hsv, b)
        if color is None or frac < 0.25:                 # 사과 색이 아니다
            continue
        short = min(b[2] - b[0], b[3] - b[1])
        boxes.append({"label": "small" if short < spec.small_px else color, "xyxy": [round(v, 1) for v in b],
                      "conf": round(c, 3), "src": "det"})
    for a in board.find_apples(frame, spec):             # COCO 가 못 잡는 작은 사과
        if a["label"] != "small":
            continue
        cx, cy = a["center"]
        if any(bb["xyxy"][0] <= cx <= bb["xyxy"][2] and bb["xyxy"][1] <= cy <= bb["xyxy"][3] for bb in boxes):
            continue
        rr = a["diameter"] / 2 * 1.15                    # 색 덩어리 반지름은 안쪽 원 — 테두리는 조금 크다
        boxes.append({"label": "small", "xyxy": [round(max(0, cx - rr), 1), round(max(0, cy - rr), 1),
                                                  round(min(w, cx + rr), 1), round(min(h, cy + rr), 1)],
                      "conf": 1.0, "src": "color"})
    return [b for b in boxes if on_board(b["xyxy"], w, h)]


def run_labeling() -> list[dict]:
    from piper_plugin import detector

    model = detector.load("PekingU/rtdetr_v2_r18vd", device="cuda:0")
    want = collections.defaultdict(dict)                 # (chunk, file) → {파일 안 프레임: (ep, when, 에피소드 안 프레임)}
    for _, row in meta.iterrows():
        ep = int(row["episode_index"])
        key = (int(row[f"videos/{CAM}/chunk_index"]), int(row[f"videos/{CAM}/file_index"]))
        f0 = round(float(row[f"videos/{CAM}/from_timestamp"]) * fps)
        n = int(row["length"])
        want[key][f0 + args.offset] = (ep, "start", args.offset)
        want[key][f0 + n - 1 - args.offset] = (ep, "end", n - 1 - args.offset)
    recs = []
    sheets = {True: [], False: []}
    colors = {"red": (40, 40, 230), "yellow": (0, 200, 230), "small": (230, 120, 30)}
    for (chunk, fidx), wanted in sorted(want.items()):
        # cv2 는 AV1(이 데이터셋의 file-000)을 못 연다 — PyAV(dav1d)로 읽는다
        with av.open(str(D / f"videos/{CAM}/chunk-{chunk:03d}/file-{fidx:03d}.mp4")) as cont:
            last = max(wanted)
            for i, fr in enumerate(cont.decode(video=0)):
                if i in wanted:
                    ep, when, local = wanted[i]
                    exp = expected(ep, when)
                    if exp is not None:
                        frame = fr.to_ndarray(format="bgr24")
                        h, w = frame.shape[:2]
                        boxes = label_frame(model, frame)
                        got = {k: sum(1 for b in boxes if b["label"] == k) for k in CLS}
                        fname = f"ep{ep:03d}_{when}.jpg"
                        cv2.imwrite(str(work / "frames" / fname), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                        recs.append({"file": fname, "episode": ep, "when": when, "frame": local,
                                     "t": round((local + 0.5) / fps, 3), "ok": got == exp, "boxes": boxes,
                                     "exp": exp, "got": got, "size": [w, h]})
                        if len(sheets[got == exp]) < 48:
                            img = frame.copy()
                            for b in boxes:
                                x1, y1, x2, y2 = (int(v) for v in b["xyxy"])
                                cv2.rectangle(img, (x1, y1), (x2, y2), colors[b["label"]], 2)
                            cv2.putText(img, f"ep{ep} {when}", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
                            sheets[got == exp].append(cv2.resize(img, (320, 240)))
                if i >= last:
                    break
    with (work / "labels.jsonl").open("w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    for ok, imgs in sheets.items():
        for s in range(0, len(imgs), 24):
            tiles = imgs[s:s + 24]
            tiles += [np.zeros_like(tiles[0])] * (-len(tiles) % 6)
            grid = np.vstack([np.hstack(tiles[k:k + 6]) for k in range(0, len(tiles), 6)])
            cv2.imwrite(str(work / f"sheet_{'ok' if ok else 'bad'}_{s // 24}.jpg"), grid, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return recs


def call(method, path, body=None, data=None, ctype="application/json"):
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(args.api + path, data=data, method=method, headers={"content-type": ctype})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def upload(recs: list[dict]) -> None:
    if args.name in {d["name"] for d in call("GET", "/api/detector/datasets")["datasets"]}:
        raise SystemExit(f"검출 데이터셋 {args.name} 이 이미 있다 — 덮어쓰지 않는다")
    call("POST", "/api/detector/datasets", {"name": args.name, "classes": CLASSES})
    n_lab = n_box = 0
    for rec in recs:
        q = urllib.parse.urlencode({"type": "episode", "dataset": args.source, "episode": rec["episode"],
                                    "cam": args.cam, "t": rec["t"]})
        fname = call("POST", f"/api/detector/datasets/{args.name}/images?{q}",
                     data=(work / "frames" / rec["file"]).read_bytes(), ctype="image/jpeg")["file"]
        if not rec["ok"]:
            continue
        w, h = rec["size"]
        boxes = []
        for b in rec["boxes"]:
            x1, x2 = max(0.0, b["xyxy"][0] / w), min(1.0, b["xyxy"][2] / w)
            y1, y2 = max(0.0, b["xyxy"][1] / h), min(1.0, b["xyxy"][3] / h)
            boxes.append({"cls": CLS[b["label"]], "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1})
        call("PUT", f"/api/detector/datasets/{args.name}/labels/{fname}", {"boxes": boxes})
        n_lab += 1
        n_box += len(boxes)
    print(f"{args.name}: 이미지 {len(recs)}장, 라벨 {n_lab}장(박스 {n_box}개), 미라벨 {len(recs) - n_lab}장")


recs = run_labeling()
print(f"프레임 {len(recs)}: 판 위 개수 일치 {sum(r['ok'] for r in recs)} → 라벨, 나머지 미라벨")
if args.name and not args.dry:
    upload(recs)
