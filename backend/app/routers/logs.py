"""로그 파일 통합 관리 — 조회 / 다운로드 / 삭제 / 차트 생성."""

import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/logs", tags=["logs"])

LOG_DIR = Path("/tmp")

# 카테고리별 로그 파일 패턴 (실행/디버그 로그만, 설정 파일 제외)
LOG_CATEGORIES = {
    "inference": {"dir": LOG_DIR, "glob": "piper_inference_*.csv", "label": "추론 CSV"},
    "inference_chart": {"dir": LOG_DIR, "glob": "piper_inference_*.html", "label": "추론 차트"},
    "debug": {"dir": LOG_DIR, "glob": "grpc_wrapper_debug.log", "label": "디버그"},
}


def _scan_category(cat_id: str, cat: dict) -> list[dict]:
    d = cat["dir"]
    if not d.exists():
        return []
    files = sorted(d.glob(cat["glob"]), key=lambda f: f.stat().st_mtime, reverse=True)
    results = []
    for f in files:
        st = f.stat()
        results.append({
            "name": f.name,
            "category": cat_id,
            "label": cat["label"],
            "size_kb": round(st.st_size / 1024, 1),
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "path": str(f),
        })
    return results


@router.get("")
async def list_logs(category: str = ""):
    """전체 로그 목록 (카테고리 필터 가능)."""
    results = []
    for cat_id, cat in LOG_CATEGORIES.items():
        if category and cat_id != category:
            continue
        results.extend(_scan_category(cat_id, cat))
    results.sort(key=lambda x: x["modified"], reverse=True)
    return results


@router.get("/categories")
async def list_categories():
    """로그 카테고리 목록 + 파일 수."""
    cats = []
    for cat_id, cat in LOG_CATEGORIES.items():
        count = len(_scan_category(cat_id, cat))
        cats.append({"id": cat_id, "label": cat["label"], "count": count})
    return cats


@router.get("/download/{filename}")
async def download_log(filename: str):
    """파일 다운로드 (모든 카테고리 검색)."""
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    for cat in LOG_CATEGORIES.values():
        path = cat["dir"] / filename
        if path.exists():
            media = "text/csv" if path.suffix == ".csv" else "application/octet-stream"
            return FileResponse(path, media_type=media, filename=filename)
    raise HTTPException(404, "File not found")


@router.delete("/delete/{filename}")
async def delete_log(filename: str):
    """파일 삭제."""
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    for cat in LOG_CATEGORIES.values():
        path = cat["dir"] / filename
        if path.exists():
            path.unlink()
            # 연관 HTML 차트도 삭제
            html_path = path.with_suffix(".html")
            if html_path.exists():
                html_path.unlink()
            logger.info("Deleted log: %s", path)
            return {"status": "deleted", "name": filename}
    raise HTTPException(404, "File not found")


