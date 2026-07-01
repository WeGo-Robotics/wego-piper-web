// plotly.js-dist-min은 타입을 제공하지 않음 → 런타임 번들만 사용.
// 타입은 @types/plotly.js / @types/react-plotly.js에서 가져온다.
declare module 'plotly.js-dist-min'

declare module 'react-plotly.js/factory' {
  import { ComponentType } from 'react'
  import { PlotParams } from 'react-plotly.js'
  export default function createPlotlyComponent(plotly: unknown): ComponentType<PlotParams>
}
