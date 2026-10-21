"""파지 순간의 자세 — 에피소드 분석(POS분석 탭)의 원료 (feature/episode-analyzer.md).

## 파지 = 사이드카의 GRASP 구간, 그 **시작 프레임**

새 검출기를 만들지 않는다. 라벨러가 이미 그리퍼 지령/실측 갭으로
GRASP → HOLD(물었다) / GRASP → APPROACH(빈손)를 갈라 두었고, 사람이 고친
라벨도 같은 파일에 있다 — 그걸 그대로 읽는다. HOLD 로 넘어가지 않은 GRASP 는
전부 빈손으로 센다.

시작(닫기 시작한 순간)을 쓰는 이유:

- 닫히는 동안 팔은 거의 안 움직인다. 실측 말단 이동 중앙값 1.0~1.6mm,
  p90 2.7~4.2mm (bolt_two1 · min_cube_071410 · yeonwonju-0709-team1) — HOLD 시작과
  같은 자세다.
- 빈손에는 HOLD 가 없다. 성공과 실패를 **같은 잣대로** 보려면 시작밖에 없다.

예외는 그리퍼 값 하나다 — 그건 **닫기를 마친 뒤**(구간 끝에서 0.5초 더 본 최소 열림)를
읽는다. 물었으면 물체 폭, 빈손이면 0 근처라 그 자체로 결과를 말해준다.

## 양팔 — 두 팔을 다 본다

⚠ **사이드카에는 에피소드마다 한 팔의 페이즈만 있다.** FSM 은 단일 pick 모델이라
라벨러가 더 활동적인 그리퍼(`fsm.main_gripper`) 하나로만 라벨을 낸다. 그런데 실제
양팔 데이터는 거의 매 에피소드 **두 팔이 다 잡는다** (dualarm_cube_4point 15/15,
two_arm_bolt_2 49/50 — 실측), 그리고 "더 활동적" 의 차이는 표준편차 24.6 대 25.0
같은 동전 던지기다. 사이드카만 읽으면 파지의 절반이 조용히 빠지고, 어느 팔이
남는지는 에피소드마다 제멋대로다.

그래서 주 팔은 사이드카(사람이 고친 라벨 포함)에서, **나머지 팔은 같은 FSM 을 그
팔의 7채널에 즉석으로** 돌려서 뽑는다. 임계값은 사이드카에 저장된 것을 쓴다 —
태스크마다 다르다. 이벤트의 `source` 가 둘을 가른다 (`sidecar` | `fsm`).

## 좌표

팔마다 자기 `base_link` 기준 mm. 양팔이면 두 팔 사이 배치를 모르므로(어디에도
저장돼 있지 않다) 한 공간에 합치지 않는다 — 이벤트에 `arm` 을 실어 화면이 팔별
패널로 가른다.

`tip` 은 **그리퍼 손가락 뿌리 두 점의 가운데**다. 플랜지(`endpoint_xyz`)보다 약
14cm 앞이고, 손가락은 이 FK 에서 고정이라 여닫아도 안 움직인다. 플랜지를 쓰면
같은 물체를 위에서 잡느냐 옆에서 잡느냐에 따라 점이 14cm 씩 갈라진다 —
"어디서 잡았나" 를 보는 화면에서는 물체에 가장 가까운 고정점이 맞다.

`yaw` 는 **손가락이 여닫히는 축의 수평 방향**(도, 0~180)이다. 평행 그리퍼라 180° 돌리면
같은 파지이므로 반으로 접는다. 축은 link6 의 국소 y — 손가락 링크가 그리퍼 베이스에서
x 로 90° 돌아 붙어 있어 그 국소 z(행정 방향, `tools/build_arm_geometry.py`)가 link6 의 -y 다.
여닫는 축이 거의 수직이면(옆으로 눕혀 위아래로 집는 자세) 수평 방향이 뜻이 없어 None.

## 접근 동작 (feature/episode-analyzer.md §6 A2)

`approach_speed` 는 닫기 **직전 0.5초**, `closing_speed` 는 **닫는 동안**(GRASP 구간)의 말단
최고 속도(mm/s)다. 둘을 가르는 이유: 보통은 멈춰 서서 닫는데(닫는 중 중앙값 7~30mm/s),
"잘 안 잡힐 것 같으면 앞으로 튀어나가며 집는" 시연 버릇은 **닫는 중**에 드러난다
(min_cube 200mm/s, yeonwonju 149mm/s — 실측 2026-10-21). 닫는 구간을 고정 0.5초로 잡지
않는 것은 그 안에 들어 올리기가 섞이기 때문이다 — HOLD 는 5프레임이면 넘어간다.
`trail` 은 닫기 0.5초 전부터 닫기가 끝날 때까지의 말단 경로, `trail_at` 은 그 안에서
파지 순간(닫기 시작)의 위치다.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from piper_phase.fsm import GRASP, HOLD, Params, label_episode, main_gripper, segments
from piper_phase.labeler import _info_json, dataset_fps, load

# 스틱(막대 인형)의 마디. 화면이 이 **순서**로 선을 잇고 마디 사이마다 색을 바꾼다
# (베이스 · 상완 · 전완 · 손목 · 그리퍼). link1/link2, link4/link5 는 원점이 겹쳐
# 하나로 묶는다 — 겹친 점을 두 번 찍으면 길이 0 인 마디가 생긴다.
STICK_LINKS = ("base_link", "link2", "link3", "link5", "link6")
STICK_LABELS = ("base", "J1·J2", "J3", "J4·J5", "J6", "grip")
_FINGERS = ("gripper_link1", "gripper_link2")
# 여닫는 축의 수평 성분이 이보다 짧으면 yaw 를 안 매긴다 (단위벡터 기준, ≈ 수직에서 17° 안쪽)
_YAW_MIN_HORIZONTAL = 0.3
# 접근 동작을 볼 시간 — 닫기 직전 이만큼
_APPROACH_S = 0.5


def _arm_names(ds_path: Path) -> list[str]:
    """`observation.state` 채널 이름. 없으면 빈 목록."""
    info = _info_json(ds_path)
    if info is None:
        return []
    try:
        feat = json.loads(info.read_text())["features"]["observation.state"]
        names = feat.get("names") or []
        # v2 일부는 {"motors": [...]} 모양이다
        return list(names.get("motors", [])) if isinstance(names, dict) else list(names)
    except Exception:
        return []


def _arm_label(names: list[str], arm: int) -> str | None:
    """`left_joint1.pos` → `left`. 단일 팔(`joint1.pos`)이면 None."""
    if len(names) <= 7 * arm:
        return None if arm == 0 else f"arm{arm}"
    prefix = names[7 * arm].split("joint")[0].rstrip("_")
    return prefix or None


def _grasps(segs) -> list[tuple[int, int, int, str]]:
    """구간 목록 → [(몇 번째 파지, 시작 프레임, 끝 프레임(포함), 결과)]."""
    out = []
    for i, (start, end, code) in enumerate(segs):
        if code != GRASP:
            continue
        nxt = segs[i + 1][2] if i + 1 < len(segs) else None
        out.append((len(out) + 1, int(start), int(end), "hold" if nxt == HOLD else "empty"))
    return out


def _params(labels: dict, fps: float) -> Params:
    """사이드카에 저장된 임계값. 모르는 키(옛 버전)는 버린다."""
    known = {f.name for f in fields(Params)}
    values = {k: v for k, v in (labels.get("params") or {}).items() if k in known}
    values.setdefault("fps", fps)
    return Params(**values)


def extract(ds_path: Path) -> dict | None:
    """파지마다 관절값(도)·그리퍼·말단(mm)·스틱 마디(mm). 라벨이 없으면 None."""
    from piper_robot.kinematics import available, geometry, link_transforms, norm_to_rad

    labels = load(ds_path)
    if labels is None:
        return None
    if not available():
        raise RuntimeError("팔 지오메트리가 없어 말단 위치를 계산할 수 없습니다")

    fps = dataset_fps(ds_path)
    names = _arm_names(ds_path)
    params = _params(labels, fps)
    episodes = labels.get("episodes") or {}
    out = {"labeled": True, "fps": fps, "episodes": len(episodes),
           "reviewed": sum(1 for r in episodes.values() if r.get("reviewed")),
           "stick": list(STICK_LABELS), "events": []}
    if not episodes:
        return out

    g = geometry()
    stick_idx = [g.index(n) for n in STICK_LINKS]
    finger_idx = [g.index(n) for n in _FINGERS]
    wrist_idx = g.index("link6")

    # 필요한 열만 읽는다 — 이미지는 안 본다. 지령은 양팔의 나머지 팔을 즉석 분석할 때 쓴다.
    files = sorted((ds_path / "data").rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"data parquet 가 없습니다: {ds_path}")
    wanted = [int(k) for k in episodes]
    parts = []
    for f in files:
        t = pq.read_table(f, columns=["episode_index", "observation.state", "action"]).to_pandas()
        parts.append(t[t.episode_index.isin(wanted)])

    import pandas as pd

    df = pd.concat(parts, ignore_index=True)
    for ep in sorted(wanted):
        # ⚠ **저장된 순서 그대로** 자른다. 라벨러(`labeler.analyze`)가 구간 번호를
        #   그렇게 매겼다 — 여기서 정렬하면 번호와 프레임이 어긋날 수 있다.
        e = df[df.episode_index == ep]
        if e.empty:
            continue
        rec = episodes[str(ep)]
        state = np.stack(e["observation.state"].to_numpy()).astype(np.float64)
        main = main_gripper(state) // 7
        per_arm = {main: [(*r, bool(rec.get("reviewed")), "sidecar")
                          for r in _grasps(rec.get("segments") or [])]}
        width = state.shape[1]
        if width >= 14 and width % 7 == 0:
            action = np.stack(e["action"].to_numpy()).astype(np.float64)
            for arm in range(width // 7):
                if arm == main:
                    continue
                cols = slice(7 * arm, 7 * arm + 7)
                phases, _ = label_episode(state[:, cols], action[:, cols], params)
                per_arm[arm] = [(*r, False, "fsm") for r in _grasps(segments(phases))]

        for arm, rows in sorted(per_arm.items()):
            # 라벨이 데이터보다 길면(에피소드를 자른 뒤 재분석 안 함) 그 파지는 버린다 —
            # 마지막 프레임으로 눌러 담으면 엉뚱한 자세가 파지로 찍힌다.
            rows = [r for r in rows if r[1] < len(state)]
            if not rows:
                continue
            grip = 7 * arm + 6
            q = norm_to_rad(state[[r[1] for r in rows]][:, 7 * arm: 7 * arm + 6])
            # ⚠ 원점부터 뽑고 링크를 고른다. `tf[:, idx, :3, 3]` 처럼 목록 인덱스와 정수
            #   사이에 슬라이스가 끼면 numpy 가 고른 축을 **맨 앞으로** 보낸다 — (n,5,3)
            #   이 아니라 (5,n,3) 이 되고, 오류 없이 엉뚱한 점이 나온다.
            tf = link_transforms(q, g)
            pos = tf[:, :, :3, 3] * 1000
            joints = np.degrees(q)
            closing = tf[:, wrist_idx, :3, 1]          # link6 국소 y = 손가락 여닫는 축
            horiz = np.hypot(closing[:, 0], closing[:, 1])
            yaw = np.degrees(np.arctan2(closing[:, 1], closing[:, 0])) % 180
            stick = pos[:, stick_idx]
            tip = pos[:, finger_idx].mean(axis=1)
            label = _arm_label(names, arm)
            settle = max(1, int(round(fps / 2)))
            motion = _approach_motion(state, arm, rows, fps, g, finger_idx)
            for k, (cycle, frame, end, outcome, reviewed, source) in enumerate(rows):
                # 그리퍼만은 **닫기를 마친 뒤**를 본다. 시작에서는 거의 다 열려 있어(실측 95~100)
                # 아무것도 말해주지 않는다. ⚠ GRASP 구간 끝도 아직 닫히는 중일 수 있다 — FSM 은
                # 지령-실측 갭만 보고 n_hold 프레임 만에 HOLD 로 넘긴다. 그래서 끝에서 0.5초 더
                # 본 구간의 **최소** 열림: 물었으면 물체 폭, 빈손이면 0 근처다.
                closed = state[frame: min(end + 1 + settle, len(state)), grip].min()
                out["events"].append({
                    "episode": ep,
                    "cycle": cycle,
                    "frame": frame,
                    "t": round(frame / fps, 3),
                    "outcome": outcome,
                    "arm": label,
                    "source": source,
                    "reviewed": reviewed,
                    "joints": np.round(joints[k], 2).tolist(),
                    "gripper": round(float(closed), 2),
                    "tip": np.round(tip[k], 1).tolist(),
                    "yaw": round(float(yaw[k]), 1) if horiz[k] >= _YAW_MIN_HORIZONTAL else None,
                    "stick": np.round(np.vstack([stick[k], tip[k]]), 1).tolist(),
                    **motion[k],
                })
    return out


def _approach_motion(state, arm: int, rows, fps: float, g, finger_idx) -> list[dict]:
    """파지마다 접근·닫는 중 최고 속도(mm/s)와 말단 경로 — 모듈 머리말 "접근 동작".

    창을 전부 이어 붙여 FK 를 **한 번** 돌린다 — 파지마다 부르면 FK 호출이 파지 수만큼 는다.
    속도는 중심 차분이라 창 양끝에 한 프레임씩 여유를 더 읽는다.
    """
    from piper_robot.kinematics import link_transforms, norm_to_rad

    w = max(1, int(round(fps * _APPROACH_S)))
    last = len(state) - 1
    spans = [(max(0, r[1] - w - 1), min(last, max(r[2], r[1]) + 1)) for r in rows]
    idx = np.concatenate([np.arange(lo, hi + 1) for lo, hi in spans])
    q = norm_to_rad(state[idx][:, 7 * arm: 7 * arm + 6])
    # ⚠ 원점부터 뽑고 손가락 링크를 고른다 (extract 의 같은 주석 — 축이 앞으로 간다)
    tips = link_transforms(q, g)[:, :, :3, 3][:, finger_idx].mean(axis=1) * 1000

    out, at = [], 0
    for (lo, hi), (_cycle, frame, end, *_rest) in zip(spans, rows):
        tip = tips[at: at + hi - lo + 1]
        at += hi - lo + 1
        frames = np.arange(lo, hi + 1)
        speed = np.full(len(tip), np.nan)
        if len(tip) >= 3:
            speed[1:-1] = np.linalg.norm(tip[2:] - tip[:-2], axis=1) * fps / 2
        # ⚠ 닫기 시작 프레임의 속도는 **어느 쪽에도 안 넣는다.** 중심 차분이라 한 프레임 뒤를
        #   보므로, 넣으면 닫으며 튄 움직임이 "직전" 쪽에도 섞여 둘이 같은 값이 된다.
        pre = speed[(frames >= frame - w) & (frames < frame)]
        during = speed[(frames > frame) & (frames <= end)]
        keep = (frames >= frame - w) & (frames <= max(end, frame))

        def peak(v):
            v = v[~np.isnan(v)]
            return round(float(v.max()), 1) if len(v) else None

        out.append({
            "approach_speed": peak(pre),
            "closing_speed": peak(during),
            "trail": np.round(tip[keep], 1).tolist(),
            "trail_at": int(np.flatnonzero(frames[keep] == frame)[0]),
        })
    return out