@router.get("/chart/{filename}")
async def chart_log(filename: str):
    """CSV → Plotly 인터랙티브 차트 HTML."""
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    csv_path = LOG_DIR / filename
    if not csv_path.exists() or not csv_path.name.startswith("piper_inference_"):
        raise HTTPException(404, "Log file not found")

    # 캐시: HTML이 이미 있고 CSV보다 새로우면 재사용
    html_path = csv_path.with_suffix(".html")
    if html_path.exists() and html_path.stat().st_mtime >= csv_path.stat().st_mtime:
        return HTMLResponse(html_path.read_text())

    try:
        import pandas as pd
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        raise HTTPException(500, "plotly/pandas not installed: pip install plotly pandas")

    df = pd.read_csv(csv_path)
    df["time_s"] = df["timestamp"] - df["timestamp"].iloc[0]
    motors = [c.removeprefix("target_") for c in df.columns if c.startswith("target_")]
    if not motors:
        raise HTTPException(400, "No motor columns in CSV")

    t = df["time_s"]
    n_joints = len(motors)
    total_rows = n_joints + 2
    row_titles = motors + ["추적 오차 (target - actual)", "FPS / Queue"]
    heights = [1.0] * n_joints + [0.8, 0.6]
    total_h = sum(heights)
    heights = [h / total_h for h in heights]

    fig = make_subplots(rows=total_rows, cols=1, shared_xaxes=True,
                        vertical_spacing=0.02, subplot_titles=row_titles, row_heights=heights)

    c_target = "rgba(100,100,255,0.4)"
    c_filtered = "rgba(50,200,50,0.9)"
    c_actual = "rgba(255,100,50,0.8)"
    palette = ["#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A", "#19D3F3", "#FF6692", "#B6E880"]

    has_filtered = any(c.startswith("filtered_") for c in df.columns)
    for i, motor in enumerate(motors):
        row = i + 1
        target = pd.to_numeric(df[f"target_{motor}"], errors="coerce")
        actual = pd.to_numeric(df[f"actual_{motor}"], errors="coerce")
        show = (i == 0)
        fig.add_trace(go.Scattergl(x=t, y=target, name="target", mode="lines",
            line=dict(color=c_target, width=1), legendgroup="target", showlegend=show), row=row, col=1)
        if has_filtered:
            filtered = pd.to_numeric(df[f"filtered_{motor}"], errors="coerce")
            fig.add_trace(go.Scattergl(x=t, y=filtered, name="filtered", mode="lines",
                line=dict(color=c_filtered, width=1.5), legendgroup="filtered", showlegend=show), row=row, col=1)
        fig.add_trace(go.Scattergl(x=t, y=actual, name="actual", mode="lines",
            line=dict(color=c_actual, width=1.5, dash="dot"), legendgroup="actual", showlegend=show), row=row, col=1)

    err_row = n_joints + 1
    for i, motor in enumerate(motors):
        target = pd.to_numeric(df[f"target_{motor}"], errors="coerce")
        actual = pd.to_numeric(df[f"actual_{motor}"], errors="coerce")
        fig.add_trace(go.Scattergl(x=t, y=target - actual, name=f"err:{motor}", mode="lines",
            line=dict(color=palette[i % len(palette)], width=1)), row=err_row, col=1)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", row=err_row, col=1)

    fq_row = n_joints + 2
    fig.add_trace(go.Scattergl(x=t, y=df["fps"], name="FPS", mode="lines",
        line=dict(color="#636EFA", width=1.5)), row=fq_row, col=1)
    fig.add_trace(go.Scattergl(x=t, y=df["queue_size"], name="Queue", mode="lines",
        line=dict(color="#FFA15A", width=1.5)), row=fq_row, col=1)

    duration = df["time_s"].iloc[-1]
    empty_pct = (df["queue_size"] == 0).mean() * 100
    fig.update_layout(
        title=dict(text=f"{csv_path.stem} — {len(df)} steps, {duration:.1f}s, queue empty {empty_pct:.1f}%"),
        height=250 * total_rows, template="plotly_dark",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_xaxes(title_text="Time (s)", row=total_rows, col=1)
    fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.03), row=total_rows, col=1)

    html = fig.to_html(include_plotlyjs="cdn", full_html=True)

    # y축 자동 스케일 JS 삽입
    auto_scale_js = """<script>
(function waitGd(){var gd=document.querySelector('.plotly-graph-div');
if(!gd||!gd._fullData){setTimeout(waitGd,300);return}
var busy=false;gd.on('plotly_relayout',function(ed){
if(busy)return;var xr=gd._fullLayout.xaxis.range;if(!xr)return;
var x0=xr[0],x1=xr[1],up={};
gd._fullData.forEach(function(tr){if(!tr.x||!tr.y||!tr.yaxis)return;
var ak='yaxis'+tr.yaxis.slice(1);if(up[ak])return;
var mn=Infinity,mx=-Infinity;
gd._fullData.forEach(function(t2){if(t2.yaxis!==tr.yaxis||!t2.x||!t2.y)return;
for(var i=0;i<t2.x.length;i++){if(t2.x[i]>=x0&&t2.x[i]<=x1){var v=t2.y[i];
if(v!=null&&isFinite(v)){if(v<mn)mn=v;if(v>mx)mx=v;}}}});
if(isFinite(mn)&&isFinite(mx)){var p=(mx-mn)*0.05||1;up[ak+'.range']=[mn-p,mx+p];}});
if(Object.keys(up).length>0){busy=true;Plotly.relayout(gd,up).then(function(){busy=false;});}});
})();</script>"""
    html = html.replace("</body>", auto_scale_js + "\n</body>")

    html_path.write_text(html)
    logger.info("Generated chart: %s", html_path)
    return HTMLResponse(html)
