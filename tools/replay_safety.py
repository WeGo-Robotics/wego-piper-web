#!/usr/bin/env python3
"""녹화된 데이터셋을 안전 필터에 통과시켜 **발동률을 잰다**
(refactor/robotd-safety.md 4단계 — "로봇을 켜기 전에").

## 왜 이걸 먼저 하나

문서가 착수 순서에 못박아 뒀다: *"4단계 전에는 실제 팔에 필터를 걸지 않는다."*
발동률이 높으면 셋 중 하나인데, 어느 쪽인지를 **실제 팔로 알아내면 위험하다**:

1. 필터가 과하다 — 여유(clearance)를 태스크에 맞게 줄인다
2. 녹화가 과하다 — 조작자가 불필요하게 깊게 들어갔다
3. **캘리브레이션·지오메트리가 틀렸다** — 가장 위험한 경우

정상적으로 수집한 에피소드에서 발동률이 0 에 가깝지 않으면 3번을 의심해야 한다.

## 무엇을 재나

각 프레임의 `observation.state` 를 현재 자세, `action` 을 목표로 놓고 필터를
그대로 통과시킨다. 필터는 순수 함수라 하드웨어가 필요 없다.

사용:  python3 tools/replay_safety.py <데이터셋...> [--clearance 0.02]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "robot"))

from piper_robot import kinematics as K            # noqa: E402
from piper_robot import safety as S                # noqa: E402

LEROBOT = Path.home() / ".cache" / "huggingface" / "lerobot"


def resolve(name: str) -> Path:
    p = Path(name)
    if p.is_dir():
        return p
    for cand in (LEROBOT / name, *LEROBOT.glob(f"*/{name}")):
        if cand.is_dir():
            return cand
    raise SystemExit(f"데이터셋을 찾을 수 없습니다: {name}")


def replay(ds: Path, cfg: S.SafetyConfig) -> dict:
    frames = pd.concat([pd.read_parquet(f) for f in sorted((ds / "data").rglob("*.parquet"))])
    reasons: Counter = Counter()
    eps_hit: set[int] = set()
    total = 0
    worst = np.inf
    worst_ep = -1
    margins: list[float] = []

    for ep, g in frames.groupby("episode_index"):
        state = np.stack(g["observation.state"].values).astype(float)
        action = np.stack(g["action"].values).astype(float)
        if state.shape[1] < len(K.ARM_JOINTS):
            continue
        # 최저점은 프레임마다 필요하다 — 한 번에 계산해 여유를 본다
        z = K.lowest_z(K.norm_to_rad(state))
        margins.append(z.min())
        if z.min() < worst:
            worst, worst_ep = z.min(), int(ep)

        for t in range(len(state)):
            now = dict(zip(S.JOINT_ORDER, state[t]))
            goal = dict(zip(S.JOINT_ORDER, action[t]))
            _, reason = S.filter_goal(now, goal, cfg)
            total += 1
            if reason is not S.Reason.OK:
                reasons[reason.value] += 1
                if reason is S.Reason.FLOOR:
                    eps_hit.add(int(ep))

    return {
        "dataset": ds.name, "frames": total,
        "episodes": int(frames["episode_index"].nunique()),
        "reasons": reasons, "floor_episodes": sorted(eps_hit),
        "worst_z": worst, "worst_ep": worst_ep,
        "ep_min_z": np.array(margins),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("datasets", nargs="+")
    ap.add_argument("--min-z", default=None,
                    help="이 높이 아래로 못 간다 (m). 쉼표로 여러 값을 주면 훑는다")
    args = ap.parse_args()

    limits = ([float(x) for x in str(args.min_z).split(",")]
              if args.min_z is not None else [S.FloorConfig().min_z])

    for name in args.datasets:
      for mz in limits:
        cfg = S.SafetyConfig(floor=S.FloorConfig(min_z=mz))
        print(f"한계 {mz * 100:.1f}cm")
        r = replay(resolve(name), cfg)
        floor_n = r["reasons"].get("floor", 0)
        pct = 100 * floor_n / max(r["frames"], 1)
        print(f"── {r['dataset']}  에피소드 {r['episodes']}개 / {r['frames']:,}프레임")
        print(f"   바닥 발동   {floor_n:,} ({pct:.2f}%)  "
              f"에피소드 {len(r['floor_episodes'])}/{r['episodes']}")
        if r["reasons"]:
            other = {k: v for k, v in r["reasons"].items() if k != "floor"}
            if other:
                print(f"   그 밖의 사유 {other}")
        q = np.percentile(r["ep_min_z"], [0, 10, 50]) * 100
        print(f"   에피소드별 최저점: 최소 {q[0]:.1f} / 10% {q[1]:.1f} / 중앙 {q[2]:.1f} cm"
              f"   (최저 에피소드 #{r['worst_ep']})")
        if r["floor_episodes"]:
            print(f"   발동 에피소드: {r['floor_episodes'][:12]}"
                  f"{' …' if len(r['floor_episodes']) > 12 else ''}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
