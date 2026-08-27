import { useEffect, useMemo, useRef } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import type { Data, Layout } from 'plotly.js'

// dist-min 번들로 Plot 컴포넌트 생성 (소스에서 plotly 빌드 회피)
const Plot = createPlotlyComponent(Plotly)

/** 이 점수를 넘을 때만 WebGL 을 쓴다 — 그 아래는 SVG 가 더 안전하다 (위 주석). */
const GL_MIN_POINTS = 4000

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

  // ⚠ **WebGL 컨텍스트는 브라우저당 몇 개뿐이다** (크롬 기준 십수 개).
  //
  //   `scattergl` 은 그래프마다 하나씩 잡는다. 한도를 넘으면 브라우저가 **오래된
  //   것부터 버리고**, 버려진 그래프는 하얗게 남는다 — 에러도 안 난다. 에피소드
  //   화면에서 관절 그래프(7축)를 펼치자 위쪽 속도 그래프들이 그렇게 비었다.
  //
  //   점이 많을 때만 GL 이 값을 한다. 이 화면의 신호는 에피소드당 수백 점이라
  //   SVG 로 충분하고, 그러면 컨텍스트를 아예 안 쓴다.
  const kind = useMemo(
    () => (series.some((s) => s.data.length > GL_MIN_POINTS) ? 'scattergl' : 'scatter'),
    [series],
  )

  const data = useMemo<Data[]>(
    () =>
      series.map((s) => ({
        type: kind,
        mode: 'lines',
        name: s.label,
        x,
        y: s.data,
        connectgaps: false,
        line: { color: s.color, width: 1.6, dash: s.dash ? 'dash' : 'solid' },
        hovertemplate: `${s.label}: %{y:.3f}<extra></extra>`,
      })),
    [x, series, kind],
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
