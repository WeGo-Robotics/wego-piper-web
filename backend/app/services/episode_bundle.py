"""에피소드 묶음 — 에이전트가 **보는** 자료를 zip 하나로 (feature/agent-cli.md §4-5).

"이 에피소드 왜 빗나갔나" 는 이미지를 읽는 CLI 가 잘하고 로컬 7b 가 못 하는 질문이다. 그 질문에
필요한 것을 한 번에 준다 — 페이지 경계의 키프레임, 타임라인, 상태·액션 표, 그리고 그걸 어떻게
읽는지 적은 README. 데이터는 **읽기만** 한다. 분석·판정은 하지 않는다 — 그건 에이전트 몫이다.

## 안에 든 것

    README.md                      무엇이 들었나 · 눈금 · 키프레임이 가리키는 프레임 · 어떻게 보나
    timeline.json                  fps·길이·task·카메라·페이즈 구간·신호(속도·그리퍼 간격·페이즈)·노트·구운 사본 표식
    state_action.csv               프레임별 observation.state / action (features 의 names 가 열 이름)
    keyframes/<cam>/frame-N.jpg    at=phases: 페이즈 경계마다 앞·뒤 한 장 + 첫·끝 / at=every:N: 균등

## 프레임은 어디서 오나

1. 디코딩 캐시(`images/<key>/episode-N/frame-N.jpg|png`) — 뷰어·편집기와 같은 파일
2. 없으면 mp4 에서 **바로 디코딩**(cv2) — 에피소드 레코드의 `videos/<key>/from_timestamp` 와 chunk/file 로
   파일을 고르고 프레임 번호로 찍는다. 캐시를 먼저 만들라고 요구하면 아무도 안 쓴다
3. 둘 다 안 되면 그 프레임은 비우고 README 에 적는다 — 묶음은 나간다

## 페이즈가 없으면

`at=phases` 는 400 이 아니라 균등 샘플로 떨어지고 README 에 그렇게 적는다 — 에이전트가 다음 질문
("분석을 먼저 돌릴까?")을 하게.

⚠ 이 파일의 한국어(README·오류)는 에이전트가 읽는 글이다 — `tools/i18n.py` 의 `PY_SKIP_FILES` 가 뺀다.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

IMG_PREFIX = "observation.images."
MAX_KEYFRAMES_PER_CAM = 60      # 묶음 크기 상한 — 넘으면 고르게 덜어낸다 (첫·끝은 지킨다)
FALLBACK_SAMPLES = 16           # 페이즈가 없을 때 균등 샘플 수(대략)
MAX_DIM = 800                   # 직접 디코딩한 프레임의 긴 변 상한 — 묶음이 수십 MB 가 되지 않게
JPEG_QUALITY = 85
_EVERY = re.compile(r"^every:(\d+)$")


class BundleError(Exception):
    """호출자 잘못(400) 또는 없음(404) — 라우터가 그대로 HTTP 로 바꾼다."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


@dataclass
class Ctx:
    dataset_id: str
    episode: int
    ds_path: Path
    info: dict
    rec: dict
    fps: float
    length: int
    cams: list[str] = field(default_factory=list)


#: (ctx, cam_key, frames) → {frame: (bytes, ext)} — 테스트가 가짜를 꽂는 자리
FrameReader = Callable[[Ctx, str, list[int]], dict[int, tuple[bytes, str]]]


# ── 인자 ──────────────────────────────────────────────────────────────────────


def parse_cams(raw: str | None) -> list[str] | None:
    if raw is None or not str(raw).strip():
        return None
    return [c.strip() for c in str(raw).split(",") if c.strip()]


def parse_at(at: str | None) -> tuple[str, int | None]:
    """'phases' | 'every:N' → ('phases', None) | ('every', N). 그 밖은 400."""
    at = (at or "phases").strip()
    if at == "phases":
        return "phases", None
    m = _EVERY.match(at)
    if m and int(m.group(1)) >= 1:
        return "every", int(m.group(1))
    raise BundleError(400, f"모르는 at 입니다: {at!r} — phases 또는 every:N (N≥1)")


