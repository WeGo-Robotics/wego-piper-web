import { useRef, useEffect } from 'react'
import { useT, fmt } from '../i18n'

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
  loader?: LoaderAdvice
}

// 백엔드 `training/loader.py` 의 `advise()` — 숫자와 판정 코드만 온다 (feature/train-loader-workers.md)
type LoaderAdvice = {
  reason: 'warming' | 'enough' | 'add_workers' | 'cpu_limit'
  num_workers: number | null
  samples: number
  update_s?: number
  data_s?: number
  wait_pct?: number
  max_gain?: number
  cap?: number | null
  amp_off?: boolean
  recommended?: number
  predicted_wait_pct?: number
  cpu_pct?: number | null
  cpu_busy?: boolean
}

type HistoryData = {
  steps: number[]
  losses: number[]
  grad_norms: number[]
  lrs: number[]
}

/** step 시간이 어디에 쓰이나 + workers 판정. 누가 병목인지 판정하지 않고, workers 로 얻을 수 있는
 *  최대 이득만 말한다 — GPU 쪽 처방(AMP·배치)은 이 숫자로 할 수 없다. */
function LoaderAdviceView({ a }: { a: LoaderAdvice }) {
  const t = useT()
  if (a.reason === 'warming' || a.update_s === undefined || a.data_s === undefined) {
    return <p className="text-[11px] text-neutral-500">{t('데이터 로더 측정 중…')}</p>
  }
  const step = a.update_s + a.data_s
  const gpuPct = step > 0 ? (a.update_s / step) * 100 : 100
  const wait = fmt.number(a.wait_pct ?? 0, { maximumFractionDigits: 1 })
  const raised = a.recommended !== undefined && a.num_workers !== null && a.recommended > a.num_workers
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-[11px] text-neutral-400">
        <span>{t('step 시간')} <span className="font-mono">{step.toFixed(3)}s</span></span>
        <span>{t('GPU 연산 {gpu}% · 데이터 대기 {wait}%', { gpu: fmt.number(gpuPct, { maximumFractionDigits: 0 }), wait })}</span>
      </div>
      <div className="flex h-1.5 w-full overflow-hidden rounded bg-neutral-700">
        <div className="h-full bg-blue-500" style={{ width: `${gpuPct}%` }} />
        <div className="h-full bg-amber-500" style={{ width: `${100 - gpuPct}%` }} />
      </div>
      {a.reason === 'enough' && (
        <p className="text-[11px] text-neutral-400">
          {t('workers {n}로 충분 — 늘려도 최대 {gain}배', { n: a.num_workers ?? 0, gain: fmt.number(a.max_gain ?? 1, { maximumFractionDigits: 2 }) })}
          {a.amp_off && <> · {t('AMP가 꺼져 있습니다 — bf16을 켜면 GPU 연산이 빨라질 수 있습니다')}</>}
        </p>
      )}
      {a.reason === 'add_workers' && (
        <p className="text-[11px] text-amber-400">
          {t('workers {n} → {rec} 권장 (예상 대기 {pred}%) · 다음 학습부터', {
            n: a.num_workers ?? 0, rec: a.recommended ?? 0,
            pred: fmt.number(a.predicted_wait_pct ?? 0, { maximumFractionDigits: 1 }),
          })}
        </p>
      )}
      {a.reason === 'cpu_limit' && (raised ? (
        <p className="text-[11px] text-amber-400">
          {t('CPU 코어가 모자라 workers로는 다 못 메웁니다 — 최대 {rec}로 올려도 대기 {pred}%', {
            rec: a.recommended ?? 0,
            pred: fmt.number(a.predicted_wait_pct ?? 0, { maximumFractionDigits: 1 }),
          })}
        </p>
      ) : (
        <p className="text-[11px] text-amber-400">
          {a.cpu_busy && a.cpu_pct != null
            ? t('CPU가 이미 {cpu}% — workers를 늘려도 빨라지지 않습니다', { cpu: fmt.number(a.cpu_pct, { maximumFractionDigits: 0 }) })
            : t('CPU 코어가 모자라 workers로는 더 빨라지지 않습니다')}
        </p>
      ))}
    </div>
  )
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
  const t = useT()
  if (!metrics) return null

  const pct = Math.round(metrics.progress * 100)

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
      <h3 className="text-sm font-semibold">{t('학습 메트릭')}</h3>

      {/* 진행률 바 */}
      <div className="space-y-1">
        <div className="flex justify-between text-xs text-neutral-400">
          <span>Step {fmt.number(metrics.step)} / {fmt.number(metrics.total_steps)}</span>
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
            <span className="text-neutral-400">{t('data_s (로딩)')}</span>
            <span className="font-mono">{metrics.data_s.toFixed(3)}</span>
          </div>
        )}
      </div>

      {metrics.loader && <LoaderAdviceView a={metrics.loader} />}

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

export type { MetricsData, HistoryData, LoaderAdvice }
