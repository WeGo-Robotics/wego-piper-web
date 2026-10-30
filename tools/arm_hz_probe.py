#!/usr/bin/env python3
"""추론 중 팔을 100Hz 로 기록해 **떨림이 어느 주파수에 있나**를 본다.

## 왜

청크 이어 붙이기(wrapper/chunk_merge.py) 뒤에도 떨림이 남으면 **어디서** 생기는지가 처방을 정한다.

  (가) robotd 가 명령이 올 때만(15Hz) MOVE J 를 보낸다 — 팔은 67ms 마다 계단을 받는다.
       → 명령 주기(f_cmd)와 그 배음에 몰린다. 처방은 robotd 의 100Hz 보간.
  (나) 서보·기구의 고유 진동 — f_cmd 와 무관한 자리에 **두드러진** 봉우리가 서고, 명령엔 그게 없다.
  (다) 팔은 명령을 그대로 따른다 — 떨림이 **명령(정책 출력) 안에** 있다. 처방은 정책·청크 쪽.

가르는 잣대는 대역별로 실제 팔과 **매끄러운 명령**(명령 사이를 선형으로 이은 것 — 계단은 팔이
걸러 낸다)을 견주는 것이다. 실제가 명령보다 크면 팔이 보탠 것이다.

추론 CSV 는 제어 주기(15Hz)로 찍혀 7.5Hz 위가 원리상 안 보이고, 15Hz 성분은 0Hz 로 접혀
사라진다. 그래서 robotd 가 내는 100Hz 상태를 따로 받는다.

⚠ 15Hz CSV 로 "실제 가속 p99 > 명령 가속 p99 → 팔이 보탠다"고 읽었다가 틀렸다(2026-10-28).
  100Hz 로 재 보니 팔은 계단을 15배 걸러 내고 2~10Hz 는 명령과 같았다 — (다). 15Hz 표본의 팔은
  계단에 대한 반응이 접혀 들어가 명령과 견줄 수 없다.

## 무엇을 읽나 — 읽기만 한다

    /dev/shm/piper.arm.<iface>.state    robotd 가 100Hz 로 쓰는 관절 상태
    /dev/shm/piper.arm.<iface>.action   추론이 쓰는 명령 (추론 중에만 있다)

리더는 `PROT_READ` 로 열고 읽은 위치를 이쪽에만 기억한다 — robotd 의 소비에 영향이 없다.
말단은 FK(link6 플랜지, piper_robot.kinematics)로 mm 로 바꿔 본다.

사용:
    python3 tools/arm_hz_probe.py                    # 추론이 시작되길 기다렸다가 30초 기록 + 분석
    python3 tools/arm_hz_probe.py --iface can2 --seconds 60
    python3 tools/arm_hz_probe.py --analyze /tmp/piper_armprobe_<시각>.npz
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

import numpy as np

# 의도된 움직임(수 Hz 아래)을 걷어내는 이동평균 길이. 이보다 느린 것은 "떨림"으로 안 센다.
DETREND_S = 0.5
# 떨림으로 보는 하한. 이동평균이 이 근처까지 덜 걷어내므로 그 아래는 빼고 센다.
MIN_HZ = 2.0
# 명령 주기 배음을 셀 때 양옆 폭
HARMONIC_BAND_HZ = 1.5
# 이 몫 이상이 명령 주기·배음에 있으면 (가) 로 본다
STAIRCASE_SHARE = 0.4
# 공진이라 부르려면 봉우리 하나가 떨림의 이만큼은 가져야 한다 — 평평한 스펙트럼의 최댓값은 봉우리가 아니다
PEAK_SHARE = 0.15
# 실제가 매끄러운 명령의 이 배 안이면 "팔이 명령을 따른다". 넘어도 그 차이가 전체 떨림의
# FOLLOW_FLOOR 몫보다 작으면 안 센다 — 명령이 거의 0 인 대역에선 비율만 터진다(0.038/0.004mm)
FOLLOW_RATIO = 1.25
FOLLOW_FLOOR = 0.1
FOLLOW_BANDS = ((2.0, 5.0), (5.0, 10.0), (10.0, 20.0))
POLL_S = 0.0005


# ── 기록 ─────────────────────────────────────────────────────────────────

def _action_ifaces() -> list[str]:
    from piper_shm import arm as A

    return [s.rsplit(".", 1)[0] for s in A.list_segments() if s.endswith(".action")]


def record(iface: str | None, seconds: float, wait_s: float) -> dict:
    """상태(·명령)를 기록한다. 추론이 끝나면(명령 세그먼트가 사라지면) 거기서 멈춘다."""
    from piper_shm import arm as A

    deadline = time.monotonic() + wait_s
    while iface is None:
        found = _action_ifaces()
        if found:
            iface = found[0]
            break
        if time.monotonic() >= deadline:
            raise SystemExit(f"{wait_s:.0f}초 동안 추론(명령 세그먼트)이 안 보였습니다 — --iface 로 지정하면 바로 기록합니다")
        time.sleep(0.2)

    state = A.StateReader(iface)
    try:
        action = A.ActionReader(iface)
    except A.ArmSegmentError:
        action = None       # 추론이 없으면 상태만 — 정지 상태의 바닥 잡음을 재는 데 쓴다
    print(f"기록: {iface} {seconds:.0f}초 (명령 {'있음' if action else '없음'})", file=sys.stderr, flush=True)

    st_t, st_can, st_q, ac_t, ac_q = [], [], [], [], []
    s_seq = a_seq = 0
    end = time.monotonic() + seconds
    stopped = ""
    while time.monotonic() < end:
        if state.seq() > s_seq:
            got = state.read()
            if got is not None:
                s_seq = got["seq"]
                st_t.append(time.monotonic())
                st_can.append(got["can_wall_ns"])
                st_q.append([got["values"][j] for j in A.JOINTS])
        if action is not None:
            if action.orphaned:
                stopped = "추론이 끝나 명령 세그먼트가 사라졌다"
                break
            if action.seq() > a_seq:
                got = action.read()
                if got is not None:
                    a_seq = got["seq"]
                    ac_t.append(time.monotonic())
                    ac_q.append([got["values"][j] for j in A.JOINTS])
        if state.orphaned:
            stopped = "robotd 가 재시작해 상태 세그먼트가 바뀌었다"
            break
        time.sleep(POLL_S)
    state.close()
    if action is not None:
        action.close()
    if stopped:
        print(f"기록 중단: {stopped}", file=sys.stderr)
    return {
        "iface": iface, "joints": list(A.JOINTS),
        "state_t": np.array(st_t), "state_can_ns": np.array(st_can, dtype=np.int64),
        "state_q": np.array(st_q, dtype=float).reshape(-1, 7),
        "action_t": np.array(ac_t), "action_q": np.array(ac_q, dtype=float).reshape(-1, 7),
    }


# ── 분석 (하드웨어 없이 돈다) ─────────────────────────────────────────────

def _uniform(t: np.ndarray, x: np.ndarray, fs: float, hold: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """균일 격자로 옮긴다. 상태는 선형 보간, 명령은 **영차 유지**(팔이 실제로 받는 모양)."""
    grid = np.arange(t[0], t[-1], 1.0 / fs)
    if hold:
        idx = np.clip(np.searchsorted(t, grid, side="right") - 1, 0, len(t) - 1)
        return grid, x[idx]
    return grid, np.stack([np.interp(grid, t, x[:, i]) for i in range(x.shape[1])], axis=1)


def _detrend(x: np.ndarray, fs: float) -> np.ndarray:
    """중심 이동평균을 뺀다 — 의도된 느린 움직임을 걷어낸 나머지가 떨림이다."""
    n = max(3, int(round(DETREND_S * fs)) | 1)
    k = np.ones(n) / n
    pad = n // 2
    xp = np.pad(x, ((pad, pad), (0, 0)), mode="edge")
    smooth = np.stack([np.convolve(xp[:, i], k, mode="valid") for i in range(x.shape[1])], axis=1)
    return x - smooth


def _psd(x: np.ndarray, fs: float, nseg: int = 256) -> tuple[np.ndarray, np.ndarray]:
    """Welch PSD (Hann, 50% 겹침), 차원을 합친다. 단위는 (입력 단위)²/Hz."""
    nseg = min(nseg, len(x))
    win = np.hanning(nseg)
    step = nseg // 2
    acc, count = None, 0
    for s in range(0, len(x) - nseg + 1, step):
        seg = x[s:s + nseg] - x[s:s + nseg].mean(axis=0)
        spec = np.abs(np.fft.rfft(seg * win[:, None], axis=0)) ** 2
        acc = spec if acc is None else acc + spec
        count += 1
    psd = acc.sum(axis=1) / count / (fs * (win ** 2).sum())
    return np.fft.rfftfreq(nseg, 1.0 / fs), psd


def _ee_mm(q: np.ndarray) -> np.ndarray:
    from piper_robot import kinematics as K

    return K.endpoint_xyz(K.norm_to_rad(q[:, :6])) * 1000.0


def _bands(f: np.ndarray, p: np.ndarray, f_cmd: float | None, fs: float) -> dict:
    """떨림 대역(MIN_HZ 이상)에서의 몫. 명령 주기·배음 몫과 봉우리 셋."""
    use = f >= MIN_HZ
    total = p[use].sum()
    out = {"rms_mm": float(np.sqrt(p[use].sum() * (f[1] - f[0])))}
    if total <= 0:
        return {**out, "cmd_share": 0.0, "peaks": []}
    harm = np.zeros_like(use)
    if f_cmd:
        k = 1
        while k * f_cmd < fs / 2:
            harm |= np.abs(f - k * f_cmd) <= HARMONIC_BAND_HZ
            k += 1
    out["cmd_share"] = float(p[use & harm].sum() / total)
    # 봉우리: 이웃보다 큰 칸 중 몫이 큰 셋
    idx = [i for i in range(1, len(p) - 1) if use[i] and p[i] >= p[i - 1] and p[i] >= p[i + 1]]
    idx.sort(key=lambda i: -p[i])
    out["peaks"] = [{"hz": round(float(f[i]), 2), "share": round(float(p[i] / total), 3),
                     "cmd_harmonic": bool(harm[i])} for i in idx[:3]]
    return out


def analyze(data: dict) -> dict:
    st_t, st_q = np.asarray(data["state_t"]), np.asarray(data["state_q"])
    if len(st_t) < 300:
        raise SystemExit(f"상태 표본이 {len(st_t)}개뿐입니다 — 최소 3초 이상 기록하세요")
    dt = np.diff(st_t)
    fs = round(1.0 / float(np.median(dt)))
    span = float(st_t[-1] - st_t[0])
    can = np.asarray(data.get("state_can_ns", []))
    out = {
        "iface": data.get("iface"), "seconds": round(span, 1),
        "state_hz": fs, "state_gap_p99_ms": round(float(np.percentile(dt, 99) * 1000), 1),
        # robotd 는 100Hz 로 쓰지만 CAN 피드백이 그보다 느리면 같은 값이 반복된다 — 실제 해상도
        "feedback_hz": round(len(np.unique(can)) / span, 1) if len(can) else None,
    }
    ac_t, ac_q = np.asarray(data.get("action_t", [])), np.asarray(data.get("action_q", []))
    f_cmd = None
    if len(ac_t) > 30:
        f_cmd = round(1.0 / float(np.median(np.diff(ac_t))), 2)
    out["cmd_hz"] = f_cmd

    _, sq = _uniform(st_t, st_q, fs)
    r_act = _detrend(_ee_mm(sq), fs)
    f, p_act = _psd(r_act, fs)
    out["actual"] = _bands(f, p_act, f_cmd, fs)

    if f_cmd:
        # 같은 구간의 명령 — 영차 유지라 계단이 그대로 들어간다
        lo, hi = max(st_t[0], ac_t[0]), min(st_t[-1], ac_t[-1])
        m = (ac_t >= lo) & (ac_t <= hi)
        if m.sum() > 30:
            _, cq = _uniform(ac_t[m], ac_q[m], fs, hold=True)
            r_cmd = _detrend(_ee_mm(cq), fs)
            f2, p_cmd = _psd(r_cmd, fs)
            out["command"] = _bands(f2, p_cmd, f_cmd, fs)
            # 매끄러운 명령(선형) 과 대역별로 견준다 — 팔이 보탰나, 그대로 따랐나
            _, cl = _uniform(ac_t[m], ac_q[m], fs)
            f3, p_lin = _psd(_detrend(_ee_mm(cl), fs), fs)
            df = f[1] - f[0]
            out["follow"] = [
                {"band": [a, b],
                 "actual_mm": round(float(np.sqrt(p_act[(f >= a) & (f < b)].sum() * df)), 3),
                 "command_mm": round(float(np.sqrt(p_lin[(f3 >= a) & (f3 < b)].sum() * df)), 3)}
                for a, b in FOLLOW_BANDS
            ]

    # 관절별 기여: 관절 하나에만 떨림을 남기고(나머지는 매끄럽게) 말단에 생기는 떨림 RMS
    sm = sq - _detrend(sq, fs)
    base = _ee_mm(sm)
    contrib = []
    for i in range(6):
        q = sm.copy()
        q[:, i] = sq[:, i]
        d = _detrend(_ee_mm(q) - base, fs)
        contrib.append(round(float(np.sqrt((d ** 2).sum(1).mean())), 3))
    out["joint_contrib_mm"] = contrib
    out["verdict"] = _verdict(out)
    return out


def _verdict(s: dict) -> str:
    a = s["actual"]
    if not s.get("cmd_hz"):
        return "명령이 없는 구간이다(추론 중이 아님) — 정지 상태의 바닥 잡음으로만 읽는다"
    if a["cmd_share"] >= STAIRCASE_SHARE:
        return (f"(가) 명령 주기 계단 — 떨림의 {a['cmd_share']:.0%} 가 {s['cmd_hz']}Hz·배음에 있다. "
                "robotd 에서 명령 사이를 100Hz 로 보간하면 걷힌다")
    follow = s.get("follow", [])
    floor = FOLLOW_FLOOR * a["rms_mm"]
    exceed = [b for b in follow if b["actual_mm"] > FOLLOW_RATIO * b["command_mm"]
              and b["actual_mm"] - b["command_mm"] > floor]
    top = a["peaks"][0] if a["peaks"] else None
    if top and not top["cmd_harmonic"] and top["share"] >= PEAK_SHARE and any(
            b["band"][0] <= top["hz"] < b["band"][1] for b in exceed):
        return (f"(나) 명령에 없는 봉우리 {top['hz']}Hz (몫 {top['share']:.0%}) — 팔이 보탠 진동이다. "
                "서보 이득·기구 공진 쪽을 본다")
    low = [b for b in follow if b["band"][1] <= 10.0]
    if low and not any(b in exceed for b in low):
        parts = ", ".join(f"{b['band'][0]:g}-{b['band'][1]:g}Hz {b['actual_mm']}/{b['command_mm']}mm" for b in low)
        return (f"(다) 팔은 명령을 그대로 따른다 (실제/명령 {parts}) — 떨림은 명령(정책 출력) 안에 있다. "
                "robotd·서보가 아니라 정책·청크 쪽을 본다")
    return f"뚜렷하지 않다 — 명령 주기 몫 {a['cmd_share']:.0%}, 명령보다 큰 대역 {[b['band'] for b in exceed]}"


def report(s: dict) -> str:
    def line(name: str, b: dict) -> str:
        peaks = ", ".join(f"{p['hz']}Hz {p['share']:.0%}{'*' if p['cmd_harmonic'] else ''}" for p in b["peaks"])
        return f"  {name:<6} 떨림 RMS {b['rms_mm']:.2f}mm | 명령주기·배음 몫 {b['cmd_share']:.0%} | 봉우리 {peaks}"

    rows = [
        f"{s['iface']} {s['seconds']}초 — 상태 {s['state_hz']}Hz(틈 p99 {s['state_gap_p99_ms']}ms), "
        f"CAN 피드백 {s['feedback_hz']}Hz, " + (f"명령 {s['cmd_hz']}Hz" if s["cmd_hz"] else "명령 없음"),
        line("실제", s["actual"]),
    ]
    if "command" in s:
        rows.append(line("명령", s["command"]))
    if s.get("follow"):
        rows.append("  대역별 실제/매끄러운 명령(mm): " + ", ".join(
            f"{b['band'][0]:g}-{b['band'][1]:g}Hz {b['actual_mm']}/{b['command_mm']}" for b in s["follow"]))
    rows.append(f"  관절별 말단 기여(mm) J1~J6: {s['joint_contrib_mm']}")
    rows.append(f"  (* = 명령 주기 배음, 떨림 = {MIN_HZ:.0f}Hz 이상)")
    rows.append(f"→ {s['verdict']}")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iface", help="팔 인터페이스 (기본: 추론이 쓰는 팔을 기다렸다 고른다)")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--wait", type=float, default=600.0, help="추론 시작을 기다리는 최대 초")
    ap.add_argument("--out", help="기록 파일 (.npz). 기본 /tmp/piper_armprobe_<시각>.npz")
    ap.add_argument("--analyze", metavar="NPZ", help="기록하지 않고 저장된 기록만 분석")
    a = ap.parse_args()

    if a.analyze:
        data = dict(np.load(a.analyze, allow_pickle=False))
        data["iface"] = str(data.get("iface", ""))
    else:
        data = record(a.iface, a.seconds, a.wait)
        out = Path(a.out or f"/tmp/piper_armprobe_{datetime.datetime.now():%Y%m%d_%H%M%S}.npz")
        np.savez(out, **{k: (np.array(v) if not isinstance(v, np.ndarray) else v) for k, v in data.items()})
        print(f"저장: {out}", file=sys.stderr)
    summary = analyze(data)
    print(report(summary))
    if not a.analyze:
        out.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
