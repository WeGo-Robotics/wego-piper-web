import { useRef, useEffect } from 'react'

type MetricsData = {
  step: number
  total_steps: number
  progress: number
  loss: number
  grad_norm: number
  lr: number
  epoch: number
  update_s?: number  // GPU 갱신 시간 (병목 힌트)
  data_s?: number    // 데이터 로딩 시간 (병목 힌트)
}

type HistoryData = {
  steps: number[]
  losses: number[]
  grad_norms: number[]
  lrs: number[]
}

function LossChart({ history }: { history: HistoryData }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || history.steps.length < 2) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const w = canvas.width
    const h = canvas.height
    const pad = { t: 10, r: 10, b: 25, l: 50 }
    const plotW = w - pad.l - pad.r
    const plotH = h - pad.t - pad.b

    ctx.clearRect(0, 0, w, h)

    // 데이터 범위
    const steps = history.steps
    const losses = history.losses
    const xMin = steps[0], xMax = steps[steps.length - 1]
    const yMin = Math.min(...losses) * 0.95
    const yMax = Math.max(...losses) * 1.05

    // 배경
    ctx.fillStyle = '#171717'
    ctx.fillRect(0, 0, w, h)

    // 그리드
    ctx.strokeStyle = '#333'
    ctx.lineWidth = 0.5
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + (plotH / 4) * i
      ctx.beginPath()
      ctx.moveTo(pad.l, y)
      ctx.lineTo(w - pad.r, y)
      ctx.stroke()
    }

    // Loss 라인
    ctx.strokeStyle = '#ef4444'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    for (let i = 0; i < steps.length; i++) {
      const x = pad.l + ((steps[i] - xMin) / Math.max(xMax - xMin, 1)) * plotW
      const y = pad.t + (1 - (losses[i] - yMin) / Math.max(yMax - yMin, 1e-6)) * plotH
      if (i === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    }
    ctx.stroke()

    // 축 라벨
    ctx.fillStyle = '#888'
    ctx.font = '10px monospace'
    ctx.textAlign = 'right'
    ctx.fillText(yMax.toFixed(3), pad.l - 4, pad.t + 10)
    ctx.fillText(yMin.toFixed(3), pad.l - 4, h - pad.b)
    ctx.textAlign = 'center'
    ctx.fillText(String(xMin), pad.l, h - 5)
    ctx.fillText(String(xMax), w - pad.r, h - 5)
    ctx.fillText('step', w / 2, h - 5)
  }, [history])

  return <canvas ref={canvasRef} width={400} height={180} className="w-full rounded" />
}

export default function TrainingMetrics({ metrics, history }: { metrics: MetricsData | null; history: HistoryData }) {
  if (!metrics) return null

  const pct = Math.round(metrics.progress * 100)

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
      <h3 className="text-sm font-semibold">학습 메트릭</h3>

      {/* 진행률 바 */}
      <div className="space-y-1">
        <div className="flex justify-between text-xs text-neutral-400">
          <span>Step {metrics.step.toLocaleString()} / {metrics.total_steps.toLocaleString()}</span>
          <span>{pct}%</span>
        </div>
        <div className="w-full h-2 bg-neutral-700 rounded overflow-hidden">
          <div className="h-full bg-blue-500 rounded transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>

      {/* 수치 */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
        <div className="flex justify-between">
          <span className="text-neutral-400">Loss</span>
          <span className="font-mono text-red-400">{metrics.loss.toFixed(4)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-neutral-400">Grad Norm</span>
          <span className="font-mono">{metrics.grad_norm.toFixed(3)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-neutral-400">Learning Rate</span>
          <span className="font-mono">{metrics.lr.toExponential(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-neutral-400">Epoch</span>
          <span className="font-mono">{metrics.epoch.toFixed(2)}</span>
        </div>
        {typeof metrics.update_s === 'number' && (
          <div className="flex justify-between">
            <span className="text-neutral-400">updt_s (GPU)</span>
            <span className="font-mono">{metrics.update_s.toFixed(3)}</span>
          </div>
        )}
        {typeof metrics.data_s === 'number' && (
          <div className="flex justify-between">
            <span className="text-neutral-400">data_s (로딩)</span>
            <span className="font-mono">{metrics.data_s.toFixed(3)}</span>
          </div>
        )}
      </div>

      {/* 병목 힌트: 데이터 로딩(CPU) vs GPU 연산 */}
      {typeof metrics.update_s === 'number' && typeof metrics.data_s === 'number'
        && (metrics.update_s > 0 || metrics.data_s > 0) && (
        metrics.data_s > metrics.update_s ? (
          <p className="text-[11px] text-amber-400">데이터 로딩 병목 (CPU) → num_workers를 늘리세요</p>
        ) : (
          <p className="text-[11px] text-green-400">GPU 연산 병목 → AMP(bf16)·Batch가 효과적</p>
        )
      )}

      {/* Loss 그래프 */}
      {history.steps.length > 1 && (
        <div>
          <p className="text-xs text-neutral-400 mb-1">Loss Curve</p>
          <LossChart history={history} />
        </div>
      )}
    </div>
  )
}

export type { MetricsData, HistoryData }
