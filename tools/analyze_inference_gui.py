#!/usr/bin/env python3
"""
추론 CSV 로그 GUI 분석기 (Plotly 인터랙티브).

브라우저에서 줌/팬/호버가 가능한 인터랙티브 차트를 생성합니다.

사용법:
  python tools/analyze_inference_gui.py                     # 파일 선택 메뉴
  python tools/analyze_inference_gui.py /tmp/piper_*.csv    # 직접 지정

의존성:
  pip install plotly pandas
"""

import argparse
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    print("plotly가 필요합니다: pip install plotly")
    sys.exit(1)


def find_logs(log_dir: str = "/tmp") -> list[Path]:
    return sorted(Path(log_dir).glob("piper_inference_*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)


def select_log(log_dir: str = "/tmp") -> str | None:
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


def load_log(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["time_s"] = df["timestamp"] - df["timestamp"].iloc[0]
    return df


def get_motor_names(df: pd.DataFrame) -> list[str]:
    return [c.removeprefix("target_") for c in df.columns if c.startswith("target_")]


def build_figure(df: pd.DataFrame, motors: list[str], csv_name: str) -> go.Figure:
    # 행 구성: 관절별 1행 + 오차 1행 + FPS/큐 1행
    n_joints = len(motors)
    total_rows = n_joints + 2  # joints + error overlay + fps/queue

    row_titles = [m for m in motors] + ["추적 오차 (target - actual)", "FPS / Queue"]
    heights = [1.0] * n_joints + [0.8, 0.6]
    total_h = sum(heights)
    heights = [h / total_h for h in heights]

    fig = make_subplots(
        rows=total_rows, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        subplot_titles=row_titles,
        row_heights=heights,
    )

    t = df["time_s"]
    colors_target = "rgba(100,100,255,0.4)"
    colors_filtered = "rgba(50,200,50,0.9)"
    colors_actual = "rgba(255,100,50,0.8)"

    # ── 관절별 target / filtered / actual ──
    for i, motor in enumerate(motors):
        row = i + 1
        target = pd.to_numeric(df[f"target_{motor}"], errors="coerce")
        filtered = pd.to_numeric(df[f"filtered_{motor}"], errors="coerce")
        actual = pd.to_numeric(df[f"actual_{motor}"], errors="coerce")

        show_legend = (i == 0)
        fig.add_trace(go.Scattergl(
            x=t, y=target, name="target (정책)", mode="lines",
            line=dict(color=colors_target, width=1),
            legendgroup="target", showlegend=show_legend,
        ), row=row, col=1)
        fig.add_trace(go.Scattergl(
            x=t, y=filtered, name="filtered (전송)", mode="lines",
            line=dict(color=colors_filtered, width=1.5),
            legendgroup="filtered", showlegend=show_legend,
        ), row=row, col=1)
        fig.add_trace(go.Scattergl(
            x=t, y=actual, name="actual (로봇)", mode="lines",
            line=dict(color=colors_actual, width=1.5, dash="dot"),
            legendgroup="actual", showlegend=show_legend,
        ), row=row, col=1)

    # ── 오차 오버레이 ──
    error_row = n_joints + 1
    palette = ["#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A", "#19D3F3", "#FF6692", "#B6E880"]
    for i, motor in enumerate(motors):
        target = pd.to_numeric(df[f"target_{motor}"], errors="coerce")
        actual = pd.to_numeric(df[f"actual_{motor}"], errors="coerce")
        error = target - actual
        color = palette[i % len(palette)]
        fig.add_trace(go.Scattergl(
            x=t, y=error, name=f"err:{motor}", mode="lines",
            line=dict(color=color, width=1),
            legendgroup=f"err_{motor}", showlegend=True,
        ), row=error_row, col=1)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", row=error_row, col=1)

    # ── FPS + Queue ──
    fq_row = n_joints + 2
    fig.add_trace(go.Scattergl(
        x=t, y=df["fps"], name="FPS", mode="lines",
        line=dict(color="#636EFA", width=1.5),
    ), row=fq_row, col=1)
    fig.add_trace(go.Scattergl(
        x=t, y=df["queue_size"], name="Queue", mode="lines",
        line=dict(color="#FFA15A", width=1.5), yaxis="y2",
    ), row=fq_row, col=1)

    # ── 큐 비었던 구간 하이라이트 ──
    empty_mask = df["queue_size"] == 0
    if empty_mask.any():
        starts = t[empty_mask & ~empty_mask.shift(1, fill_value=False)]
        ends = t[empty_mask & ~empty_mask.shift(-1, fill_value=False)]
        for s, e in zip(starts, ends):
            fig.add_vrect(
                x0=s, x1=e, fillcolor="red", opacity=0.06,
                layer="below", line_width=0,
                row=fq_row, col=1,
            )

    # ── 레이아웃 ──
    duration = df["time_s"].iloc[-1]
    empty_pct = (df["queue_size"] == 0).mean() * 100
    fig.update_layout(
        title=dict(text=f"{csv_name} — {len(df)} steps, {duration:.1f}s, queue empty {empty_pct:.1f}%"),
        height=250 * total_rows,
        template="plotly_dark",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_xaxes(title_text="Time (s)", row=total_rows, col=1)

    # range slider on bottom x-axis
    fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.03), row=total_rows, col=1)

    return fig


def print_stats(df: pd.DataFrame, motors: list[str]) -> None:
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


def main():
    parser = argparse.ArgumentParser(description="추론 CSV 로그 GUI 분석 (Plotly)")
    parser.add_argument("csv", nargs="?", default=None, help="CSV 파일 경로 (생략 시 선택 메뉴)")
    parser.add_argument("--dir", default="/tmp", help="로그 디렉토리 (기본: /tmp)")
    parser.add_argument("--joint", help="특정 관절만 표시 (예: joint1.pos)")
    parser.add_argument("--port", type=int, default=0, help="로컬 서버 포트 (0=HTML 파일 직접 열기)")
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

    csv_name = Path(csv_path).stem
    fig = build_figure(df, motors, csv_name)

    # HTML 저장 후 브라우저에서 열기
    html_path = Path(csv_path).with_suffix(".html")

    fig.write_html(str(html_path), include_plotlyjs="cdn")

    # x축 범위 변경 시 y축 자동 스케일 JS 삽입
    auto_scale_js = """
<script>
(function waitForPlotly() {
    var gd = document.querySelector('.plotly-graph-div');
    if (!gd || !gd._fullLayout || !gd._fullData) {
        setTimeout(waitForPlotly, 300); return;
    }

    var _busy = false;
    gd.on('plotly_relayout', function(ed) {
        if (_busy) return;

        // x축 범위: _fullLayout에서 직접 읽기 (rangeslider/zoom 모두 대응)
        var xr = gd._fullLayout.xaxis.range;
        if (!xr) return;
        var x0 = xr[0], x1 = xr[1];

        var update = {};
        gd._fullData.forEach(function(trace, ti) {
            if (!trace.x || !trace.y || !trace.yaxis) return;
            var yaxName = 'yaxis' + trace.yaxis.slice(1); // 'y' -> 'yaxis', 'y2' -> 'yaxis2'
            if (update[yaxName]) return; // 이미 이 축 처리함 — 아래서 통합

            // 같은 y축의 모든 트레이스에서 min/max 계산
            var ymin = Infinity, ymax = -Infinity;
            gd._fullData.forEach(function(tr) {
                if (tr.yaxis !== trace.yaxis) return;
                if (!tr.x || !tr.y) return;
                for (var i = 0; i < tr.x.length; i++) {
                    if (tr.x[i] >= x0 && tr.x[i] <= x1) {
                        var v = tr.y[i];
                        if (v !== null && v !== undefined && isFinite(v)) {
                            if (v < ymin) ymin = v;
                            if (v > ymax) ymax = v;
                        }
                    }
                }
            });

            if (isFinite(ymin) && isFinite(ymax)) {
                var pad = (ymax - ymin) * 0.05;
                if (pad === 0) pad = 1;
                update[yaxName + '.range'] = [ymin - pad, ymax + pad];
            }
        });

        if (Object.keys(update).length > 0) {
            _busy = true;
            Plotly.relayout(gd, update).then(function() { _busy = false; });
        }
    });

    console.log('Y-axis auto-scale enabled (' + gd._fullData.length + ' traces)');
})();
</script>
"""
    html_content = Path(html_path).read_text()
    html_content = html_content.replace("</body>", auto_scale_js + "\n</body>")
    Path(html_path).write_text(html_content)
    print(f"\nHTML 저장: {html_path}")
    print("브라우저에서 열기...")
    webbrowser.open(f"file://{html_path.resolve()}")


if __name__ == "__main__":
    main()
