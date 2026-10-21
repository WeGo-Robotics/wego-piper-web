"""가중치 분석 — 오프라인 평가 작업·캐시·집계 (feature/weight-analyzer.md).

LeRobot 을 부르는 일은 `wrapper/offline_eval.py` 가 한다. 여기는:

- 에피소드 평가를 **작업 유닛**(`piper-weval`)으로 띄우고 진행을 읽는다
- 결과를 (체크포인트, 데이터셋, 표본) 키로 캐시한다 — 같은 조합은 다시 안 돈다
- 데이터셋 단위 값을 **도·mm** 로 바꾸고 페이즈별·에피소드별로 묶는다
- 한 프레임(장면 편집기)과 반응은 수 초라 블로킹 subprocess 로 돈다

체크포인트 개요(W1)도 여기서 엮는다 — 학습 곡선(실행 폴더) · 평가 기록 · 오프라인 결과.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import numpy as np

from app.core.config import settings
from app.services.systemd_process import make_process

logger = logging.getLogger(__name__)

SCRIPT = str(Path(__file__).resolve().parents[3] / "wrapper" / "offline_eval.py")
# 정책 로드(수 초) + 데이터셋 열기 + 추론 한두 번
BLOCKING_TIMEOUT_S = 240
# 학습 곡선을 실행 폴더에 남기는 파일 — `train_manager` 가 쓴다
TRAIN_METRICS_FILE = "piper_train_metrics.json"

weval_pm = make_process("piper-weval")
_blocking_lock = asyncio.Lock()


def root() -> Path:
    r = Path(settings.log_dir) / "weight_eval"
    r.mkdir(parents=True, exist_ok=True)
    return r


def _python() -> str:
    return settings.grpc_python if Path(settings.grpc_python).exists() else settings.local_python


# ── 체크포인트·데이터셋 찾기 ─────────────────────────────────────────────────


def resolve_checkpoint(model_id: str) -> dict:
    from app.services.model_scanner import scan_models

    for m in scan_models():
        if m["id"] == model_id:
            return m
    raise LookupError(f"체크포인트를 찾을 수 없습니다: {model_id}")


def resolve_dataset(dataset_id: str) -> Path:
    from app.services.dataset_scanner import find_dataset_path

    p = find_dataset_path(dataset_id)
    if p is None:
        raise LookupError(f"데이터셋을 찾을 수 없습니다: {dataset_id}")
    return p


def _mtime(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


def job_key(ckpt_path: Path, ds_path: Path, episodes: list[int], stride: int) -> str:
    """같은 가중치·같은 데이터·같은 표본이면 같은 키. 파일이 바뀌면 다른 키."""
    data_sig = max((_mtime(f) for f in (ds_path / "data").rglob("*.parquet")), default=0)
    raw = json.dumps([str(ckpt_path), _mtime(ckpt_path / "model.safetensors"),
                      str(ds_path), data_sig, sorted(episodes), stride])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# ── 에피소드 평가 작업 ───────────────────────────────────────────────────────


def _current() -> dict | None:
    f = root() / "current.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def _job_state(key: str) -> str:
    d = root() / key
    if (d / "error.json").exists():
        return "error"
    if (d / "meta.json").exists():
        return "done"
    cur = _current()
    if cur and cur.get("key") == key and weval_pm.is_running:
        return "running"
    return "stopped" if d.exists() else "missing"


async def start_job(checkpoint: str, dataset: str, episodes: list[int], stride: int) -> dict:
    from app.services import exclusivity
    from app.services.exclusivity import Activity

    m = resolve_checkpoint(checkpoint)
    ds = resolve_dataset(dataset)
    stride = max(1, int(stride))
    key = job_key(Path(m["path"]), ds, episodes, stride)
    if _job_state(key) == "done":
        return {"key": key, "cached": True}
    await _settle()
    exclusivity.require_idle(Activity.WEIGHT_EVAL)

    out = root() / key
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    job = {"key": key, "checkpoint": checkpoint, "checkpoint_path": m["path"],
           "policy_type": m.get("policy_type"), "dataset": dataset,
           "episodes": sorted(episodes), "stride": stride, "started": time.time()}
    (out / "job.json").write_text(json.dumps(job, ensure_ascii=False))
    (root() / "current.json").write_text(json.dumps(job, ensure_ascii=False))

    cmd = [_python(), "-u", SCRIPT, "--mode", "episodes", "--checkpoint", m["path"],
           "--repo-id", dataset, "--root", str(ds), "--stride", str(stride), "--out", str(out)]
    if episodes:
        cmd += ["--episodes", *[str(e) for e in sorted(episodes)]]
    await weval_pm.start(cmd)
    return {"key": key, "cached": False}


async def _settle(timeout_s: float = 5.0) -> None:
    """앞 작업이 결과를 다 쓰고 끝나는 중이면 유닛이 내려갈 때까지 잠깐 기다린다.

    ⚠ 유닛 상태는 감시 주기(2초)마다 갱신된다. 결과 파일은 이미 다 썼는데 상태는 아직
    "실행 중" 인 틈에 다음 체크포인트를 시작하면 409 로 막혔다(체크포인트를 잇달아 돌릴 때 실측).
    """
    cur = _current()
    if not cur or not weval_pm.is_running or _job_state(cur["key"]) not in ("done", "error"):
        return
    # 배타 검사가 보는 것은 이 객체의 상태다 — systemctl 이 아니라 감시가 그걸 내려줄 때까지
    deadline = time.monotonic() + timeout_s
    while weval_pm.is_running and time.monotonic() < deadline:
        await asyncio.sleep(0.25)


async def stop_job() -> None:
    await weval_pm.stop()


def status() -> dict:
    cur = _current()
    if not cur:
        return {"state": "idle"}
    key = cur["key"]
    state = _job_state(key)
    out = {"state": state, "job": cur}
    try:
        out["progress"] = json.loads((root() / key / "progress.json").read_text())
    except (OSError, ValueError):
        pass
    if state == "error":
        try:
            out["error"] = json.loads((root() / key / "error.json").read_text()).get("error")
        except (OSError, ValueError):
            out["error"] = None
    elif state == "stopped" and not weval_pm.is_running:
        # 유닛이 결과 없이 끝났다 — 로그 끝을 사유로 보인다
        out["error"] = " / ".join((weval_pm.recent_log(3) or [])[-3:]) or None
    return out


def restore() -> bool:
    """게이트웨이 재시작 뒤 살아있는 평가 유닛에 다시 붙는다 (dataset_jobs 와 같은 이유)."""
    reattach = getattr(weval_pm, "reattach", None)
    return bool(reattach and reattach())


def jobs(checkpoint: str | None = None, dataset: str | None = None) -> list[dict]:
    out = []
    for d in root().iterdir():
        f = d / "job.json"
        if not f.is_file():
            continue
        try:
            job = json.loads(f.read_text())
        except ValueError:
            continue
        if checkpoint and job.get("checkpoint") != checkpoint:
            continue
        if dataset and job.get("dataset") != dataset:
            continue
        job["state"] = _job_state(job["key"])
        if job["state"] == "done":
            job["summary"] = summary(job["key"])["overall"]
        out.append(job)
    return sorted(out, key=lambda j: j.get("started", 0), reverse=True)


# ── 단위 변환과 집계 ─────────────────────────────────────────────────────────


def _dims(names: list[str] | None, width: int) -> list[dict]:
    """행동 차원마다 무엇인가 — 관절이면 몇 번이고 정규화 1단위가 몇 도인지.

    `denormalize_joint` 가 선형이라 차이(오차)는 곱 하나로 도가 된다.
    """
    from piper_robot.kinematics import ARM_JOINTS, norm_to_rad

    per_unit = np.degrees(norm_to_rad(np.array([[100.0] * 6]))[0] - norm_to_rad(np.zeros((1, 6)))[0]) / 100
    if not names or len(names) != width:
        names = [f"{'arm' + str(i // 7) + '_' if width > 7 else ''}"
                 f"{'gripper' if i % 7 == 6 else f'joint{i % 7 + 1}'}.pos" for i in range(width)]
    out = []
    for i, n in enumerate(names):
        base = n.rsplit(".", 1)[0]
        arm, _, leaf = base.rpartition("_")
        if leaf.startswith("joint") and leaf in ARM_JOINTS:
            j = ARM_JOINTS.index(leaf)
            out.append({"name": n, "kind": "joint", "arm": arm or None, "joint": j,
                        "scale": float(abs(per_unit[j]))})
        else:
            out.append({"name": n, "kind": "gripper" if "gripper" in leaf else "other",
                        "arm": arm or None, "joint": None, "scale": 1.0})
    return out


def _load(key: str):
    import pandas as pd

    d = root() / key
    meta = json.loads((d / "meta.json").read_text())
    job = json.loads((d / "job.json").read_text())
    df = pd.read_parquet(d / "result.parquet")
    return meta, job, df


def _phase_map(dataset: str) -> dict[tuple[int, int], int]:
    """(에피소드, 프레임) → 페이즈. 사이드카가 없으면 빈 것."""
    import pandas as pd
    from piper_phase import labeler as PL

    try:
        _, signals = PL.sidecar_paths(resolve_dataset(dataset))
        s = pd.read_parquet(signals, columns=["episode_index", "frame_index", "phase"])
    except Exception:
        return {}
    return {(int(e), int(f)): int(p)
            for e, f, p in zip(s.episode_index, s.frame_index, s.phase)}


def _grip_timing(frames: np.ndarray, true: np.ndarray, pred: np.ndarray, fps: float) -> dict | None:
    """녹화에서 그리퍼가 닫히는 순간마다, 예측이 닫힌 순간과 몇 초 차이인가.

    문턱은 그 에피소드 녹화의 가운데 값. 녹화가 거의 안 움직이면(20단위 미만) 재지 않는다.
    짝은 2초 안의 가장 가까운 예측 닫힘 — 없으면 "놓침".
    """
    lo, hi = float(true.min()), float(true.max())
    if hi - lo < 20:
        return None
    thr = (lo + hi) / 2

    def downs(v):
        return frames[1:][(v[:-1] > thr) & (v[1:] <= thr)]

    t_cross, p_cross = downs(true), downs(pred)
    deltas, missed = [], 0
    for t in t_cross:
        near = p_cross[np.abs(p_cross - t) <= 2 * fps]
        if len(near) == 0:
            missed += 1
        else:
            deltas.append(float(near[np.argmin(np.abs(near - t))] - t) / fps)
    return {"closes": int(len(t_cross)), "missed": missed, "deltas_s": deltas}


def summary(key: str) -> dict:
    meta, job, df = _load(key)
    pred = np.stack(df["pred"].to_numpy()).astype(float)
    true = np.stack(df["true"].to_numpy()).astype(float)
    cmae = np.stack(df["chunk_mae"].to_numpy()).astype(float)
    dims = _dims(meta.get("action_names"), pred.shape[1])
    scale = np.array([d["scale"] for d in dims])
    joint = np.array([d["kind"] == "joint" for d in dims])
    grip = np.array([d["kind"] == "gripper" for d in dims])
    err = np.abs(pred - true) * scale          # 관절은 도, 그리퍼는 정규화 단위
    cerr = cmae * scale

    def block(mask_rows) -> dict:
        e, c = err[mask_rows], cerr[mask_rows]
        if len(e) == 0:
            return {"n": 0}
        return {
            "n": int(len(e)),
            "joint_deg": round(float(e[:, joint].mean()), 2) if joint.any() else None,
            "gripper": round(float(e[:, grip].mean()), 2) if grip.any() else None,
            "chunk_joint_deg": round(float(c[:, joint].mean()), 2) if joint.any() else None,
        }

    episodes = df["episode"].to_numpy()
    frames = df["frame"].to_numpy()
    fps = float(meta.get("fps") or 15)

    phases = _phase_map(job["dataset"])
    ph = np.array([phases.get((int(e), int(f)), -1) for e, f in zip(episodes, frames)])
    from piper_phase import PHASE_NAMES

    by_phase = []
    for code in sorted(set(ph.tolist())):
        if code < 0:
            continue
        by_phase.append({"phase": code, "name": PHASE_NAMES[code] if code < len(PHASE_NAMES) else str(code),
                         **block(ph == code)})

    by_episode, deltas, closes, missed = [], [], 0, 0
    for ep in sorted(set(episodes.tolist())):
        sel = episodes == ep
        order = np.argsort(frames[sel])
        timing = []
        for i, d in enumerate(dims):
            if d["kind"] != "gripper":
                continue
            tm = _grip_timing(frames[sel][order], true[sel][order, i], pred[sel][order, i], fps)
            if tm:
                timing.append({"dim": i, "arm": d["arm"], **tm})
                deltas += tm["deltas_s"]
                closes += tm["closes"]
                missed += tm["missed"]
        by_episode.append({"episode": int(ep), **block(sel), "grip": timing})

    overall = block(np.ones(len(df), bool))
    overall["grip_closes"] = closes
    overall["grip_missed"] = missed
    overall["grip_mean_abs_s"] = round(float(np.mean(np.abs(deltas))), 2) if deltas else None
    overall["grip_mean_s"] = round(float(np.mean(deltas)), 2) if deltas else None
    return {
        "key": key, "job": job, "meta": meta,
        "dims": [{k: d[k] for k in ("name", "kind", "arm", "joint")} for d in dims],
        "per_dim": [round(float(v), 2) for v in err.mean(axis=0)],
        "overall": overall,
        "by_phase": by_phase,
        "by_episode": by_episode,
    }


def episode_series(key: str, episode: int) -> dict:
    """그래프용 — 데이터셋 단위(뷰어의 관절 그래프와 같은 단위) 예측·녹화와 페이즈."""
    meta, job, df = _load(key)
    e = df[df["episode"] == episode].sort_values("frame")
    if e.empty:
        raise LookupError(f"에피소드 {episode} 결과가 없습니다")
    phases = _phase_map(job["dataset"])
    pred = np.stack(e["pred"].to_numpy()).astype(float)
    true = np.stack(e["true"].to_numpy()).astype(float)
    return {
        "episode": episode,
        "frames": e["frame"].astype(int).tolist(),
        "phase": [phases.get((episode, int(f)), -1) for f in e["frame"]],
        "names": [d["name"] for d in _dims(meta.get("action_names"), pred.shape[1])],
        "pred": np.round(pred.T, 2).tolist(),
        "true": np.round(true.T, 2).tolist(),
        "fps": meta.get("fps"),
    }


# ── 한 프레임: 장면 편집기의 바탕과 반응 ─────────────────────────────────────


def _run_blocking(cmd: list[str], out: Path) -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=BLOCKING_TIMEOUT_S)
    if (out / "error.json").exists():
        raise RuntimeError(json.loads((out / "error.json").read_text()).get("error") or "실패")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(tail[-1] if tail else f"exit {proc.returncode}")
    return json.loads((out / "meta.json").read_text())


async def frames(dataset: str, episode: int, frame: int) -> dict:
    ds = resolve_dataset(dataset)
    sid = uuid.uuid4().hex[:12]
    out = root() / "frames" / sid
    out.mkdir(parents=True)
    cmd = [_python(), "-u", SCRIPT, "--mode", "frames", "--repo-id", dataset, "--root", str(ds),
           "--episode", str(episode), "--frame", str(frame), "--out", str(out)]
    async with _blocking_lock:
        meta = await asyncio.to_thread(_run_blocking, cmd, out)
    _prune(root() / "frames", keep=40)
    return {"sid": sid, **meta}


def frame_png(sid: str, camera: str) -> Path:
    p = root() / "frames" / sid / f"{camera}.png"
    if "/" in sid or "/" in camera or not p.is_file():
        raise LookupError("프레임 이미지가 없습니다")
    return p


def _prune(d: Path, keep: int) -> None:
    items = sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for p in items[:-keep]:
        shutil.rmtree(p, ignore_errors=True)


def _tips(chunk: np.ndarray, dims: list[dict]) -> dict[str, list]:
    """청크의 관절값 → 팔마다 말단(그리퍼 손가락 뿌리 가운데) 궤적 mm."""
    from piper_robot.kinematics import geometry, link_transforms, norm_to_rad

    g = geometry()
    fingers = [g.index("gripper_link1"), g.index("gripper_link2")]
    out = {}
    for arm in sorted({d["arm"] or "" for d in dims}):
        cols = sorted((d["joint"], i) for i, d in enumerate(dims)
                      if d["kind"] == "joint" and (d["arm"] or "") == arm)
        if len(cols) != 6:
            continue
        q = norm_to_rad(chunk[:, [i for _, i in cols]])
        # ⚠ 원점부터 뽑고 손가락 링크를 고른다 (piper_phase.grasps 의 같은 주석)
        tip = link_transforms(q, g)[:, :, :3, 3][:, fingers].mean(axis=1) * 1000
        out[arm or "single"] = np.round(tip, 1).tolist()
    return out


async def reaction(checkpoint: str, dataset: str, episode: int, frame: int,
                   edits: dict[str, bytes]) -> dict:
    from app.services import exclusivity
    from app.services.exclusivity import Activity

    m = resolve_checkpoint(checkpoint)
    ds = resolve_dataset(dataset)
    sid = uuid.uuid4().hex[:12]
    out = root() / "reaction" / sid
    out.mkdir(parents=True)
    edit_args = []
    for cam, data in edits.items():
        if "/" in cam:
            raise ValueError(f"카메라 이름이 이상합니다: {cam}")
        (out / f"edit_{cam}.png").write_bytes(data)
        edit_args.append(f"{cam}={out / f'edit_{cam}.png'}")
    # 학습·추론이 GPU 를 쥐고 있으면 막지 않고 CPU 로 — 엔코더 프로브와 같다(한 프레임이라 견딜 만하다)
    device = "cpu" if exclusivity.blocked_reason(Activity.WEIGHT_EVAL) else "cuda"
    cmd = [_python(), "-u", SCRIPT, "--mode", "reaction", "--checkpoint", m["path"],
           "--repo-id", dataset, "--root", str(ds), "--episode", str(episode),
           "--frame", str(frame), "--device", device, "--out", str(out)]
    if edit_args:
        cmd += ["--edit", *edit_args]
    async with _blocking_lock:
        await asyncio.to_thread(_run_blocking, cmd, out)
    _prune(root() / "reaction", keep=40)

    r = json.loads((out / "reaction.json").read_text())
    orig = np.array(r["original"], float)
    names = _ds_action_names(ds)
    dims = _dims(names, orig.shape[1])
    result = {"checkpoint": checkpoint, "dataset": dataset, "episode": episode, "frame": frame,
              "device": device, "edited_cameras": r.get("edited_cameras", []),
              "names": [d["name"] for d in dims],
              "original": np.round(orig, 2).tolist(), "original_tips": _tips(orig, dims)}
    if "edited" in r:
        ed = np.array(r["edited"], float)
        result["edited"] = np.round(ed, 2).tolist()
        result["edited_tips"] = _tips(ed, dims)
        # 두 계획이 얼마나 갈라지나 — 팔마다 말단 거리, 그리퍼마다 닫는 시점
        dist = {}
        for arm, a in result["original_tips"].items():
            b = np.array(result["edited_tips"][arm])
            d = np.linalg.norm(np.array(a) - b, axis=1)
            dist[arm] = {"mean_mm": round(float(d.mean()), 1), "max_mm": round(float(d.max()), 1),
                         "end_mm": round(float(d[-1]), 1)}
        result["tip_distance"] = dist
    return result


def _ds_action_names(ds: Path) -> list[str] | None:
    for f in (ds / "meta" / "info.json", ds / "info.json"):
        try:
            names = json.loads(f.read_text())["features"]["action"].get("names")
            return names.get("motors") if isinstance(names, dict) else names
        except (OSError, ValueError, KeyError):
            continue
    return None


# ── W1: 체크포인트 개요 ──────────────────────────────────────────────────────


def _eval_stats() -> dict[str, dict]:
    """평가 기록(실기 O/X)을 체크포인트별로."""
    f = Path(settings.log_dir) / "eval_logs" / "eval_log.jsonl"
    out: dict[str, dict] = {}
    try:
        lines = f.read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        s = out.setdefault(r.get("checkpoint") or "", {"n": 0, "success": 0})
        s["n"] += 1
        s["success"] += 1 if r.get("success") else 0
    return out


def _run_dir(ckpt_path: str) -> Path:
    # .../<run>/checkpoints/<step>/pretrained_model
    return Path(ckpt_path).parents[2]


def runs() -> list[dict]:
    from app.services.model_scanner import scan_models

    by_run: dict[str, dict] = {}
    for m in scan_models():
        run = m.get("run")
        if not run or not m.get("is_policy", True):
            continue
        r = by_run.setdefault(run, {"run": run, "policy_type": m.get("policy_type"),
                                    "created": m.get("run_created") or m.get("created"),
                                    "checkpoints": 0, "path": m["path"]})
        r["checkpoints"] += 1
    out = []
    for r in by_run.values():
        try:
            tc = json.loads((Path(r.pop("path")) / "train_config.json").read_text())
            r["dataset"] = (tc.get("dataset") or {}).get("repo_id")
        except (OSError, ValueError):
            r["dataset"] = None
        out.append(r)
    return sorted(out, key=lambda r: str(r.get("created") or r["run"]), reverse=True)


def run_detail(run: str) -> dict:
    from app.services.model_scanner import scan_models

    ckpts = [m for m in scan_models() if m.get("run") == run]
    if not ckpts:
        raise LookupError(f"학습 실행을 찾을 수 없습니다: {run}")
    any_path = ckpts[0]["path"]
    try:
        tc = json.loads((Path(any_path) / "train_config.json").read_text())
    except (OSError, ValueError):
        tc = {}
    ds_cfg = tc.get("dataset") or {}
    history = None
    try:
        history = json.loads((_run_dir(any_path) / TRAIN_METRICS_FILE).read_text())
    except (OSError, ValueError):
        pass
    evals = _eval_stats()
    done = [j for j in jobs() if j["state"] == "done"]
    rows = []
    for m in ckpts:
        offline = [j for j in done if j["checkpoint"] == m["id"]]
        rows.append({
            "id": m["id"], "checkpoint": m.get("checkpoint"), "step": m.get("step"),
            "real": evals.get(m["id"]),
            # 같은 체크포인트를 여러 데이터·표본으로 돌렸으면 전부 — 화면이 학습 데이터 것을 앞에 둔다
            "offline": [{"key": j["key"], "dataset": j["dataset"], "episodes": j["episodes"],
                         "stride": j["stride"], **j["summary"]} for j in offline],
        })
    # step 순, last 는 맨 뒤 (step 이 없다)
    rows.sort(key=lambda r: (r["step"] is None, r["step"] or 0))
    return {
        "run": run, "policy_type": ckpts[0].get("policy_type"),
        "dataset": ds_cfg.get("repo_id"), "dataset_episodes": ds_cfg.get("episodes"),
        "steps": tc.get("steps"), "history": history, "checkpoints": rows,
    }
