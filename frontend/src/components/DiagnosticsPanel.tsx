import { useCallback, useEffect, useRef, useState } from 'react'
import DiagChart, { type Row } from './DiagCharts'
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

/** 흔드는 폭.
 *
 * ⚠ **작게 흔들면 부하가 안 걸린다.** ±10°(15.7°/s)에서는 토크가 0.17 N·m 밖에
 *   안 나와 멀쩡한 관절과 나쁜 관절이 구분되지 않았다. 속도는 팔의 한계(0.3 rad/s)
 *   때문에 못 올리므로 **폭을 키우고 주기를 늘린다** — 부하는 이동 범위에서 온다. */
const INTENSITY = [
  // ⚠ 속도를 같이 적는다. 폭만 보이면 "강하게" 가 느리게 크게 흔드는 것으로
  //   읽히는데, 관절 이상은 **빠를 때** 드러난다 — 무엇을 고르는지가 폭이 아니라
  //   부하라는 것이 보여야 한다. 70°/s 는 실제 수집 데이터의 상위 5% 자리다.
  { key: 'gentle', label: '약하게', hint: '±10° · 15°/s' },
  { key: 'normal', label: '보통', hint: '±20° · 40°/s' },
  { key: 'strong', label: '강하게', hint: '±30° · 70°/s · 수집 속도' },
] as const
type Intensity = typeof INTENSITY[number]['key']

type PlanJoint = { joint: string; center_deg: number; amplitude_deg: number
                   period_s: number; peak_speed_deg_s: number
                   phase_deg: number; note: string }
type Plan = { duration_s: number; intensity: string; joints: PlanJoint[] }
type Status = { running: boolean; elapsed_s: number; duration_s: number
                samples: number; error: string | null; plan?: Plan }
type Saved = {
  name: string; iface: string; rows: number; saved_at: number
  note?: string; adapter_serial?: string | null; firmware?: string | null
  plan?: Plan
}
type CmpCell = { a: number | null; b: number | null; delta: number | null; ratio: number | null }
type Compare = {
  a: Saved; b: Saved; keys: string[]; plan_differs: string[]
  joints: Record<string, Record<string, CmpCell>>
}

type Summary = {
  joints: Record<string, {
    samples: number; err_max_deg: number | null; err_rms_deg: number | null
    current_max_a: number | null; current_mean_a: number | null
    effort_max_nm: number | null; temp_rise_c: number | null; flags: string[]
  }>
  outliers: Record<string, string[]>
}

