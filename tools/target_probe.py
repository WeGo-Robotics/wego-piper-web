"""목표 입력 체크포인트가 목표를 **쓰는지** 실기 전에 본다 (feature/target-conditioning.md §평가).

    python tools/target_probe.py <체크포인트> [--dataset wego-hansu/dualarm_sort_apple_target_stage]
        [--episodes 24] [--shift-mm 80] [--seeds 3] [--out probe.json]

같은 관측(카메라 세 장 + 로봇 state)에 목표만 바꿔 넣고 청크를 뽑는다 — 참 목표 T0, 판 위에서 `--shift-mm`
옮긴 목표 T1, 목표 없음. 디노이즈 잡음은 셋이 같다(`--seeds` 벌 평균). 팔(지시문의 left/right)의 말단 궤적을
FK 로 펴서 잰다:

- **민감도** = 청크 끝 말단이 T0 와 T1 사이에서 움직인 거리 / 목표를 옮긴 거리. 목표를 무시하는 정책은 0 근처다.
- **시연 오차** = 청크 동안 말단이 시연(데이터셋 액션)에서 떨어진 평균 거리 — T0 가 T1 보다 작아야 목표를 따르는 것.
- **목표 없음 이동** = 목표 없음일 때 청크 끝 말단이 지금 자리에서 간 거리 — `empty_target` 을 배웠으면 작다.

두 순간에서 본다: 에피소드 시작(원위치, 출발 전)과 APPROACH 한가운데(이미 대상으로 가는 중 — 목표를 바꾸면
방향을 트는가). 둘째는 페이즈 라벨(`meta/phase_labels.json`)이 있어야 한다.

로봇·게이트웨이 없이 돈다. 관측은 wrapper 와 같은 길이다 — 같은 전처리기, 같은 `TargetInput.extend`.
영상은 PyAV 로 읽는다(셸에서 torchcodec 이 FFmpeg 를 못 여는 기계가 있다).
"""

from __future__ import annotations

# ── lerobot.policies.__init__ 실행 방지 — wrapper 와 같은 우회 (groot 가 3.13 dataclass 와 안 맞는다) ──
import importlib as _il
import os as _os
import sys
import types as _types

_lerobot = _il.import_module("lerobot")
_pkg = _types.ModuleType("lerobot.policies")
_pkg.__path__ = [_os.path.join(_os.path.dirname(_lerobot.__file__), "policies")]
_pkg.__package__ = "lerobot.policies"
sys.modules["lerobot.policies"] = _pkg

import argparse  # noqa: E402
import glob  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import av  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "wrapper"))
from jump_repair import piper_fk  # noqa: E402
from policy_registry import policy_imports  # noqa: E402
from target_input import TargetInput  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("checkpoint", help="pretrained_model 폴더")
ap.add_argument("--dataset", default="wego-hansu/dualarm_sort_apple_target_stage")
ap.add_argument("--episodes", type=int, default=24, help="볼 에피소드 수 (목표 있는 것 중 고르게)")
ap.add_argument("--shift-mm", type=float, default=80.0, help="T1 을 판 위에서 옮기는 거리 (사과 하나 ≈ 80mm)")
ap.add_argument("--seeds", type=int, default=3, help="디노이즈 잡음 벌 수 (셋이 같은 잡음을 쓴다)")
ap.add_argument("--device", default="cuda")
ap.add_argument("--out", default="", help="결과 JSON")
args = ap.parse_args()

import torch  # noqa: E402
from lerobot.utils.import_utils import register_third_party_plugins  # noqa: E402

register_third_party_plugins()

D = Path(args.dataset).expanduser()
if not D.is_dir():
    D = Path(_os.path.expanduser(f"~/.cache/huggingface/lerobot/{args.dataset}"))
info = json.loads((D / "meta/info.json").read_text())
FPS = float(info["fps"])
side = json.loads((D / "meta/piper_target.json").read_text())
spec = side["spec"]
src_doc = json.loads((D / "meta/piper_target_source.json").read_text())
Hm = np.asarray(src_doc["board"]["homography_mm_to_px"], dtype=np.float64)
W_IMG, H_IMG = spec["frame"]
robot_names = spec["state_names"]
cams = [k for k, v in info["features"].items() if v.get("dtype") == "video"]
tasks = pd.read_parquet(D / "meta/tasks.parquet")
task_of = {int(i): t for t, i in zip(tasks.index, tasks["task_index"])} if "task_index" in tasks.columns \
    else {int(i): t for i, t in enumerate(tasks.index)}
