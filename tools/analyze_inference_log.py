#!/usr/bin/env python3
"""
추론 CSV 로그 분석 및 시각화.

사용법:
  python tools/analyze_inference_log.py /tmp/piper_inference_YYYYMMDD_HHMMSS.csv
  python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --joint joint1.pos
  python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --save  # PNG 저장
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import numpy as np


def load_log(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["time_s"] = df["timestamp"] - df["timestamp"].iloc[0]
    return df


def get_motor_names(df: pd.DataFrame) -> list[str]:
    return [c.removeprefix("target_") for c in df.columns if c.startswith("target_")]


def plot_joint(df: pd.DataFrame, joint: str, ax: plt.Axes) -> None:
    """하나의 관절에 대해 target / filtered / actual 비교 플롯."""
    t = df["time_s"]
    target = pd.to_numeric(df[f"target_{joint}"], errors="coerce")
    filtered = pd.to_numeric(df[f"filtered_{joint}"], errors="coerce")
    actual = pd.to_numeric(df[f"actual_{joint}"], errors="coerce")

    ax.plot(t, target, label="target (정책 출력)", alpha=0.5, linewidth=0.8)
    ax.plot(t, filtered, label="filtered (전송값)", alpha=0.8, linewidth=1.0)
    ax.plot(t, actual, label="actual (로봇 위치)", alpha=0.8, linewidth=1.0, linestyle="--")
    ax.set_ylabel(joint)
    ax.legend(fontsize=7, loc="upper right")
    ax.grid(True, alpha=0.3)


def plot_error(df: pd.DataFrame, joint: str, ax: plt.Axes) -> None:
    """target vs actual 오차 플롯."""
    t = df["time_s"]
    target = pd.to_numeric(df[f"target_{joint}"], errors="coerce")
    actual = pd.to_numeric(df[f"actual_{joint}"], errors="coerce")
    error = target - actual

    ax.fill_between(t, error, alpha=0.4, label=f"error (target-actual)")
    ax.axhline(0, color="gray", linewidth=0.5)
    ax.set_ylabel(f"{joint} error")
    ax.legend(fontsize=7, loc="upper right")
    ax.grid(True, alpha=0.3)


def plot_overview(df: pd.DataFrame, motors: list[str], save_path: str | None = None) -> None:
    """전체 관절 target/filtered/actual 비교."""
    n = len(motors)
    fig, axes = plt.subplots(n, 1, figsize=(14, 2.5 * n), sharex=True)
    if n == 1:
        axes = [axes]
    for ax, motor in zip(axes, motors):
        plot_joint(df, motor, ax)
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle("Inference Log: Target vs Filtered vs Actual", fontsize=12)
    fig.tight_layout()
    if save_path:
        fig.savefig(save_path, dpi=150)
        print(f"Saved: {save_path}")
    else:
        plt.show()


def plot_error_overview(df: pd.DataFrame, motors: list[str], save_path: str | None = None) -> None:
    """전체 관절 오차 플롯."""
    n = len(motors)
    fig, axes = plt.subplots(n, 1, figsize=(14, 2 * n), sharex=True)
    if n == 1:
        axes = [axes]
    for ax, motor in zip(axes, motors):
        plot_error(df, motor, ax)
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle("Tracking Error (Target - Actual)", fontsize=12)
    fig.tight_layout()
    if save_path:
        fig.savefig(save_path, dpi=150)
        print(f"Saved: {save_path}")
    else:
        plt.show()


def plot_fps_queue(df: pd.DataFrame, save_path: str | None = None) -> None:
    """FPS + 큐 사이즈 시계열."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 5), sharex=True)
    t = df["time_s"]

    ax1.plot(t, df["fps"], linewidth=0.8)
    ax1.set_ylabel("FPS")
    ax1.axhline(30, color="red", linewidth=0.5, linestyle="--", label="target 30")
    ax1.legend(fontsize=7)
    ax1.grid(True, alpha=0.3)

    ax2.plot(t, df["queue_size"], linewidth=0.8, color="orange")
    ax2.set_ylabel("Queue Size")
    ax2.set_xlabel("Time (s)")
    ax2.grid(True, alpha=0.3)

    fig.suptitle("FPS & Action Queue", fontsize=12)
    fig.tight_layout()
    if save_path:
        fig.savefig(save_path, dpi=150)
        print(f"Saved: {save_path}")
    else:
        plt.show()