const METRIC_LABEL: Record<string, string> = {
  err_max_deg: '오차 최대', err_rms_deg: '오차 RMS', current_max_a: '전류 최대',
  current_mean_a: '전류 평균', effort_max_nm: '토크 최대', temp_rise_c: '온도 상승',
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
  const [intensity, setIntensity] = useState<Intensity>('normal')
  const [status, setStatus] = useState<Status | null>(null)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [rows, setRows] = useState<Row[]>([])
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState<Saved[]>([])
  const [saveName, setSaveName] = useState('')
  const [saveNote, setSaveNote] = useState('')
  const [pick, setPick] = useState<{ a: string; b: string }>({ a: '', b: '' })
  const [cmp, setCmp] = useState<Compare | null>(null)

  const loadSaved = useCallback(() => {
    api.get<{ saved: Saved[] }>('/robots/diag/saved')
      .then((r) => setSaved(r.saved)).catch(() => {})
  }, [])
  useEffect(() => { loadSaved() }, [loadSaved])

  const save = async () => {
    try {
      await api.post('/robots/diag/save', { name: saveName.trim(), note: saveNote })
      setSaveName(''); setSaveNote(''); loadSaved()
    } catch (e) {
      notify({ level: 'error', source: '검사',
               text: e instanceof Error ? e.message : '저장 실패' })
    }
  }

  const open = async (name: string) => {
    try {
      const d = await api.get<{ rows: Row[]; summary: Summary }>(
        `/robots/diag/saved/${encodeURIComponent(name)}`)
      setRows(d.rows); setSummary(d.summary); setCmp(null)
    } catch (e) {
      notify({ level: 'error', source: '검사',
               text: e instanceof Error ? e.message : '불러오기 실패' })
    }
  }

  const runCompare = async () => {
    try {
      setCmp(await api.get<Compare>(
        `/robots/diag/compare?a=${encodeURIComponent(pick.a)}&b=${encodeURIComponent(pick.b)}`))
    } catch (e) {
      notify({ level: 'error', source: '검사',
               text: e instanceof Error ? e.message : '비교 실패' })
    }
  }

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
      const d = await api.get<{ rows: Row[]; summary: Summary
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
    const amp = { gentle: 10, normal: 20, strong: 30 }[intensity]
    if (!await confirm(
      `${iface} 의 ${scope === 'all' ? '여섯 관절을 모두' : scope + ' 를'} 흔들어 잽니다.\n\n` +
      `· 지금 자세를 중심으로 사인파 **±${amp}°**\n` +
      '· 속도는 팔의 한계 안(약 14°/s)이라 폭이 클수록 오래 걸립니다\n' +
      '· 끝나면 원래 자세로 돌아옵니다\n' +
      (scope === 'all' ? '· 여섯이 위상을 어긋나게 움직입니다\n' : '') +
      '\n팔 주변이 비어 있는지 확인하세요.')) return
    setBusy(true); setSummary(null); setRows([])
    try {
      await api.post('/robots/diag/start', { iface, joints, intensity })
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

          <div className="flex items-center gap-1 rounded bg-neutral-900 p-0.5">
            {INTENSITY.map((it) => (
              <button key={it.key} onClick={() => setIntensity(it.key)}
                title={it.hint}
                className={`rounded px-2 py-1 text-xs ${intensity === it.key
                  ? 'bg-neutral-600 text-white' : 'text-neutral-400 hover:text-neutral-200'}`}>
                {it.label}
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
            계획 ({status.plan.duration_s}초): {status.plan.joints.map((p) =>
              `${p.joint} ±${p.amplitude_deg}° ${p.peak_speed_deg_s}°/s${p.note ? `(${p.note})` : ''}`
            ).join(' · ')}
          </p>
        )}
      </div>

      {/* ── 저장·조회·비교 ── */}
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <input value={saveName} onChange={(e) => setSaveName(e.target.value)}
            placeholder="결과 이름" className="w-40 rounded border border-neutral-700
            bg-neutral-900 px-2 py-1 text-sm" />
          {/* ⚠ 팔은 CAN 으로 **시리얼을 안 준다** — 어댑터 시리얼은 케이블을
              가리킬 뿐이다. 어느 팔인지는 사람이 여기 적어야 한다. */}
          <input value={saveNote} onChange={(e) => setSaveNote(e.target.value)}
            placeholder="팔 표시 (시리얼·위치 등 — 팔은 시리얼을 안 줍니다)"
            className="flex-1 min-w-[16rem] rounded border border-neutral-700
            bg-neutral-900 px-2 py-1 text-sm" />
          <button onClick={() => void save()} disabled={!saveName.trim() || !rows.length}
            title={rows.length ? '' : '저장할 결과가 없습니다 — 검사를 먼저 하세요'}
            className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300
                       hover:bg-blue-600 hover:text-white disabled:opacity-50">
            결과 저장
          </button>
        </div>

        {saved.length > 0 && (
          <div className="space-y-1">
            {saved.map((r) => (
              <div key={r.name} className="flex flex-wrap items-center gap-2 text-xs">
                <button onClick={() => void open(r.name)}
                  className="rounded bg-neutral-700 px-2 py-0.5 text-neutral-200 hover:bg-neutral-600">
                  {r.name}
                </button>
                <span className="text-neutral-500">
                  {r.iface} · {r.rows}행 · {new Date(r.saved_at * 1000).toLocaleString('ko-KR')}
                  {r.note ? ` · ${r.note}` : ''}
                </span>
                <span className="text-neutral-600" title="CAN 어댑터의 USB 시리얼 — 팔이 아니라 케이블을 가리킵니다">
                  어댑터 {r.adapter_serial?.slice(-6) ?? '—'}
                </span>
                <button onClick={() => setPick((p) => ({ ...p, a: r.name }))}
                  className={`rounded px-1.5 py-0.5 ${pick.a === r.name
                    ? 'bg-blue-600 text-white' : 'text-neutral-500 hover:text-neutral-300'}`}>A</button>
                <button onClick={() => setPick((p) => ({ ...p, b: r.name }))}
                  className={`rounded px-1.5 py-0.5 ${pick.b === r.name
                    ? 'bg-orange-600 text-white' : 'text-neutral-500 hover:text-neutral-300'}`}>B</button>
                <button onClick={() => void api.delete(`/robots/diag/saved/${encodeURIComponent(r.name)}`).then(loadSaved)}
                  className="text-neutral-600 hover:text-red-400">삭제</button>
              </div>
            ))}
            <button onClick={() => void runCompare()} disabled={!pick.a || !pick.b || pick.a === pick.b}
              className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300
                         hover:bg-green-600 hover:text-white disabled:opacity-50">
              A ↔ B 비교
            </button>
          </div>
        )}
      </div>

      {cmp && (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 space-y-2">
          <div className="text-xs text-neutral-400">
            <b className="text-blue-300">A {cmp.a.name}</b> → <b className="text-orange-300">B {cmp.b.name}</b>
          </div>
          {/* ⚠ 모션이 다르면 비교가 거짓이다 — 관절이 아니라 계획의 차이를 보는 것이다.
              막지는 않되 **다르다는 사실을 먼저** 말한다. */}
          {cmp.plan_differs.length > 0 && (
            <p className="rounded bg-amber-600/15 p-2 text-xs text-amber-300">
              ⚠ 두 회차의 모션이 다릅니다 — 아래 차이는 관절이 아니라 계획의 차이일 수
              있습니다: {cmp.plan_differs.join(' · ')}
            </p>
          )}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] text-xs tabular-nums">
              <thead className="text-neutral-500">
                <tr className="text-left">
                  <th className="py-1 pr-3 font-normal">관절</th>
                  {cmp.keys.map((k) => (
                    <th key={k} className="py-1 pr-3 font-normal">{METRIC_LABEL[k] ?? k}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="text-neutral-300">
                {Object.entries(cmp.joints).map(([j, row]) => (
                  <tr key={j} className="border-t border-neutral-700/60">
                    <td className="py-1 pr-3 text-neutral-100">{j}</td>
                    {cmp.keys.map((k) => {
                      const c = row[k]
                      const big = c.ratio != null && (c.ratio >= 1.5 || c.ratio <= 0.67)
                      return (
                        <td key={k} className={`py-1 pr-3 ${big ? 'text-amber-400' : ''}`}
                            title={`A ${c.a ?? '—'} → B ${c.b ?? '—'}`}>
                          {c.ratio != null ? `×${c.ratio}` : '—'}
                          <span className="ml-1 text-neutral-600">
                            {c.delta != null ? (c.delta >= 0 ? `+${c.delta}` : c.delta) : ''}
                          </span>
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-neutral-600">
            숫자는 <b>B ÷ A</b> 배율이고 작은 글씨가 차이입니다. 1.5배를 넘거나 0.67배
            아래일 때만 색이 붙습니다.
          </p>
        </div>
      )}

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
          {/* ⚠ **겹쳐 그린다.** 여섯이 같은 모션을 했다는 것이 이 검사의 전제이고,
              그러면 비교도 한 그림에서 되어야 한다 — 나란히 놓으면 눈이 축을
              오가야 하고, 그 차이가 바로 표만 봐서는 안 보이던 것이다. */}
          <div className="flex flex-wrap gap-4 overflow-x-auto pt-1">
            <DiagChart rows={rows} joints={Object.keys(summary.joints)}
                       field="ctrl_minus_feedback_deg" unit="°"
                       title="추종 오차 — 시킨 값과 실제의 차이" zeroLine />
            <DiagChart rows={rows} joints={Object.keys(summary.joints)}
                       field="motor_current_a" unit="A" title="모터 전류" zeroLine />
            <DiagChart rows={rows} joints={Object.keys(summary.joints)}
                       field="effort_nm" unit="N·m" title="토크" zeroLine />
            <DiagChart rows={rows} joints={Object.keys(summary.joints)}
                       field="feedback_deg" unit="°" title="실제 각도" />
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
