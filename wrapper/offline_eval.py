"""오프라인 평가 — 데이터셋 프레임을 정책에 넣어 예측을 녹화와 견준다 (feature/weight-analyzer.md §4·§5).

두 모드:

    episodes  고른 에피소드를 stride 프레임 간격으로. 프레임마다 청크 하나를 예측해
              첫 동작과 청크 전체를 녹화 행동과 비교한다 → result.parquet
    reaction  한 프레임을 원본·편집 두 벌로 넣어 두 예측 청크를 낸다 → reaction.json
              (편집 이미지가 없으면 원본 한 벌만)
    frames    에피소드×프레임의 카메라 이미지를 PNG 로 → 장면 편집기·엔코더 프로브의 바탕 (정책 불필요)

LeRobot 을 직접 부르는 자리다 — 엔코더 프로브와 같은 이유로 백엔드가 아니라 여기 있다.
정책 로드·전처리·`predict_action_chunk`·후처리는 **추론 wrapper(lerobot_wrapper.py)와
같은 길**이다. 다르면 오프라인에서 본 정책이 실기에서 도는 정책과 다른 물건이 된다.

값은 전부 **데이터셋 단위**(정규화 관절값)로 낸다. 도·mm 변환과 페이즈별 집계는
게이트웨이가 한다 — 여기서는 기구학·사이드카를 모른다.

출력 폴더:
    meta.json       무엇을 어떻게 돌렸나 + 걸린 시간 (끝났다는 표시이기도 하다)
    progress.json   {"done": n, "total": m} — 화면 진행 막대
    error.json      실패 사유 (있으면 실패)
"""

import lerobot_bootstrap  # noqa: F401  (다른 lerobot import 보다 먼저 — 머리말 참고)

import argparse
import importlib
import json
import sys
import time
import traceback
from contextlib import nullcontext
from pathlib import Path

import numpy as np

from policy_registry import policy_imports


def _log(msg: str) -> None:
    print(f"[offline_eval] {msg}", flush=True)


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


def load_policy(checkpoint: str, device: str):
    """추론 wrapper 와 같은 순서: 정책 클래스 → from_pretrained → 전처리·후처리."""
    import torch
    from lerobot.utils.import_utils import register_third_party_plugins

    register_third_party_plugins()
    ptype = json.loads((Path(checkpoint) / "config.json").read_text()).get("type", "")
    imports = policy_imports()
    if ptype not in imports:
        raise ValueError(f"지원하지 않는 정책입니다: {ptype}")
    model_mod, model_cls, _cfg_mod, _cfg_cls = imports[ptype]
    policy = getattr(importlib.import_module(model_mod), model_cls).from_pretrained(checkpoint)
    policy.config.device = device
    policy.eval()
    policy.to(torch.device(device))

    for mod in ("lerobot.policies.smolvla.processor_smolvla",
                "lerobot.policies.pi0.processor_pi0",
                "lerobot.policies.pi05.processor_pi05"):
        try:
            importlib.import_module(mod)
        except Exception:
            pass
    from lerobot.policies.factory import make_pre_post_processors

    pre, post = make_pre_post_processors(
        policy.config,
        pretrained_path=checkpoint,
        preprocessor_overrides={"device_processor": {"device": device}},
        postprocessor_overrides={"device_processor": {"device": "cpu"}},
    )
    return ptype, policy, pre, post


def predict(policy, pre, post, item: dict, image_keys: list[str], device: str) -> np.ndarray:
    """데이터셋 항목 하나 → 후처리까지 마친 청크 (chunk, action_dim), 데이터셋 단위."""
    import torch

    obs = {k: item[k] for k in image_keys}
    obs["observation.state"] = item["observation.state"]
    obs["task"] = item.get("task", "")
    amp = (torch.autocast(device_type="cuda")
           if device.startswith("cuda") and getattr(policy.config, "use_amp", False) else nullcontext())
    with torch.inference_mode(), amp:
        chunk = policy.predict_action_chunk(pre(obs))
    if chunk.ndim == 2:
        chunk = chunk.unsqueeze(0)
    # 후처리는 행동 하나씩 — wrapper 와 같다 (정규화 해제 단계가 (1, D) 를 기대한다)
    rows = [post(chunk[0, i].unsqueeze(0)).squeeze(0).float().cpu().numpy()
            for i in range(chunk.shape[1])]
    return np.stack(rows)


