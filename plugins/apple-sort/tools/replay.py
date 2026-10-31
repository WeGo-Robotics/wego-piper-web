"""녹화 에피소드를 실시간으로 재생해 apple-sort 미션 루프를 드라이런으로 돌린다 — 로봇·카메라 없이.

위 카메라 프레임과 같은 시각의 observation.state(왼팔 0~6, 오른팔 7~13)를 가짜 카메라·팔로 준다.
녹화의 지시문은 사람이 고른 순서라 플러그인 판단과 다를 수 있다 — 보는 것은 **완료 시각이
에피소드 끝(팔 복귀 뒤)인가**와 개수 변화다. 2026-10-31 ep0~7: 완료 36.5/55.6/74.1 s
(에피소드 끝 37.0/56.1/73.7 s), 1라운드를 다 옮긴 88.9 s 에 판 빔 → 종료.

    python plugins/apple-sort/tools/replay.py plugins/apple-sort <첫 에피소드> <끝 에피소드(포함)> \
        [--model NAME] [--conf 0.08]
출력: 재생 위치가 붙은 판단·완료 로그, 마지막에 에피소드 경계 대 완료 시각 표.
"""
import argparse
import glob
import json
import os
import sys
import threading
import time

import av
import cv2
import numpy as np
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("plugin_dir")
ap.add_argument("ep0", type=int)
ap.add_argument("ep1", type=int)
ap.add_argument("--model", default=None)
ap.add_argument("--conf", type=float, default=None)
ap.add_argument("--gap", type=float, default=2.0, help="에피소드 사이에 마지막 프레임을 유지할 초 (사람 리셋 대신)")
args = ap.parse_args()
sys.path.insert(0, args.plugin_dir)

D = os.path.expanduser("~/.cache/huggingface/lerobot/wego-hansu/dualarm_sort_apple_stage")
CAM = "observation.images.left_top"
FPS = 15
meta = pd.concat(pd.read_parquet(f) for f in sorted(glob.glob(f"{D}/meta/episodes/*/*.parquet"))).set_index("episode_index")
data = pd.concat(pd.read_parquet(f, columns=["episode_index", "frame_index", "observation.state"])
                 for f in sorted(glob.glob(f"{D}/data/*/*.parquet")))

# ── 재생 타임라인: (jpeg, state, ep, frame) ──
timeline = []
bounds = []                     # (ep, 시작 인덱스, 끝 인덱스, 지시문)
for ep in range(args.ep0, args.ep1 + 1):
    row = meta.loc[ep]
    path = f"{D}/videos/{CAM}/chunk-{int(row[f'videos/{CAM}/chunk_index']):03d}/file-{int(row[f'videos/{CAM}/file_index']):03d}.mp4"
    f0 = round(float(row[f"videos/{CAM}/from_timestamp"]) * FPS)
    n = int(row["length"])
    states = np.stack(data[data.episode_index == ep].sort_values("frame_index")["observation.state"].to_numpy())
    frames = []
    with av.open(path) as c:
        for i, fr in enumerate(c.decode(video=0)):
            if i >= f0 + n:
                break
            if i >= f0:
                ok, jpg = cv2.imencode(".jpg", fr.to_ndarray(format="bgr24"), [cv2.IMWRITE_JPEG_QUALITY, 92])
                frames.append(jpg)
    start = len(timeline)
    for k, jpg in enumerate(frames):
        timeline.append((jpg, states[k], ep, k))
    for _ in range(int(args.gap * FPS)):
        timeline.append((frames[-1], states[len(frames) - 1], ep, len(frames) - 1))
    bounds.append((ep, start, start + len(frames) - 1, list(row["tasks"])[0]))
print(f"재생 {len(timeline)} 프레임 ({len(timeline) / FPS:.0f} s), 에피소드 {args.ep0}~{args.ep1}", flush=True)

T0 = [None]


