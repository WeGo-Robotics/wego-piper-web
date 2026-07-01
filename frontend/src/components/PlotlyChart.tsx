import { useEffect, useMemo, useRef } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import type { Data, Layout } from 'plotly.js'

// dist-min 번들로 Plot 컴포넌트 생성 (소스에서 plotly 빌드 회피)
const Plot = createPlotlyComponent(Plotly)

export type Series = { label: string; color: string; data: (number | null)[]; dash?: boolean }

type Props = {
  x: number[]
  series: Series[]
  markerX?: number | null
  height?: number
  /** 줌/팬(UI) 유지 키. 값이 같으면 데이터 갱신 시에도 줌 유지, 바뀌면 오토스케일 리셋. */
  uirevision?: string
  yTitle?: string
}

/**
 * Plotly 기반 인터랙티브 라인 차트 — 박스 줌(드래그) / 휠 줌 / 팬 / 더블클릭 오토스케일 리셋.
 * 휠 줌은 기본적으로 X·Y 동시 확대. Shift+휠 = X축만, Alt+휠 = Y축만 확대.
 */
export default function PlotlyChart({ x, series, markerX = null, height = 220, uirevision = 'chart', yTitle }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)

  // Shift+휠 → X축만, Alt+휠 → Y축만. 수정자 키가 없으면 Plotly 기본(양축) 동작에 맡긴다.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const onWheel = (ev: WheelEvent) => {
      const axis = ev.shiftKey ? 'x' : ev.altKey ? 'y' : null
      if (!axis) return // 수정자 키 없음 → Plotly 기본 양축 줌

      const gd = container.querySelector<HTMLElement>('.js-plotly-plot')
      const drag = gd?.querySelector<SVGRectElement>('.nsewdrag')
      const full = (gd as any)?._fullLayout
      if (!gd || !drag || !full?.xaxis || !full?.yaxis) return

      // Plotly의 기본 휠 줌으로 이벤트가 전파되지 않도록 가로챈다.
      ev.preventDefault()
      ev.stopPropagation()

      const rect = drag.getBoundingClientRect()
      const factor = ev.deltaY > 0 ? 1.1 : 1 / 1.1 // 아래로 스크롤 = 축소
      const ax = axis === 'x' ? full.xaxis : full.yaxis
      const px = axis === 'x' ? ev.clientX - rect.left : ev.clientY - rect.top
      const [r0, r1] = ax.range
      const center = ax.p2d(px) // 커서 위치의 데이터 좌표 = 확대 기준점
      const n0 = center - (center - r0) * factor
      const n1 = center + (r1 - center) * factor

      ;(Plotly as any).relayout(gd, { [`${axis}axis.range`]: [n0, n1] })
    }

    // 캡처 단계로 등록해 Plotly 내부 드래그 레이어의 휠 핸들러보다 먼저 처리한다.
    container.addEventListener('wheel', onWheel, { capture: true, passive: false })
    return () => container.removeEventListener('wheel', onWheel, { capture: true } as any)
  }, [])

  const data = useMemo<Data[]>(
    () =>
      series.map((s) => ({
        type: 'scattergl',
        mode: 'lines',
        name: s.label,
        x,
        y: s.data,
        connectgaps: false,
        line: { color: s.color, width: 1.6, dash: s.dash ? 'dash' : 'solid' },
        hovertemplate: `${s.label}: %{y:.3f}<extra></extra>`,
      })),
    [x, series],
  )

  const layout = useMemo<Partial<Layout>>(
    () => ({
      height,
      margin: { l: 50, r: 12, t: 8, b: 28 },
      paper_bgcolor: '#171717',
      plot_bgcolor: '#171717',
      font: { color: '#999', size: 10 },
      showlegend: true,
      legend: { orientation: 'h', y: 1.16, x: 0, font: { size: 10 } },
      xaxis: { gridcolor: '#2a2a2a', zerolinecolor: '#444', color: '#888', showspikes: true, spikethickness: 1, spikecolor: '#555', spikemode: 'across' },
      yaxis: { gridcolor: '#2a2a2a', zerolinecolor: '#444', color: '#888', title: yTitle ? { text: yTitle } : undefined },
      hovermode: 'x unified',
      dragmode: 'zoom',
      uirevision,
      shapes:
        markerX != null && isFinite(markerX)
          ? [{ type: 'line', x0: markerX, x1: markerX, yref: 'paper', y0: 0, y1: 1, line: { color: '#eab308', width: 1, dash: 'dot' } }]
          : [],
    }),
    [height, uirevision, markerX, yTitle],
  )

  return (
    <div ref={containerRef}>
      <Plot
        data={data}
        layout={layout}
        config={{ scrollZoom: true, displaylogo: false, responsive: true, modeBarButtonsToRemove: ['select2d', 'lasso2d'] }}
        style={{ width: '100%' }}
        useResizeHandler
      />
    </div>
  )
}