def open_dataset(repo_id: str, root: str, episodes: list[int] | None):
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    return LeRobotDataset(repo_id, root=root, episodes=episodes)


def run_episodes(args, out: Path) -> None:
    import pandas as pd

    ptype, policy, pre, post = load_policy(args.checkpoint, args.device)
    ds = open_dataset(args.repo_id, args.root, args.episodes or None)
    image_keys = [k for k in policy.config.input_features if k.startswith("observation.images.")]

    cols = ds.hf_dataset.select_columns(["episode_index", "frame_index", "action"]).with_format("numpy")
    ep_idx = np.asarray(cols["episode_index"]).astype(int)
    fr_idx = np.asarray(cols["frame_index"]).astype(int)
    actions = np.stack(cols["action"]).astype(np.float32)

    # 에피소드마다 녹화 행동을 frame_index 순으로 — 청크(앞으로의 계획)와 견줄 정답
    recorded: dict[int, np.ndarray] = {}
    for ep in np.unique(ep_idx):
        sel = np.flatnonzero(ep_idx == ep)
        recorded[int(ep)] = actions[sel[np.argsort(fr_idx[sel])]]

    picks = [i for i in range(len(ep_idx)) if fr_idx[i] % args.stride == 0]
    _write(out / "progress.json", {"done": 0, "total": len(picks)})
    _log(f"{ptype} · {len(recorded)}개 에피소드 · {len(picks)}프레임 (stride {args.stride})")

    rows, t0 = [], time.time()
    for n, i in enumerate(picks, 1):
        item = ds[i]
        ep, fr = int(ep_idx[i]), int(fr_idx[i])
        chunk = predict(policy, pre, post, item, image_keys, args.device)
        truth = recorded[ep][fr: fr + len(chunk)]
        h = len(truth)
        rows.append({
            "episode": ep,
            "frame": fr,
            "pred": chunk[0].tolist(),
            "true": truth[0].tolist(),
            # 청크가 녹화와 얼마나 같은 길을 그리나 — 관절별 평균 절대 오차(지평 h)
            "chunk_mae": np.abs(chunk[:h] - truth).mean(axis=0).tolist(),
            "horizon": h,
        })
        if n % 10 == 0 or n == len(picks):
            _write(out / "progress.json", {"done": n, "total": len(picks)})
    elapsed = time.time() - t0

    pd.DataFrame(rows).to_parquet(out / "result.parquet")
    names = (ds.meta.features.get("action") or {}).get("names")
    _write(out / "meta.json", {
        "mode": "episodes",
        "policy_type": ptype,
        "checkpoint": args.checkpoint,
        "repo_id": args.repo_id,
        "episodes": sorted(recorded),
        "stride": args.stride,
        "frames": len(rows),
        "chunk_size": int(len(chunk)) if rows else 0,
        "action_names": names.get("motors") if isinstance(names, dict) else names,
        "fps": ds.meta.fps,
        "elapsed_s": round(elapsed, 1),
        "device": args.device,
    })
    _log(f"끝: {len(rows)}프레임 {elapsed:.1f}초")


def run_reaction(args, out: Path) -> None:
    """한 프레임 — 원본과(있으면) 편집본. 관절 상태는 같은 것을 넣는다."""
    import torch
    from PIL import Image

    ptype, policy, pre, post = load_policy(args.checkpoint, args.device)
    ds = open_dataset(args.repo_id, args.root, [args.episode])
    image_keys = [k for k in policy.config.input_features if k.startswith("observation.images.")]

    cols = ds.hf_dataset.select_columns(["episode_index", "frame_index"]).with_format("numpy")
    hit = np.flatnonzero((np.asarray(cols["episode_index"]) == args.episode)
                         & (np.asarray(cols["frame_index"]) == args.frame))
    if len(hit) == 0:
        raise ValueError(f"에피소드 {args.episode} 에 프레임 {args.frame} 이 없습니다")
    item = ds[int(hit[0])]

    result = {"mode": "reaction", "policy_type": ptype, "checkpoint": args.checkpoint,
              "episode": args.episode, "frame": args.frame,
              "state": item["observation.state"].tolist(),
              "original": predict(policy, pre, post, item, image_keys, args.device).tolist()}

    edits = dict(e.split("=", 1) for e in (args.edit or []))
    if edits:
        edited = dict(item)
        for cam, path in edits.items():
            key = f"observation.images.{cam}"
            if key not in item:
                raise ValueError(f"이 데이터셋에 없는 카메라입니다: {cam}")
            c, h, w = item[key].shape
            img = Image.open(path).convert("RGB").resize((w, h))
            edited[key] = torch.from_numpy(np.asarray(img).copy()).permute(2, 0, 1).float() / 255.0
        result["edited"] = predict(policy, pre, post, edited, image_keys, args.device).tolist()
        result["edited_cameras"] = sorted(edits)
    _write(out / "reaction.json", result)
    _write(out / "meta.json", {"mode": "reaction", "policy_type": ptype})
    _log("끝: 반응")