# ── 프레임 고르기 ──────────────────────────────────────────────────────────────


def frames_at_boundaries(length: int, segments: list) -> list[int]:
    """페이즈 경계마다 앞·뒤 한 장씩 + 첫·끝. 경계 = 0 이 아닌 구간 시작."""
    if length <= 0:
        return []
    picks = {0, length - 1}
    for seg in segments or []:
        try:
            start = int(seg[0])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 < start < length:
            picks.update({start - 1, start})
    return sorted(picks)


def frames_every(length: int, every: int) -> list[int]:
    if length <= 0:
        return []
    picks = set(range(0, length, max(1, every)))
    picks.add(length - 1)
    return sorted(picks)


def thin(frames: list[int], cap: int = MAX_KEYFRAMES_PER_CAM) -> list[int]:
    """상한을 넘으면 고르게 덜어낸다 — 첫·끝은 지킨다."""
    if len(frames) <= cap or cap < 2:
        return list(frames)
    idx = [round(i * (len(frames) - 1) / (cap - 1)) for i in range(cap)]
    return sorted({frames[i] for i in idx})


# ── 데이터 ────────────────────────────────────────────────────────────────────


def _episode_df(ctx: Ctx):
    """이 에피소드의 프레임 표 — 레코드가 가리키는 parquet 하나를 먼저, 없으면 data/ 전체를 훑는다."""
    import pandas as pd
    import pyarrow.parquet as pq

    cols = ["episode_index", "frame_index", "timestamp", "observation.state", "action"]
    files: list[Path] = []
    tpl = ctx.info.get("data_path")
    chunk, file = ctx.rec.get("data/chunk_index"), ctx.rec.get("data/file_index")
    if tpl and chunk is not None and file is not None:
        try:
            p = ctx.ds_path / tpl.format(chunk_index=int(chunk), file_index=int(file),
                                         episode_chunk=int(chunk), episode_index=ctx.episode)
            if p.exists():
                files = [p]
        except (KeyError, IndexError, ValueError):
            files = []
    if not files:
        files = sorted((ctx.ds_path / "data").rglob("*.parquet"))
    parts = []
    for f in files:
        try:
            schema_cols = [c for c in cols if c in pq.read_schema(f).names]
            t = pq.read_table(f, columns=schema_cols).to_pandas()
        except Exception as exc:
            logger.warning("parquet 읽기 실패 (%s): %s", f, exc)
            continue
        if "episode_index" in t.columns:
            t = t[t.episode_index == ctx.episode]
        if not t.empty:
            parts.append(t)
    if not parts:
        return None
    df = pd.concat(parts, ignore_index=True)
    if "frame_index" in df.columns:
        df = df.sort_values("frame_index", ignore_index=True)
    return df


def _names(info: dict, key: str, width: int) -> list[str]:
    names = ((info.get("features") or {}).get(key) or {}).get("names")
    if isinstance(names, dict):      # 일부 데이터셋은 {"motors": [...]} 모양
        names = next(iter(names.values()), None)
    if isinstance(names, list) and len(names) == width:
        return [str(n) for n in names]
    return [str(i) for i in range(width)]