meta = pd.concat(pd.read_parquet(f) for f in sorted(glob.glob(str(D / "meta/episodes/*/*.parquet")))).set_index("episode_index")
data = pd.concat(pd.read_parquet(f, columns=["episode_index", "frame_index", "observation.state", "action", "task_index"])
                 for f in sorted(glob.glob(str(D / "data/*/*.parquet"))))
phases = json.loads((D / "meta/phase_labels.json").read_text()) if (D / "meta/phase_labels.json").is_file() else None

# ── 정책 — wrapper 와 같은 로드 ──
ckpt = Path(args.checkpoint)
cfg_json = json.loads((ckpt / "config.json").read_text())
model_mod, model_cls, _, _ = policy_imports()[cfg_json["type"]]
policy = getattr(importlib.import_module(model_mod), model_cls).from_pretrained(str(ckpt))
policy.eval().to(args.device)
policy.requires_grad_(False)
from lerobot.policies.factory import make_pre_post_processors  # noqa: E402

for _m in ("lerobot.policies.smolvla.processor_smolvla",):
    try:
        importlib.import_module(_m)
    except Exception:
        pass
pre, post = make_pre_post_processors(policy.config, pretrained_path=str(ckpt),
                                     preprocessor_overrides={"device_processor": {"device": args.device}},
                                     postprocessor_overrides={"device_processor": {"device": "cpu"}})
expected = int(cfg_json["input_features"]["observation.state"]["shape"][0])
tin = TargetInput(spec, robot_names, expected)
fk = piper_fk()
if fk is None:
    sys.exit("PIPER 기구학을 못 읽는다 (piper_robot) — 말단 거리를 잴 수 없다")
chunk = int(getattr(policy.config, "chunk_size", 50))
max_a = int(getattr(policy.config, "max_action_dim", 32))
tr_eps = set(json.loads((ckpt / "train_config.json").read_text()).get("dataset", {}).get("episodes") or []) \
    if (ckpt / "train_config.json").is_file() else set()


def frame_rgb(cam: str, ep: int, k: int) -> np.ndarray:
    row = meta.loc[ep]
    f = D / f"videos/{cam}/chunk-{int(row[f'videos/{cam}/chunk_index']):03d}/file-{int(row[f'videos/{cam}/file_index']):03d}.mp4"
    want = round(float(row[f"videos/{cam}/from_timestamp"]) * FPS) + k
    with av.open(str(f)) as cont:
        stream = cont.streams.video[0]
        # 키프레임으로 뛰고 앞으로 푼다 — 처음부터 풀면 파일 하나에 수만 장이다
        ts = int(max(0.0, (want / FPS) - 2.0) / stream.time_base)
        cont.seek(ts, stream=stream, backward=True, any_frame=False)
        for fr in cont.decode(video=0):
            idx = round(float(fr.pts * stream.time_base) * FPS)
            if idx >= want:
                return fr.to_ndarray(format="rgb24")
    raise RuntimeError(f"{cam} ep{ep} 프레임 {k} 를 못 찾았다")


def shifted(uv: tuple[float, float], mm: float) -> tuple[float, float]:
    """판 열 방향으로 `mm` 옮긴 화면 자리 — 판 밖으로 나가면 반대로."""
    import cv2

    Hinv = np.linalg.inv(Hm)
    p = cv2.perspectiveTransform(np.float64([[[uv[0] * W_IMG, uv[1] * H_IMG]]]), Hinv).reshape(2)
    cols = float(src_doc["board"]["cell_mm"]) * 10
    for d in (mm, -mm):
        q = p + np.array([d, 0.0])
        if 0 <= q[0] <= cols:
            x, y = cv2.perspectiveTransform(np.float64([[q]]), Hm).reshape(2)
            return float(x) / W_IMG, float(y) / H_IMG
    return uv


def ee(q14: np.ndarray, arm: str) -> np.ndarray:
    """`(T, 14)` 또는 `(14,)` 관절 → 그 팔 말단 `(T, 3)` mm."""
    off = 0 if arm == "left" else 7
    return fk(np.atleast_2d(np.asarray(q14, dtype=np.float64))[:, off:off + 6])


def chunk_for(images: dict, state14: np.ndarray, task: str, target, noise) -> np.ndarray:
    obs = {k: torch.tensor(v).permute(2, 0, 1).float() / 255.0 for k, v in images.items()}
    obs["observation.state"] = torch.tensor(tin.extend(state14, target))
    obs["task"] = task
    batch = pre(obs)
    policy.reset()
    with torch.no_grad():
        a = policy.predict_action_chunk(batch, noise=noise)
    a = a[0] if a.ndim == 3 else a
    return np.stack([post(a[i].unsqueeze(0)).squeeze(0).cpu().numpy() for i in range(a.shape[0])])


