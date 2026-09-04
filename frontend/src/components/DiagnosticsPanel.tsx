import { useCallback, useEffect, useRef, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 관절 검사 — 정해진 모션으로 흔들며 전 항목을 로그하고 관절끼리 견준다
 * (feature/joint-diagnostics.md).
 *
 * ⚠ **이 화면의 버튼은 팔을 실제로 움직인다.** 누르기 전에 무엇이 얼마나
 *   움직이는지 숫자로 말하고 확인을 받는다.
 */

const JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6'] as const

type PlanJoint = { joint: string; center_deg: number; amplitude_deg: number
                   phase_deg: number; note: string }
type Plan = { duration_s: number; joints: PlanJoint[] }
type Status = { running: boolean; elapsed_s: number; duration_s: number
                samples: number; error: string | null; plan?: Plan }
type Summary = {
  joints: Record<string, {
    samples: number; err_max_deg: number | null; err_rms_deg: number | null
    current_max_a: number | null; current_mean_a: number | null
    effort_max_nm: number | null; temp_rise_c: number | null; flags: string[]
  }>
  outliers: Record<string, string[]>
}

const METRICS = [
  { key: 'err_max_deg', label: '추종 오차 최대', unit: '°' },
  { key: 'err_rms_deg', label: '추종 오차 RMS', unit: '°' },
  { key: 'current_max_a', label: '전류 최대', unit: 'A' },
  { key: 'current_mean_a', label: '전류 평균', unit: 'A' },
  { key: 'effort_max_nm', label: '토크 최대', unit: 'N·m' },
  { key: 'temp_rise_c', label: '온도 상승', unit: '℃' },
] as const

const FLAG_LABEL: Record<string, string> = {
  driver_overcurrent: '과전류', stall: '스톨', driver_error: '드라이버 오류',
  collision: '충돌 보호', angle_limit: '각도 한계', comm_error: '통신 오류',
}

const num = (v: number | null | undefined, d = 3) => v == null ? '—' : v.toFixed(d)

export default function DiagnosticsPanel({ arms }: {
  arms: { iface: string; connected: boolean
          master_slave?: string | null; responding?: boolean | null }[]
}) {
  const { notify, confirm } = useSystemMessage()
  const [iface, setIface] = useState('')
  const [scope, setScope] = useState<'all' | string>('all')
  const [status, setStatus] = useState<Status | null>(null)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [rows, setRows] = useState<Record<string, unknown>[]>([])
  const [busy, setBusy] = useState(false)

  // ⚠ 마스터는 외부 명령을 무시한다 — "안 움직였다" 가 고장으로 오독된다.
  //   조용한 팔도 마찬가지다: 안 움직이는 이유가 관절이 아니라 전원이다.
  const usable = arms.filter((a) => a.connected && a.master_slave !== 'master'
                                    && a.responding !== false)

  const poll = useCallback(async () => {
    try { setStatus(await api.get<Status>('/robots/diag/status')) } catch { /* 무시 */ }
  }, [])

  const timer = useRef<ReturnType<typeof setInterval>>(undefined)
  useEffect(() => {
    clearInterval(timer.current)
    if (status?.running) timer.current = setInterval(() => void poll(), 300)
    return () => clearInterval(timer.current)
  }, [status?.running, poll])

  // 끝나면 결과를 가져온다
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && status && !status.running) void fetchResult()
    wasRunning.current = !!status?.running
  }, [status?.running])   // eslint-disable-line react-hooks/exhaustive-deps

  const fetchResult = async () => {
    try {
      const d = await api.get<{ rows: Record<string, unknown>[]; summary: Summary
                                error: string | null }>('/robots/diag/result')
      setRows(d.rows); setSummary(d.summary)
      if (d.error) notify({ level: 'error', source: '검사', text: d.error })
    } catch (e) {
      notify({ level: 'error', source: '검사',
               text: e instanceof Error ? e.message : '결과를 읽지 못했습니다' })
    }
  }

  const start = async () => {
    const joints = scope === 'all' ? [...JOINTS] : [scope]
    if (!await confirm(
      `${iface} 의 ${scope === 'all' ? '여섯 관절을 모두' : scope + ' 를'} 흔들어 잽니다.\n\n` +
      '· 지금 자세를 중심으로 사인파, 최대 ±10° · 약 8초\n' +
      '· 끝나면 원래 자세로 돌아옵니다\n' +
      (scope === 'all' ? '· 여섯이 위상을 어긋나게 움직입니다\n' : '') +
      '\n팔 주변이 비어 있는지 확인하세요.')) return
    setBusy(true); setSummary(null); setRows([])
    try {
      await api.post('/robots/diag/start', { iface, joints })
      await poll()
    } catch (e) {
      notify({ level: 'error', source: '검사',
               text: e instanceof Error ? e.message : '시작하지 못했습니다' })
    } finally { setBusy(false) }
  }

  const stop = async () => {
    try { await api.post('/robots/diag/stop', {}); await poll() } catch { /* 무시 */ }
  }

  const csv = () => {
    if (!rows.length) return
    const cols = Object.keys(rows[0])
    const body = [cols.join(','),
                  ...rows.map((r) => cols.map((c) => String(r[c] ?? '')).join(','))]
    const url = URL.createObjectURL(new Blob([body.join('\n')], { type: 'text/csv' }))
    const a = document.createElement('a')
    a.href = url; a.download = `diag_${iface}_${Date.now()}.csv`; a.click()
    URL.revokeObjectURL(url)
  }

  const running = !!status?.running
  const pct = status && status.duration_s
    ? Math.min(100, (status.elapsed_s / status.duration_s) * 100) : 0

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <select value={iface} onChange={(e) => setIface(e.target.value)}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm">
            <option value="">팔 고르기</option>
            {usable.map((a) => <option key={a.iface} value={a.iface}>{a.iface}</option>)}
          </select>

          <div className="flex items-center gap-1 rounded bg-neutral-900 p-0.5">
            <button onClick={() => setScope('all')}
              className={`rounded px-2 py-1 text-xs ${scope === 'all'
                ? 'bg-neutral-600 text-white' : 'text-neutral-400 hover:text-neutral-200'}`}>
              전체
            </button>
            {JOINTS.map((j) => (
              <button key={j} onClick={() => setScope(j)}
                className={`rounded px-2 py-1 text-xs ${scope === j
                  ? 'bg-neutral-600 text-white' : 'text-neutral-400 hover:text-neutral-200'}`}>
                {j.replace('joint', 'J')}
              </button>
            ))}
          </div>

          {running ? (
            <button onClick={() => void stop()}
              className="rounded bg-red-600 px-4 py-1 text-xs text-white hover:bg-red-500">
              중지
            </button>
          ) : (
            <button onClick={() => void start()} disabled={!iface || busy}
              className="rounded bg-green-600 px-4 py-1 text-xs text-white
                         hover:bg-green-500 disabled:opacity-50">
              검사 시작
            </button>
          )}
          {rows.length > 0 && !running && (
            <button onClick={csv}
              className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300 hover:bg-neutral-600">
              CSV ({rows.length}행)
            </button>
          )}
        </div>

        {usable.length === 0 && (
          <p className="text-xs text-amber-400">
            검사할 수 있는 팔이 없습니다 — 연결된 <b>슬레이브</b> 팔이 필요합니다.
            마스터는 외부 명령을 무시해서 "안 움직였다" 가 고장으로 오독됩니다.
          </p>
        )}

        {running && (
          <div className="space-y-1">
            <div className="h-1 w-full overflow-hidden rounded-full bg-neutral-700">
              <div className="h-full bg-green-500 transition-all" style={{ width: `${pct}%` }} />
            </div>
            <p className="text-xs text-neutral-400 tabular-nums">
              {status?.elapsed_s.toFixed(1)}s / {status?.duration_s}s · {status?.samples}샘플
            </p>
          </div>
        )}

        {status?.plan && !running && (
          <p className="text-xs text-neutral-500">
            계획: {status.plan.joints.map((p) =>
              `${p.joint} ±${p.amplitude_deg}°${p.note ? `(${p.note})` : ''}`).join(' · ')}
          </p>
        )}
      </div>

      {summary && (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 space-y-2">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[46rem] table-fixed text-xs tabular-nums">
              <colgroup>
                <col style={{ width: '12%' }} />
                {METRICS.map((m) => <col key={m.key} style={{ width: '13%' }} />)}
                <col style={{ width: '10%' }} />
              </colgroup>
              <thead className="text-neutral-500">
                <tr className="text-left">
                  <th className="py-1 pr-3 font-normal">관절</th>
                  {METRICS.map((m) => (
                    <th key={m.key} className="py-1 pr-3 font-normal">{m.label}</th>
                  ))}
                  <th className="py-1 font-normal">플래그</th>
                </tr>
              </thead>
              <tbody className="text-neutral-300">
                {Object.entries(summary.joints).map(([j, d]) => (
                  <tr key={j} className="border-t border-neutral-700/60">
                    <td className="py-1 pr-3 text-neutral-100">{j}</td>
                    {METRICS.map((m) => {
                      const hit = summary.outliers[m.key]?.includes(j)
                      return (
                        <td key={m.key}
                            className={`py-1 pr-3 ${hit ? 'font-semibold text-amber-400' : ''}`}
                            title={hit ? '다른 관절의 중앙값보다 2배 넘게 큽니다' : ''}>
                          {num(d[m.key as keyof typeof d] as number | null)}{m.unit}
                        </td>
                      )
                    })}
                    <td className="py-1">
                      {d.flags.length
                        ? <span className="text-red-400">
                            {d.flags.map((f) => FLAG_LABEL[f] ?? f).join(' · ')}
                          </span>
                        : <span className="text-neutral-600">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-neutral-600">
            ⚠ 합격/불합격을 판정하지 않습니다 — 절대 기준이 없습니다. 여섯이 <b>같은
            모션</b>을 했으므로 관절끼리가 서로의 대조군이고, 중앙값의 2배를 넘는
            항목에만 색이 붙습니다. 원인은 사람이 봅니다.
          </p>
        </div>
      )}
    </div>
  )
}
