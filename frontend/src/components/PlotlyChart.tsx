import { useMemo } from 'react'
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

/** Plotly 기반 인터랙티브 라인 차트 — 박스 줌(드래그) / 휠 줌 / 팬 / 더블클릭 오토스케일 리셋. */
export default function PlotlyChart({ x, series, markerX = null, height = 220, uirevision = 'chart', yTitle }: Props) {
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
    <Plot
      data={data}
      layout={layout}
      config={{ scrollZoom: true, displaylogo: false, responsive: true, modeBarButtonsToRemove: ['select2d', 'lasso2d'] }}
      style={{ width: '100%' }}
      useResizeHandler
    />
  )
}