def print_stats(df: pd.DataFrame, motors: list[str]) -> None:
    """통계 요약."""
    duration = df["time_s"].iloc[-1]
    print(f"\n{'='*60}")
    print(f"Duration: {duration:.1f}s | Steps: {len(df)} | Avg FPS: {len(df)/max(duration,1):.1f}")
    print(f"Queue empty: {(df['queue_size']==0).sum()}/{len(df)} ({(df['queue_size']==0).mean()*100:.1f}%)")
    print(f"{'='*60}")
    print(f"{'Joint':<20} {'|target-actual| mean':>20} {'max':>10} {'std':>10}")
    print(f"{'-'*60}")
    for m in motors:
        target = pd.to_numeric(df[f"target_{m}"], errors="coerce")
        actual = pd.to_numeric(df[f"actual_{m}"], errors="coerce")
        err = (target - actual).abs().dropna()
        if len(err) == 0:
            continue
        print(f"{m:<20} {err.mean():>20.3f} {err.max():>10.3f} {err.std():>10.3f}")

    # 필터 효과 (target vs filtered 차이)
    print(f"\n{'Joint':<20} {'|target-filtered| mean':>22} {'max':>10}")
    print(f"{'-'*52}")
    for m in motors:
        target = pd.to_numeric(df[f"target_{m}"], errors="coerce")
        filtered = pd.to_numeric(df[f"filtered_{m}"], errors="coerce")
        diff = (target - filtered).abs().dropna()
        if len(diff) == 0:
            continue
        print(f"{m:<20} {diff.mean():>22.3f} {diff.max():>10.3f}")


def find_logs(log_dir: str = "/tmp") -> list[Path]:
    """로그 파일을 최신순으로 정렬하여 반환."""
    files = sorted(Path(log_dir).glob("piper_inference_*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
    return files


def select_log(log_dir: str = "/tmp") -> str | None:
    """대화형 로그 파일 선택."""
    files = find_logs(log_dir)
    if not files:
        print(f"로그 파일 없음 ({log_dir}/piper_inference_*.csv)")
        return None

    print(f"\n{'='*60}")
    print(f" 추론 로그 파일 ({len(files)}개)")
    print(f"{'='*60}")
    for i, f in enumerate(files):
        size_kb = f.stat().st_size / 1024
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        marker = " (최신)" if i == 0 else ""
        print(f"  [{i+1}] {f.name}  {size_kb:>8.1f} KB  {mtime}{marker}")
    print(f"{'='*60}")

    while True:
        choice = input(f"\n파일 선택 (1-{len(files)}, Enter=최신, q=종료): ").strip()
        if choice == "q":
            return None
        if choice == "":
            return str(files[0])
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(files):
                return str(files[idx])
        except ValueError:
            pass
        print(f"  1~{len(files)} 사이 숫자를 입력하세요.")


def main():
    parser = argparse.ArgumentParser(description="추론 CSV 로그 분석")
    parser.add_argument("csv", nargs="?", default=None, help="CSV 파일 경로 (생략 시 선택 메뉴)")
    parser.add_argument("--dir", default="/tmp", help="로그 디렉토리 (기본: /tmp)")
    parser.add_argument("--joint", help="특정 관절만 표시 (예: joint1.pos)")
    parser.add_argument("--save", action="store_true", help="PNG 파일로 저장 (GUI 없이)")
    parser.add_argument("--no-plot", action="store_true", help="통계만 출력")
    args = parser.parse_args()

    csv_path = args.csv
    if csv_path is None:
        csv_path = select_log(args.dir)
        if csv_path is None:
            sys.exit(0)

    print(f"\n로드: {csv_path}")
    df = load_log(csv_path)
    motors = get_motor_names(df)
    if not motors:
        print("No motor columns found in CSV")
        sys.exit(1)

    if args.joint:
        motors = [m for m in motors if args.joint in m]
        if not motors:
            print(f"Joint '{args.joint}' not found. Available: {get_motor_names(df)}")
            sys.exit(1)

    print_stats(df, motors)

    if args.no_plot:
        return

    base = Path(csv_path).stem
    save_dir = Path(csv_path).parent

    plot_overview(df, motors, str(save_dir / f"{base}_overview.png") if args.save else None)
    plot_error_overview(df, motors, str(save_dir / f"{base}_error.png") if args.save else None)
    plot_fps_queue(df, str(save_dir / f"{base}_fps_queue.png") if args.save else None)


if __name__ == "__main__":
    main()
