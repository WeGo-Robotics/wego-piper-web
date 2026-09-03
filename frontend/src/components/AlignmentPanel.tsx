import { useCallback, useEffect, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 정렬 검사 — 정해진 자세로 가서 AprilTag 로 얼마나 틀어졌는지 잰다
 * (feature/alignment-check.md).
 *
 * ⚠ **이 화면의 버튼은 팔을 실제로 움직인다.** 그래서 누르기 전에 무엇이
 *   일어나는지 적고, 확인을 받는다.
 */

type Check = {
  name: string; iface: string; camera_id: string
  tag_id: number; tag_mm: number; family: string
  baseline: { at: number; pose: Record<string, unknown> } | null
  last: { at: number; dx_mm: number; dy_mm: number; dz_mm: number
          dist_mm: number; rot_deg: number } | null
}

const when = (t?: number) => t ? new Date(t * 1000).toLocaleString('ko-KR') : '—'
const mm = (v: number) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(2)}`

export default function AlignmentPanel({ arms, cameras }: {
  arms: { iface: string; connected: boolean }[]
  cameras: { id: string; label: string; connected: boolean }[]
}) {
  const { notify, confirm: askConfirm } = useSystemMessage()
  const [checks, setChecks] = useState<Check[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [iface, setIface] = useState('')
  const [camId, setCamId] = useState('')
  const [tagId, setTagId] = useState(0)
  const [tagMm, setTagMm] = useState(40)

  const load = useCallback(() => {
    api.get<{ checks: Check[] }>('/alignment')
      .then((r) => setChecks(r.checks)).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  const liveArms = arms.filter((a) => a.connected)
  const liveCams = cameras.filter((c) => c.connected)

  const act = async (label: string, run: () => Promise<unknown>, key: string) => {
    setBusy(key)
    try { await run(); load() } catch (e) {
      notify({ level: 'error', source: '정렬',
               text: e instanceof Error ? e.message : `${label} 실패` })
    } finally { setBusy(null) }
  }

  const create = () => act('검사 만들기', async () => {
    await api.post('/alignment', { name: name.trim(), iface, camera_id: camId,
                                   tag_id: tagId, tag_mm: tagMm, family: '36h11' })
    setName('')
  }, 'new')

  const baseline = async (c: Check) => {
    if (!await askConfirm(
      `"${c.name}" 의 기준을 잡습니다.\n\n` +
      `· ${c.iface} 가 저장된 자세로 **실제로 이동합니다**\n` +
      '· 이 값이 이후 모든 판단의 0 점이 됩니다\n\n' +
      '⚠ 팔이 정상일 때 잡으세요. 틀어진 상태에서 잡으면 그 틀어짐이 "정상" 이 됩니다.\n' +
      '팔 주변이 비어 있는지 확인하세요.')) return
    act('기준 잡기', () => api.post(`/alignment/${c.name}/baseline`, {}), c.name)
  }

  const run = async (c: Check) => {
    if (!await askConfirm(`"${c.name}" 를 검사합니다.\n\n` +
      `· ${c.iface} 가 저장된 자세로 **실제로 이동합니다**\n\n` +
      '팔 주변이 비어 있는지 확인하세요.')) return
    act('검사', () => api.post(`/alignment/${c.name}/run`, {}), c.name)
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
        <h2 className="text-sm font-semibold">검사 만들기</h2>
        <p className="text-xs text-neutral-500">
          지금 팔이 있는 자세를 그대로 찍어 저장합니다 — 팔을 원하는 자세로 옮긴 뒤 만드세요.
          숫자를 손으로 적게 하면 오타 하나가 팔을 엉뚱한 곳으로 보냅니다.
        </p>
        <div className="flex flex-wrap gap-2">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="검사 이름"
            className="w-40 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm" />
          <select value={iface} onChange={(e) => setIface(e.target.value)}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm">
            <option value="">팔 고르기</option>
            {liveArms.map((a) => <option key={a.iface} value={a.iface}>{a.iface}</option>)}
          </select>
          <select value={camId} onChange={(e) => setCamId(e.target.value)}
            className="rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm">
            <option value="">카메라 고르기</option>
            {liveCams.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </select>
          <label className="flex items-center gap-1 text-xs text-neutral-400">
            태그 ID
            <input type="number" value={tagId} onChange={(e) => setTagId(Number(e.target.value))}
              className="w-16 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm" />
          </label>
          <label className="flex items-center gap-1 text-xs text-neutral-400">
            한 변(mm)
            <input type="number" value={tagMm} onChange={(e) => setTagMm(Number(e.target.value))}
              className="w-20 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-sm" />
          </label>
          <button onClick={create} disabled={!name.trim() || !iface || !camId || busy === 'new'}
            className="rounded bg-green-600 px-4 py-1 text-xs text-white hover:bg-green-500 disabled:opacity-50">
            {busy === 'new' ? '만드는 중…' : '현재 자세로 만들기'}
          </button>
        </div>
        {liveArms.length === 0 && (
          <p className="text-xs text-amber-400">연결된 팔이 없습니다 — 디바이스 탭에서 먼저 연결하세요.</p>
        )}
      </div>

      {checks.length === 0 ? (
        <p className="text-xs text-neutral-500">저장된 검사가 없습니다.</p>
      ) : checks.map((c) => (
        <div key={c.name} className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold">{c.name}</span>
            <span className="text-xs text-neutral-500">
              {c.iface} · {c.camera_id} · 태그 {c.tag_id} ({c.tag_mm}mm)
            </span>
            <div className="ml-auto flex gap-1.5">
              <button onClick={() => baseline(c)} disabled={busy === c.name}
                className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300 hover:bg-blue-600 hover:text-white disabled:opacity-50">
                {busy === c.name ? '진행 중…' : '기준 잡기'}
              </button>
              <button onClick={() => run(c)} disabled={busy === c.name || !c.baseline}
                title={c.baseline ? '' : '기준을 먼저 잡으세요'}
                className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300 hover:bg-green-600 hover:text-white disabled:opacity-50">
                검사
              </button>
              <button onClick={() => act('삭제', () => api.delete(`/alignment/${c.name}`), c.name)}
                className="rounded bg-neutral-700 px-2 py-1 text-xs text-neutral-400 hover:bg-red-600 hover:text-white">
                삭제
              </button>
            </div>
          </div>

          {!c.baseline ? (
            <p className="text-xs text-amber-400">
              기준이 없습니다 — <b>팔이 정상일 때</b> 기준을 먼저 잡으세요.
              기준이 없으면 "얼마나 틀어졌나" 를 잴 대상이 없습니다.
            </p>
          ) : (
            <p className="text-xs text-neutral-500">기준: {when(c.baseline.at)}</p>
          )}

          {c.last && (
            <div className="flex flex-wrap items-center gap-3 rounded bg-neutral-900 p-2 text-xs tabular-nums">
              <span className="text-neutral-500">{when(c.last.at)}</span>
              <span className={c.last.dist_mm >= 5 ? 'font-semibold text-amber-400' : 'text-neutral-200'}>
                {c.last.dist_mm.toFixed(2)} mm
              </span>
              <span className="text-neutral-400">
                ΔX {mm(c.last.dx_mm)} · ΔY {mm(c.last.dy_mm)} · ΔZ {mm(c.last.dz_mm)}
              </span>
              <span className="text-neutral-400">Δ회전 {c.last.rot_deg.toFixed(3)}°</span>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