def _csv(ctx: Ctx, df, phase_of: Callable[[int], str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    state0 = list(df["observation.state"].iloc[0]) if "observation.state" in df.columns else []
    action0 = list(df["action"].iloc[0]) if "action" in df.columns else []
    s_names = _names(ctx.info, "observation.state", len(state0))
    a_names = _names(ctx.info, "action", len(action0))
    w.writerow(["frame_index", "timestamp", "phase", *(f"state.{n}" for n in s_names), *(f"action.{n}" for n in a_names)])
    for i, row in df.iterrows():
        fi = int(row["frame_index"]) if "frame_index" in df.columns else int(i)
        ts = row.get("timestamp")
        state = [round(float(v), 4) for v in (row["observation.state"] if "observation.state" in df.columns else [])]
        action = [round(float(v), 4) for v in (row["action"] if "action" in df.columns else [])]
        w.writerow([fi, round(float(ts), 4) if ts is not None and not (isinstance(ts, float) and math.isnan(ts)) else "",
                    phase_of(fi), *state, *action])
    return buf.getvalue()


def _signals(ctx: Ctx, names: list[str]) -> dict | None:
    from piper_phase import labeler as PL

    _, signals_path = PL.sidecar_paths(ctx.ds_path)
    if not signals_path.exists():
        return None
    try:
        import pandas as pd

        df = pd.read_parquet(signals_path)
        e = df[df.episode_index == ctx.episode].sort_values("frame_index")
    except Exception as exc:
        logger.warning("신호 사이드카 읽기 실패 (%s): %s", signals_path, exc)
        return None
    if e.empty:
        return None
    out: dict = {"frame_index": [int(x) for x in e["frame_index"]]}
    if "phase" in e.columns:
        out["phase"] = [names[int(p)] if 0 <= int(p) < len(names) else int(p) for p in e["phase"]]
    for col in ("speed", "gripper_gap", "tip_speed", "home_dist"):
        if col in e.columns:
            out[col] = [round(float(v), 4) for v in e[col]]
    return out


def _phase_names(labels: dict | None) -> list[str]:
    names = (labels or {}).get("phases")
    if isinstance(names, list) and names:
        return [str(n) for n in names]
    try:
        from piper_phase.fsm import PHASE_NAMES
        return list(PHASE_NAMES)
    except Exception:
        return []


# ── 프레임 읽기 — 캐시, 그다음 mp4 직접 ─────────────────────────────────────


def _cached_frame(ds_path: Path, cam_key: str, episode: int, frame: int) -> tuple[bytes, str] | None:
    ep_dir = ds_path / "images" / cam_key / f"episode-{episode:06d}"
    for ext in ("jpg", "png"):
        p = ep_dir / f"frame-{frame:06d}.{ext}"
        if p.exists():
            return p.read_bytes(), ext
    return None


def _video_path(ctx: Ctx, cam_key: str) -> Path | None:
    tpl = ctx.info.get("video_path") or "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
    chunks = int(ctx.info.get("chunks_size") or 1000)
    chunk, file = ctx.rec.get(f"videos/{cam_key}/chunk_index"), ctx.rec.get(f"videos/{cam_key}/file_index")
    kw = {"video_key": cam_key, "episode_index": ctx.episode, "episode_chunk": ctx.episode // chunks,
          "chunk_index": int(chunk) if chunk is not None else ctx.episode // chunks,
          "file_index": int(file) if file is not None else ctx.episode}
    try:
        rel = tpl.format(**kw)
    except (KeyError, IndexError, ValueError):
        return None
    # 허브 데이터셋은 snapshots → blobs 심볼릭이라 .resolve() 로 판정하면 밖으로 나간다 — normpath 로 `..` 만 접는다
    base = ctx.ds_path.resolve()
    p = Path(os.path.normpath(base / rel))
    return p if p.is_relative_to(base) and p.exists() else None


def _decode_frames(video: Path, start_ts: float, fps: float, frames: list[int]) -> dict[int, bytes]:
    try:
        import cv2
    except ImportError:
        return {}
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return {}
    out: dict[int, bytes] = {}
    try:
        base = int(round(start_ts * fps))          # 파일 안에서 이 에피소드가 시작하는 프레임
        for idx in sorted(frames):
            cap.set(cv2.CAP_PROP_POS_FRAMES, base + idx)
            ok, img = cap.read()
            if not ok or img is None:
                continue
            h, w = img.shape[:2]
            if max(h, w) > MAX_DIM:
                s = MAX_DIM / max(h, w)
                img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
            if ok:
                out[idx] = buf.tobytes()
    finally:
        cap.release()
    return out


def default_frame_reader(ctx: Ctx, cam_key: str, frames: list[int]) -> dict[int, tuple[bytes, str]]:
    out: dict[int, tuple[bytes, str]] = {}
    missing: list[int] = []
    for idx in frames:
        hit = _cached_frame(ctx.ds_path, cam_key, ctx.episode, idx)
        if hit:
            out[idx] = hit
        else:
            missing.append(idx)
    if missing:
        video = _video_path(ctx, cam_key)
        if video is not None:
            start = float(ctx.rec.get(f"videos/{cam_key}/from_timestamp") or 0.0)
            for idx, data in _decode_frames(video, start, ctx.fps, missing).items():
                out[idx] = (data, "jpg")
    return out


# ── 묶음 ──────────────────────────────────────────────────────────────────────


def build(dataset_id: str, episode: int, *, cams: list[str] | None = None, at: str = "phases",
          frame_reader: FrameReader | None = None) -> tuple[bytes, dict]:
    """zip 바이트와 요약. 데이터셋·에피소드 없음은 404, 카메라·at 가 틀리면 400 (BundleError)."""
    from piper_phase import labeler as PL

    from app.services import dataset_scanner as DS
    from app.services.notes_sidecar import read_notes

    found = DS.episode_meta(dataset_id, episode)
    if not found:
        raise BundleError(404, f"데이터셋 '{dataset_id}' 에 에피소드 {episode} 가 없습니다")
    ds_path, info, rec = found
    features = info.get("features") or {}
    # 카메라 순서는 데이터셋(features)이 적은 순서다 — 정렬하면 top 보다 hand 가 앞서 보는 사람이 헷갈린다
    available = [k[len(IMG_PREFIX):] for k in features if k.startswith(IMG_PREFIX)]
    wanted = cams or available
    bad = [c for c in wanted if c not in available]
    if bad:
        raise BundleError(400, f"모르는 카메라: {bad} — 이 데이터셋의 카메라: {available}")
    mode, every = parse_at(at)
    fps = float(info.get("fps") or 15.0)
    length = int(rec.get("length") or 0)
    ctx = Ctx(dataset_id, episode, ds_path, info, rec, fps, length, list(wanted))

    labels = PL.load(ds_path)
    names = _phase_names(labels)
    entry = ((labels or {}).get("episodes") or {}).get(str(episode))
    segments = (entry or {}).get("segments") or []
    fallback = False
    if mode == "phases":
        if segments:
            frames = frames_at_boundaries(length, segments)
        else:
            fallback = True
            every = max(1, round(length / FALLBACK_SAMPLES)) if length else 1
            frames = frames_every(length, every)
    else:
        frames = frames_every(length, every or 1)
    thinned = len(frames) > MAX_KEYFRAMES_PER_CAM
    frames = thin(frames)

    def seg_rows() -> list[dict]:
        rows = []
        for seg in segments:
            try:
                s, t, v = seg
                rows.append({"start": int(s), "end": int(t),
                             "phase": names[int(v)] if 0 <= int(v) < len(names) else int(v)})
            except (TypeError, ValueError):
                rows.append({"raw": seg})
        return rows

    phase_rows = seg_rows()

    def phase_of(frame: int) -> str:
        for r in phase_rows:
            if "start" in r and r["start"] <= frame <= r["end"]:
                return str(r["phase"])
        return ""

    reader = frame_reader or default_frame_reader
    keyframes: dict[str, dict[int, tuple[bytes, str]]] = {}
    missing: dict[str, list[int]] = {}
    for cam in wanted:
        got = reader(ctx, IMG_PREFIX + cam, frames)
        keyframes[cam] = got
        miss = [f for f in frames if f not in got]
        if miss:
            missing[cam] = miss

    df = _episode_df(ctx)
    signals = _signals(ctx, names)
    notes = read_notes(ds_path, kind="dataset")
    delta, baked = DS.delta_info(ds_path), DS.baked_info(ds_path)

    timeline = {
        "dataset": dataset_id, "episode": episode, "fps": fps, "length": length,
        "tasks": rec.get("tasks"), "robot_type": info.get("robot_type"), "cameras": wanted,
        "features": {"observation.state": _names(info, "observation.state", _width(df, "observation.state")),
                     "action": _names(info, "action", _width(df, "action"))},
        "phases": ({"segments": phase_rows, "reviewed": bool(entry.get("reviewed")), "note": entry.get("note"),
                    "cycles": entry.get("cycles"), "version": (labels or {}).get("version")} if entry else None),
        "keyframes": {"at": "phases" if mode == "phases" and not fallback else f"every:{every}",
                      "fallback": fallback, "thinned": thinned, "frames": frames,
                      "missing": missing},
        "signals": signals, "notes": notes, "delta": delta, "baked": baked,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    readme = _readme(ctx, timeline, keyframes)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", readme)
        zf.writestr("timeline.json", json.dumps(timeline, ensure_ascii=False, indent=1, default=str))
        if df is not None and not df.empty:
            zf.writestr("state_action.csv", _csv(ctx, df, phase_of))
        for cam, got in keyframes.items():
            for idx in sorted(got):
                data, ext = got[idx]
                zf.writestr(f"keyframes/{cam}/frame-{idx:06d}.{ext}", data, compress_type=zipfile.ZIP_STORED)
    summary = {"dataset": dataset_id, "episode": episode, "cameras": wanted, "frames": frames,
               "at": timeline["keyframes"]["at"], "fallback": fallback, "missing": missing,
               "has_csv": bool(df is not None and not df.empty), "bytes": out.tell()}
    return out.getvalue(), summary


def _width(df, col: str) -> int:
    try:
        return len(df[col].iloc[0]) if df is not None and col in df.columns and len(df) else 0
    except Exception:
        return 0


def _readme(ctx: Ctx, tl: dict, keyframes: dict) -> str:
    kf = tl["keyframes"]
    lines = [f"# 에피소드 묶음 — {ctx.dataset_id} / 에피소드 {ctx.episode}", "",
             f"생성 {tl['generated_at']} · fps {ctx.fps:g} · 길이 {ctx.length}프레임"
             f"({ctx.length / ctx.fps:.1f}초) · task {tl.get('tasks')} · robot_type {tl.get('robot_type')}", "",
             "## 무엇이 들었나", "",
             "- `timeline.json` — 이 파일의 숫자 전부(페이즈 구간·신호·키프레임 번호·노트·구운 사본 표식)",
             "- `state_action.csv` — 프레임별 `observation.state`(실측) 와 `action`(지령). 열 이름은 데이터셋 features 의 names"
             + ("" if tl.get("has_csv", True) else " — **이 묶음엔 없다(parquet 을 못 읽었다)**"),
             f"- `keyframes/<cam>/frame-N.jpg` — 카메라 {', '.join(ctx.cams)}. "
             + ("**페이즈 경계마다 앞·뒤 한 장씩** + 첫·끝 프레임" if kf["at"] == "phases"
                else f"**{kf['at'].split(':')[1]}프레임마다** 균등 + 끝 프레임"), ""]
    if kf["fallback"]:
        lines += ["⚠ 이 에피소드는 **페이즈 분석이 없어** 균등 샘플로 떨어졌다. 경계가 필요하면 먼저 "
                  f"`POST /api/phase/{ctx.dataset_id}/analyze` 를 돌리고 다시 받아라.", ""]
    if kf["thinned"]:
        lines += [f"⚠ 키프레임이 상한({MAX_KEYFRAMES_PER_CAM}장/카메라)을 넘어 고르게 덜어냈다 — 번호는 아래 표가 정본이다.", ""]
    if kf["missing"]:
        lines += ["⚠ 못 읽은 프레임(캐시도 없고 mp4 디코딩도 안 됐다): "
                  + ", ".join(f"{cam} {m}" for cam, m in kf["missing"].items()), ""]
    lines += ["## 눈금", "",
              "- 값은 데이터셋에 **저장된 그대로**다. PIPER(`piper_*`)는 관절 -100..100 · 그리퍼 0..100 의 정규화 눈금이다. "
              "다른 로봇은 robot_type 과 features 를 본다.",
              "- `action` 은 그 프레임에 내린 **지령**, `observation.state` 는 그때 **실측**이다. 둘의 차이가 추종 오차다."]
    if tl.get("delta"):
        lines += [f"- ⚠ **델타 사본**이다 — `action` 의 {tl['delta'].get('delta_names')} 는 절대 위치가 아니라 **이동 변화량**"
                  f"(원본 {tl['delta'].get('source')})."]
    if tl.get("baked"):
        lines += [f"- 구운 사본(stage) — 원본 {tl['baked'].get('source')}, 단계 이름 {tl['baked'].get('stage_names')}."]
    lines += [""]
    if tl.get("phases"):
        lines += ["## 페이즈 구간", "", "| 시작 | 끝 | 페이즈 |", "|---|---|---|"]
        lines += [f"| {r.get('start')} | {r.get('end')} | {r.get('phase')} |" if "start" in r else f"| {r} | | |"
                  for r in tl["phases"]["segments"]]
        lines += ["", f"검토됨 {tl['phases']['reviewed']} · 사이클 {tl['phases'].get('cycles')}"
                  + (f" · 메모 {tl['phases']['note']}" if tl["phases"].get("note") else ""), ""]
    lines += ["## 키프레임 → 프레임 번호", ""]
    for cam, got in keyframes.items():
        lines.append(f"- `{cam}`: " + (", ".join(f"{i}" for i in sorted(got)) if got else "(없음)"))
    lines += ["", "## 어떻게 보나", "",
              "1. `timeline.json` 의 페이즈 구간에서 **GRASP 가 시작되는 프레임**을 찾는다 (없으면 그리퍼 열이 닫히기 시작하는 프레임).",
              "2. 그 경계의 앞·뒤 키프레임을 **카메라마다** 본다 — 그리퍼가 물체의 어디에 닿는지, 물체가 움직였는지, 가려졌는지.",
              "3. `state_action.csv` 의 같은 프레임 행에서 그리퍼 열(`action` 대 `state`)과 관절 추종 오차를 본다.",
              "4. 답은 **프레임 번호와 카메라**로 말한다. 보이지 않는 것은 추측하지 않는다 — 더 촘촘히 보려면 `?at=every:3` 으로 다시 받는다.", ""]
    if tl.get("notes") and (tl["notes"].get("name") or tl["notes"].get("description")):
        lines += ["## 데이터셋 노트", "", f"{tl['notes'].get('name')} — {tl['notes'].get('description')}", ""]
    return "\n".join(lines)


# ── 디버그 런(추론 기록)에도 같은 모양의 keyframes/ ───────────────────────────


def debug_run_keyframes(run_dir: Path, every: int) -> tuple[dict[str, list[Path]], str]:
    """`images/<cam>/<seq>.jpg` 에서 every 장마다 + 마지막을 고른다. (카메라별 선택, README 글)"""
    picked: dict[str, list[Path]] = {}
    images = run_dir / "images"
    if images.is_dir():
        for cam_dir in sorted(p for p in images.iterdir() if p.is_dir()):
            files = sorted(p for p in cam_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
            if not files:
                continue
            idx = frames_every(len(files), every)
            picked[cam_dir.name] = [files[i] for i in thin(idx)]
    lines = [f"# 디버그 런 키프레임 — {run_dir.name}", "",
             f"`images/` 전체 중 {every}장마다 + 마지막을 `keyframes/<cam>/` 에 복사했다 (상한 {MAX_KEYFRAMES_PER_CAM}장/카메라). "
             "파일 이름이 곧 seq 번호다 — `observations.jsonl`·`inference.jsonl`·`control.jsonl` 의 seq 와 맞춰 본다.", "",
             "수집 데이터셋의 에피소드 묶음(`/api/datasets/<id>/episodes/<n>/bundle`)과 같은 눈으로 보라고 만든 자리다 "
             "(feature/agent-cli.md §4-5).", ""]
    for cam, files in picked.items():
        lines.append(f"- `{cam}`: {len(files)}장 — {', '.join(f.stem for f in files[:12])}{' …' if len(files) > 12 else ''}")
    return picked, "\n".join(lines) + "\n"