def now_idx() -> int:
    if T0[0] is None:
        T0[0] = time.monotonic()
    return int((time.monotonic() - T0[0]) * FPS)


def where() -> str:
    i = min(now_idx(), len(timeline) - 1)
    _, _, ep, k = timeline[i]
    return f"[t={i / FPS:6.1f}s ep{ep} f{k}]"


class FakeCamera:
    def __init__(self):
        self.last = -1

    def read_new(self, timeout_s=1.0):
        deadline = time.monotonic() + timeout_s
        while True:
            i = now_idx()
            if i >= len(timeline):
                return None
            if i != self.last:
                self.last = i
                return cv2.imdecode(timeline[i][0], cv2.IMREAD_COLOR), i, time.time_ns()
            if time.monotonic() > deadline:
                return None
            time.sleep(0.005)

    def close(self):
        pass


from piper_plugin.arm import ArmState  # noqa: E402

NAMES = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "gripper"]


class FakeArm:
    def __init__(self, off):
        self.off = off

    def read(self):
        i = min(now_idx(), len(timeline) - 1)
        s = timeline[i][1]
        return ArmState(joints={n: float(s[self.off + j]) for j, n in enumerate(NAMES)}, err_code=0, ctrl_mode=0,
                        seq=i, can_wall_ns=time.time_ns())


import main as plugin_main  # noqa: E402
from piper_plugin.env import Env  # noqa: E402
from piper_plugin.manifest import load_manifest  # noqa: E402
from pathlib import Path  # noqa: E402

m = load_manifest(Path(args.plugin_dir))
config = {**m.defaults(), "dry_run": True}
if args.model:
    config["model"] = args.model
if args.conf is not None:
    config["conf"] = args.conf
env = Env.from_environ({"PIPER_PLUGIN_NAME": "apple-sort-replay", "PIPER_PLUGIN_DIR": args.plugin_dir,
                        "PIPER_PLUGIN_RUNTIME_DIR": "/tmp/apple_probe/rt",
                        "PIPER_PLUGIN_CONFIG": json.dumps(config),
                        "PIPER_PLUGIN_CAMERAS": json.dumps({"top": "replay"}),
                        "PIPER_PLUGIN_ARMS": json.dumps({"left": "replay_l", "right": "replay_r"})})

cam = FakeCamera()
arms = {"left": FakeArm(0), "right": FakeArm(7)}
events = []


class Replay(plugin_main.AppleSort):
    def camera(self, key):
        return cam

    def arm(self, key):
        return arms[key]

    def _apply(self, d):
        self.log.info("%s 지시문 전달: %s", where(), d.text)
        events.append(("task", now_idx(), d.text))
        return super()._apply(d)

    def record(self, ctx, result):
        rec = super().record(ctx, result)
        kind = "done" if result.get("done") else ("skip" if result.get("skipped") else "timeout")
        events.append((kind, now_idx(), rec))
        self.log.info("%s 회차 끝 %s %s", where(), kind, rec)
        return rec

    def decide(self, ctx):
        d = super().decide(ctx)
        if d.kind == "skip":
            self.log.info("%s 건너뜀: %s", where(), d.reason)
        return d


p = Replay(env)
end_s = len(timeline) / FPS + 3


def stopper():
    while T0[0] is None:
        time.sleep(0.1)
    while time.monotonic() - T0[0] < end_s and not p.stopping:
        time.sleep(0.2)
    p._stopping, p._stop_reason = True, "재생 끝"


threading.Thread(target=stopper, daemon=True).start()
p.run()

print("\n에피소드 경계 (재생 시각) — 데이터셋 지시문")
for ep, a, b, task in bounds:
    print(f"  ep{ep}: {a / FPS:6.1f}s ~ {b / FPS:6.1f}s  {task}")
print("플러그인 사건")
for kind, i, what in events:
    print(f"  {i / FPS:6.1f}s {kind:7s} {what}")