def run_frames(args, out: Path) -> None:
    """프레임들의 카메라 이미지를 PNG 로 — 장면 편집기·엔코더 프로브의 바탕.

    에피소드 × 프레임 조합마다 `e{에피소드}_f{프레임}/{카메라}.png`. 데이터셋을 여는 것이 비싸서
    (수 초) 여러 장을 **한 번에** 꺼낸다 — 엔코더 프로브는 보통 여러 장을 견준다.

    ⚠ **평가와 같은 디코더**에서 꺼낸다. 브라우저 `<video>` 에서 캡처하면 한 프레임 어긋나거나
    색이 미세하게 달라, 원본과 편집본의 차이에 편집 말고 다른 것이 섞인다.
    """
    from PIL import Image

    episodes = args.episodes or [args.episode]
    frames = args.frames or [args.frame]
    ds = open_dataset(args.repo_id, args.root, sorted(set(episodes)))
    cols = ds.hf_dataset.select_columns(["episode_index", "frame_index"]).with_format("numpy")
    index = {(int(e), int(f)): i for i, (e, f) in
             enumerate(zip(np.asarray(cols["episode_index"]), np.asarray(cols["frame_index"])))}

    items, cams, task = [], set(), ""
    for ep in episodes:
        for fr in frames:
            i = index.get((ep, fr))
            if i is None:
                items.append({"episode": ep, "frame": fr, "dir": None,
                              "error": f"에피소드 {ep} 에 프레임 {fr} 이 없습니다"})
                continue
            item = ds[i]
            task = task or item.get("task", "")
            d = out / f"e{ep}_f{fr}"
            d.mkdir(exist_ok=True)
            for key, val in item.items():
                if not key.startswith("observation.images."):
                    continue
                cam = key.split("observation.images.", 1)[1]
                img = (val.permute(1, 2, 0).clamp(0, 1).numpy() * 255).round().astype(np.uint8)
                Image.fromarray(img).save(d / f"{cam}.png")
                cams.add(cam)
            items.append({"episode": ep, "frame": fr, "dir": d.name})
    if not any(it["dir"] for it in items):
        raise ValueError(items[0]["error"] if items else "꺼낼 프레임이 없습니다")
    _write(out / "meta.json", {"mode": "frames", "cameras": sorted(cams), "items": items, "task": task})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["episodes", "reaction", "frames"], default="episodes")
    ap.add_argument("--checkpoint", default="", help="pretrained_model 폴더 (frames 에는 필요 없다)")
    ap.add_argument("--repo-id", required=True)
    ap.add_argument("--root", required=True, help="데이터셋 폴더")
    ap.add_argument("--episodes", type=int, nargs="*", default=[])
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--episode", type=int, default=0)
    ap.add_argument("--frame", type=int, default=0)
    ap.add_argument("--frames", type=int, nargs="*", default=[], help="frames 모드: 여러 프레임 (--episodes 와 조합)")
    ap.add_argument("--edit", nargs="*", help="카메라=편집이미지경로 (reaction)")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        if args.device.startswith("cuda"):
            import torch
            if not torch.cuda.is_available():
                _log("CUDA 가 없어 CPU 로 돈다")
                args.device = "cpu"
        if args.mode != "frames" and not args.checkpoint:
            raise ValueError("--checkpoint 가 필요합니다")
        {"reaction": run_reaction, "frames": run_frames}.get(args.mode, run_episodes)(args, out)
        return 0
    except Exception as exc:
        traceback.print_exc()
        _write(out / "error.json", {"error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