def probe(ep: int, k: int, label: str) -> dict:
    rows = data[data.episode_index == ep].sort_values("frame_index")
    k = min(k, len(rows) - 1)
    state14 = np.asarray(rows.iloc[k]["observation.state"], dtype=np.float32)[:tin.n]
    task = task_of[int(rows.iloc[k]["task_index"])]
    arm = "left" if "left arm" in task else "right"
    demo = np.stack(rows["action"].to_numpy()[k:k + chunk]).astype(np.float64)
    images = {c: frame_rgb(c, ep, k) for c in cams}
    t0 = tuple(side["targets"][str(ep)])
    t1 = shifted(t0, args.shift_mm)
    now = ee(state14, arm)[0]
    res = {"ep": ep, "frame": k, "when": label, "arm": arm, "trained_on": ep in tr_eps, "t0": t0, "t1": t1}
    acc = {"sens": [], "err0": [], "err1": [], "none_move": [], "t0_move": []}
    for s in range(args.seeds):
        g = torch.Generator(device=args.device).manual_seed(1000 + s)
        noise = torch.randn((1, chunk, max_a), generator=g, device=args.device)
        e0 = ee(chunk_for(images, state14, task, t0, noise), arm)
        e1 = ee(chunk_for(images, state14, task, t1, noise), arm)
        n = len(demo)
        acc["sens"].append(float(np.linalg.norm(e0[-1] - e1[-1])) / args.shift_mm)
        acc["err0"].append(float(np.linalg.norm(e0[:n] - ee(demo, arm), axis=1).mean()))
        acc["err1"].append(float(np.linalg.norm(e1[:n] - ee(demo, arm), axis=1).mean()))
        acc["t0_move"].append(float(np.linalg.norm(e0[-1] - now)))
        if spec["names"][0] == "target.valid":
            en = ee(chunk_for(images, state14, task, None, noise), arm)
            acc["none_move"].append(float(np.linalg.norm(en[-1] - now)))
    for key, vals in acc.items():
        res[key] = round(float(np.mean(vals)), 3) if vals else None
    return res


valid = sorted(int(e) for e, t in side["targets"].items() if t is not None)
pick = [valid[i] for i in np.linspace(0, len(valid) - 1, min(args.episodes, len(valid))).round().astype(int)]
results = []
for ep in pick:
    frames_at = [(1, "start")]
    if phases is not None:
        seg = [s for s in phases["episodes"].get(str(ep), {}).get("segments", []) if s[2] == 1]   # APPROACH
        if seg:
            frames_at.append(((seg[0][0] + seg[0][1]) // 2, "approach_mid"))
    for k, when in frames_at:
        r = probe(ep, k, when)
        results.append(r)
        print(f"ep{ep:3d} {r['when']:12s} {r['arm']:5s} 민감도 {r['sens']:.2f}  시연오차 T0 {r['err0']:6.1f} / "
              f"T1 {r['err1']:6.1f}mm  목표없음 이동 {r['none_move']}", flush=True)

summary = {}
for when in ("start", "approach_mid"):
    rs = [r for r in results if r["when"] == when]
    if not rs:
        continue
    summary[when] = {
        "n": len(rs),
        "sensitivity_median": round(float(np.median([r["sens"] for r in rs])), 3),
        "demo_err_t0_median_mm": round(float(np.median([r["err0"] for r in rs])), 1),
        "demo_err_t1_median_mm": round(float(np.median([r["err1"] for r in rs])), 1),
        "t0_closer_to_demo": f"{sum(r['err0'] < r['err1'] for r in rs)}/{len(rs)}",
        "none_move_median_mm": (round(float(np.median([r["none_move"] for r in rs])), 1)
                                if rs[0]["none_move"] is not None else None),
        "t0_move_median_mm": round(float(np.median([r["t0_move"] for r in rs])), 1),
    }
print(json.dumps(summary, indent=1, ensure_ascii=False))
if args.out:
    Path(args.out).write_text(json.dumps({"checkpoint": str(ckpt), "dataset": str(D), "shift_mm": args.shift_mm,
                                          "seeds": args.seeds, "summary": summary, "results": results},
                                         indent=1, ensure_ascii=False) + "\n")
