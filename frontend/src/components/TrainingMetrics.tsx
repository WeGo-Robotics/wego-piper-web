import { useRef, useEffect, useState } from 'react'
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
  timing?: Timing | null
  gpu_mem?: GpuMem | null
  oom?: OomInfo | null
}

// 백엔드 `training/gpu_mem.py` — 이 학습 프로세스의 GPU 메모리(MiB). 이 기계 학습만 온다.
type GpuMem = { used_mb: number; peak_mb: number; total_mb: number; other_mb: number }
// 메모리 부족으로 죽었을 때 — `max_batch` 는 같은 설정의 실측·기록으로 낸 다시 시작할 배치
type OomInfo = { batch: number | null; in_use_mb: number | null; capacity_mb: number | null; max_batch: number | null }

const gb = (mb: number) => fmt.number(mb / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1 })

// 백엔드 `TrainManager.timing()` — 길이는 초, 시각은 게이트웨이 시계. 돌고 있을 때만 온다.
type Timing = {
  started_at: string | null
  elapsed_s: number | null
  remaining_s: number | null
  total_s: number | null
  finish_at: string | null
  basis: 'progress' | 'step_time' | null
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

/** 경과·남은 시간·완료 예정. 길이는 서버가 잰 초에 **받은 뒤 흐른 시간**을 더해 매초 센다 —
 *  메트릭은 log_freq 마다(수 분) 오고, 브라우저 시계가 게이트웨이와 달라도 길이는 안 틀린다. */
function TimingView({ timing }: { timing: Timing }) {
  const t = useT()
  // 새 값이 오면 받은 시각을 다시 잡는다 (prop 이 바뀔 때 상태를 맞추는 React 의 권장 모양)
  const [got, setGot] = useState(() => ({ timing, at: Date.now() }))
  if (got.timing !== timing) setGot({ timing, at: Date.now() })
  const [, tick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 1000)
    return () => clearInterval(id)
  }, [])

  const since = (Date.now() - got.at) / 1000
  const elapsed = timing.elapsed_s === null ? null : timing.elapsed_s + since
  const remaining = timing.remaining_s === null ? null : Math.max(0, timing.remaining_s - since)
  const clock = (s: number) => {
    const n = Math.floor(s)
    const pad = (x: number) => String(x).padStart(2, '0')
    return `${Math.floor(n / 3600)}:${pad(Math.floor(n / 60) % 60)}:${pad(n % 60)}`
  }
  const span = (s: number) => {
    const m = Math.round(s / 60)
    return m >= 60 ? t('{h}시간 {m}분', { h: Math.floor(m / 60), m: m % 60 }) : t('{m}분', { m })
  }
  const finish = timing.finish_at ? new Date(timing.finish_at) : null
  const finishText = finish && (finish.toDateString() === new Date().toDateString()
    ? fmt.time(finish, { hour: '2-digit', minute: '2-digit' })
    : fmt.dateTime(finish, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }))

  return (
    <div className="flex flex-wrap justify-between gap-x-3 text-[11px] text-neutral-400">
      <span>
        {elapsed !== null && <>{t('경과 {elapsed}', { elapsed: clock(elapsed) })} · </>}
        {remaining === null ? t('남은 시간 계산 중…') : t('남은 시간 약 {remaining}', { remaining: span(remaining) })}
      </span>
      {finishText && (
        <span title={timing.basis === 'step_time' ? t('step 시간 기준 — 체크포인트 저장 시간이 빠져 조금 이를 수 있습니다') : undefined}>
          {t('완료 예정 {at}', { at: finishText })}
          {timing.total_s !== null && <> · {t('총 약 {total}', { total: span(timing.total_s) })}</>}
        </span>
      )}
    </div>
  )
}

/** 이 학습의 GPU 메모리. 막대는 GPU 전체 대비 — 다른 프로세스 몫도 학습이 못 쓰는 자리다. */
function GpuMemView({ m }: { m: GpuMem }) {
  const t = useT()
  const pct = (m.used_mb / m.total_mb) * 100
  const otherPct = (m.other_mb / m.total_mb) * 100
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-[11px] text-neutral-400">
        <span>{t('GPU 메모리')} <span className="font-mono">{gb(m.used_mb)} / {gb(m.total_mb)} GB</span></span>
        <span>{t('최대 {peak} GB · 다른 프로세스 {other} GB', { peak: gb(m.peak_mb), other: gb(m.other_mb) })}</span>
      </div>
      <div className="flex h-1.5 w-full overflow-hidden rounded bg-neutral-700">
        <div className={`h-full ${pct + otherPct > 90 ? 'bg-amber-500' : 'bg-emerald-500'}`} style={{ width: `${pct}%` }} />
        <div className="h-full bg-neutral-500" style={{ width: `${otherPct}%` }} />
      </div>
    </div>
  )
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

      {/* 메모리 부족은 첫 메트릭 줄 전에 죽는다 — 그때 화면에 남는 건 이 줄뿐이다 */}
      {metrics.oom && (
        <p className="rounded border border-red-500/30 bg-red-500/10 px-2 py-1.5 text-[11px] text-red-300">
          {metrics.oom.batch !== null && metrics.oom.in_use_mb !== null
            ? t('⚠ GPU 메모리 부족으로 멈췄습니다 — 배치 {batch}에서 {used} GB를 쓰고 있었습니다.',
              { batch: metrics.oom.batch, used: gb(metrics.oom.in_use_mb) })
            : t('⚠ GPU 메모리 부족으로 멈췄습니다.')}{' '}
          {metrics.oom.max_batch
            ? t('배치 {n} 이하로 다시 시작하세요 (학습 폼에도 같은 권장이 뜹니다).', { n: metrics.oom.max_batch })
            : t('배치를 줄여 다시 시작하세요.')}
        </p>
      )}

      {/* 진행률 바 */}
      <div className="space-y-1">
        <div className="flex justify-between text-xs text-neutral-400">
          <span>Step {fmt.number(metrics.step)} / {fmt.number(metrics.total_steps)}</span>
          <span>{pct}%</span>
        </div>
        <div className="w-full h-2 bg-neutral-700 rounded overflow-hidden">
          <div className="h-full bg-blue-500 rounded transition-all" style={{ width: `${pct}%` }} />
        </div>
        {metrics.timing && <TimingView timing={metrics.timing} />}
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
      {metrics.gpu_mem && <GpuMemView m={metrics.gpu_mem} />}

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

export type { MetricsData, HistoryData, LoaderAdvice, Timing, GpuMem, OomInfo }
