import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { api } from '../services/api'
import type { SidecarNotes, WeightCheckpoint, WeightRun } from '../types/models'
import { sizeText } from '../types/camera'
import { useSystemMessage } from './SystemMessages'
import { useT, fmt } from '../i18n'

/**
 * 가중치 표 — **학습 하나가 한 행**, 체크포인트는 행 안의 칩.
 *
 * ## 왜 표인가
 *
 * select 한 줄에는 이름·데이터셋·날짜·스텝을 나란히 비교할 자리가 없다. 학습 36개에
 * 체크포인트 207개(실측)를 줄 세우면 날짜만 읽힌다. 한 행에 한 학습을 두면 36줄이고,
 * 칩 하나 누르면 끝난다.
 *
 * 추론·학습(Fine-tune)은 창(`WeightTableModal`)으로 띄워 고르고(`onPick`), 저장소는 이 표가
 * 본문이다(`onOpen` — 누르면 상세 창이 뜬다). ▸ 는 어디서든 행 아래에 요약을 펼친다.
 */

type SortKey = 'name' | 'dataset' | 'policy' | 'created' | 'steps' | 'size'

type Props = {
  rows: WeightRun[]
  /** 고른 체크포인트 id — 그 칩을 강조한다 */
  value?: string
  /** 칩·행을 누르면 고른다 (추론·학습의 창) */
  onPick?: (ckpt: WeightCheckpoint, row: WeightRun) => void
  /** 칩·행을 누르면 연다 (저장소 — 상세 창). `ckptId` 는 누른 칩, 행이면 마지막 체크포인트 */
  onOpen?: (row: WeightRun, ckptId: string) => void
  /** 못 고르는 행이면 그 이유 — 흐리게 하고 이유를 보인다 */
  blockedReason?: (row: WeightRun) => string | null
  /** 이름을 고친 뒤 — 부모가 목록을 다시 읽는다 */
  onRenamed?: () => void
  /** 행마다 붙일 버튼 (저장소의 [Hub 업로드]). 누르면 그 버튼만 — 행을 열지 않는다 */
  actions?: (row: WeightRun) => ReactNode
}

/** 스텝은 `20K` 로 — 학습 쪽에서 다들 이렇게 읽는다 */
function kSteps(n: number | null): string {
  if (n == null) return '—'
  return n >= 1000 && n % 1000 === 0 ? `${n / 1000}K` : fmt.number(n)
}

function formatBytes(bytes: number): string {
  if (!bytes) return '—'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

const when = (iso: string | null) => iso
  ? fmt.dateTime(iso, { year: '2-digit', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
  : '—'

export default function WeightTable({ rows, value, onPick, onOpen, blockedReason, onRenamed, actions }: Props) {
  const t = useT()
  const { notify } = useSystemMessage()
  const [search, setSearch] = useState('')
  const [policy, setPolicy] = useState('')
  const [dataset, setDataset] = useState('')
  const [kind, setKind] = useState<'' | 'run' | 'model'>('')
  const [namedOnly, setNamedOnly] = useState(false)
  const [sortKey, setSortKey] = useState<SortKey>('created')
  const [sortAsc, setSortAsc] = useState(false)
  // ▸ 로 요약을 펼친 행
  const [open, setOpen] = useState<string | null>(null)
  const [editing, setEditing] = useState<{ key: string; draft: string } | null>(null)
  const [saving, setSaving] = useState(false)
  // 저장 직후 다시 읽기 전까지 고친 이름을 보인다. ⚠ 서버 값이 바뀌면(다시 읽었거나 상세에서
  //   또 고쳤으면) 버린다 — 계속 이기면 나중에 고친 이름이 표에 안 뜬다.
  const [renamed, setRenamed] = useState<Record<string, { name: string; was: string }>>({})

  const nameOf = (r: WeightRun) => {
    const o = renamed[r.key]
    return o && o.was === r.name ? o.name : r.name
  }
  const byRun = useMemo(() => new Map(rows.filter((r) => r.run).map((r) => [r.run as string, r])), [rows])
  const policies = useMemo(() => [...new Set(rows.filter((r) => r.is_policy).map((r) => r.policy_type))].sort(), [rows])
  const datasets = useMemo(() => [...new Set(rows.map((r) => r.dataset).filter(Boolean) as string[])].sort(), [rows])

  const baseText = (r: WeightRun) => describeBase(r, byRun, nameOf, t('처음부터'))

  const q = search.trim().toLowerCase()
  const shown = rows
    .filter((r) => !policy || r.policy_type === policy)
    .filter((r) => !dataset || r.dataset === dataset)
    .filter((r) => !kind || r.kind === kind)
    .filter((r) => !namedOnly || nameOf(r))
    // 이름·설명·데이터셋·경로·출발점 — 경로에는 날짜밖에 없다. 체크포인트에 따로 붙인 이름도
    //   본다: 행 안의 칩에만 있는 이름으로 찾으면 그 행이 걸려야 한다
    .filter((r) => !q || [nameOf(r), r.description, r.dataset, r.key, r.policy_type, baseText(r),
      ...r.checkpoints.map((c) => c.name)].some((v) => v?.toLowerCase().includes(q)))
    .sort((a, b) => {
      let cmp = 0
      if (sortKey === 'name') cmp = (nameOf(a) || a.key).localeCompare(nameOf(b) || b.key)
      else if (sortKey === 'dataset') cmp = (a.dataset ?? '').localeCompare(b.dataset ?? '')
      else if (sortKey === 'policy') cmp = a.policy_type.localeCompare(b.policy_type)
      else if (sortKey === 'created') cmp = (a.created ?? '').localeCompare(b.created ?? '')
      else if (sortKey === 'steps') cmp = (a.steps ?? 0) - (b.steps ?? 0)
      else if (sortKey === 'size') cmp = a.size_bytes - b.size_bytes
      return sortAsc ? cmp : -cmp
    })

  const sortBy = (k: SortKey) => {
    if (sortKey === k) setSortAsc(!sortAsc)
    else { setSortKey(k); setSortAsc(k === 'name' || k === 'dataset' || k === 'policy') }
  }

  const toggle = (r: WeightRun) => setOpen((o) => (o === r.key ? null : r.key))

  const saveName = async (r: WeightRun) => {
    if (!editing) return
    const name = editing.draft.trim()
    setSaving(true)
    try {
      // ⚠ 설명은 **그 사이드카에 있던 것 그대로** 보낸다 — 보이는 설명은 체크포인트에서
      //   빌려 온 것일 수 있다. 학습이면 run 루트(학습 폼 제목과 같은 파일), 아니면 그 모델.
      const notes = r.notes as Partial<SidecarNotes>
      const endpoint = r.kind === 'run' ? `/models/runs/${r.run}/notes` : `/models/${r.key}/notes`
      await api.put(endpoint, { name, description: notes.description ?? '' })
      setRenamed((m) => ({ ...m, [r.key]: { name, was: r.name } }))
      setEditing(null)
      onRenamed?.()
    } catch (e) {
      notify({ level: 'error', source: t('모델'), text: e instanceof Error ? e.message : t('저장 실패') })
    } finally { setSaving(false) }
  }

  const th = (k: SortKey, label: string, cls = '') => (
    <th className={`px-2 py-2 text-left font-medium cursor-pointer select-none whitespace-nowrap hover:text-neutral-200 ${cls}`}
      onClick={() => sortBy(k)}>
      {label}{sortKey === k ? (sortAsc ? ' ↑' : ' ↓') : ''}
    </th>
  )
  const control = 'rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-xs text-neutral-100'

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <input type="text" value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder={t('검색 — 이름·설명·데이터셋·경로')}
          className={`${control} min-w-[14rem] flex-1 py-1.5 text-sm placeholder:text-neutral-500`} />
        <select value={policy} onChange={(e) => setPolicy(e.target.value)} className={control}>
          <option value="">{t('정책 전체')}</option>
          {policies.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <select value={dataset} onChange={(e) => setDataset(e.target.value)} className={`${control} max-w-[16rem]`}>
          <option value="">{t('데이터셋 전체')}</option>
          {datasets.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} className={control}>
          <option value="">{t('학습 + 받은 모델')}</option>
          <option value="run">{t('학습한 것만')}</option>
          <option value="model">{t('받은 모델만')}</option>
        </select>
        <label className="flex items-center gap-1 text-xs text-neutral-400">
          <input type="checkbox" checked={namedOnly} onChange={(e) => setNamedOnly(e.target.checked)} />
          {t('이름 있는 것만')}
        </label>
        <span className="ml-auto text-xs text-neutral-500">{shown.length}/{rows.length}</span>
      </div>

      <div className="overflow-x-auto rounded border border-neutral-700">
        <table className="w-full text-xs">
          <thead className="sticky top-0 z-10 bg-neutral-900 text-neutral-400">
            <tr>
              <th className="w-6" />
              {th('name', t('이름'))}
              {th('dataset', t('데이터셋'))}
              {th('policy', t('정책'))}
              {th('created', t('학습일'))}
              {th('steps', t('스텝·배치'), 'text-right')}
              <th className="px-2 py-2 text-left font-medium whitespace-nowrap">{t('출발점')}</th>
              <th className="px-2 py-2 text-left font-medium whitespace-nowrap">{t('체크포인트')}</th>
              {actions && <th className="w-px" />}
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const blocked = blockedReason?.(r) ?? null
              const isOpen = open === r.key
              const name = nameOf(r)
              const picked = !!value && r.checkpoints.some((c) => c.ids.includes(value))
              return (
                <Fragment key={r.key}>
                  <tr
                    onClick={() => {
                      if (editing?.key === r.key) return
                      // 행 = 마지막 체크포인트. 고르는 창이면 고르고, 저장소면 상세 창을 연다
                      if (!r.checkpoints[0]) return
                      if (onPick) { if (!blocked) onPick(r.checkpoints[0], r) }
                      else if (onOpen) onOpen(r, r.checkpoints[0].id)
                      else toggle(r)
                    }}
                    className={`border-t border-neutral-800 align-top ${blocked ? 'opacity-50' : 'cursor-pointer hover:bg-neutral-800/60'}
                      ${picked ? 'bg-blue-500/10' : ''} ${isOpen ? 'bg-neutral-800/40' : ''}`}>
                    <td className="px-1 py-2 text-center">
                      <button type="button" onClick={(e) => { e.stopPropagation(); toggle(r) }}
                        title={t('자세히')} className="text-neutral-500 hover:text-neutral-200">
                        {isOpen ? '▾' : '▸'}
                      </button>
                    </td>
                    <td className="px-2 py-2 min-w-[12rem] max-w-[22rem]">
                      {editing?.key === r.key ? (
                        <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                          {/* ⚠ Esc 로 취소하지 않는다 — Esc 는 전역 E-stop 이다(EStopButton) */}
                          <input autoFocus value={editing.draft}
                            onChange={(e) => setEditing({ key: r.key, draft: e.target.value })}
                            onKeyDown={(e) => { if (e.key === 'Enter') void saveName(r) }}
                            placeholder={t('이름 (예: 볼트 조립 1차 — 주간 조명)')}
                            className="min-w-0 flex-1 rounded border border-blue-500 bg-neutral-950 px-1.5 py-0.5 text-xs text-neutral-100 focus:outline-none" />
                          <button type="button" disabled={saving} onClick={() => void saveName(r)}
                            className="rounded bg-blue-600 px-1.5 py-0.5 text-white hover:bg-blue-500 disabled:opacity-50">
                            {saving ? '…' : t('저장')}
                          </button>
                          <button type="button" onClick={() => setEditing(null)}
                            className="rounded bg-neutral-700 px-1.5 py-0.5 text-neutral-300 hover:bg-neutral-600">
                            {t('취소')}
                          </button>
                        </div>
                      ) : (
                        <div className="group flex items-start gap-1">
                          <div className="min-w-0">
                            <p className={`truncate ${name ? 'font-medium text-neutral-100' : 'text-neutral-500'}`}
                              title={r.description || undefined}>
                              {name || (r.kind === 'run' ? t('이름 없음') : r.key)}
                            </p>
                            {/* 이름이 있으면 경로를 밑에 — 파일로 찾을 때 쓴다 */}
                            {(name || r.kind === 'run') && <p className="truncate text-[10px] text-neutral-500">{r.key}</p>}
                            {blocked && <p className="text-[10px] text-amber-400">{blocked}</p>}
                          </div>
                          <button type="button" title={t('이름 고치기')}
                            onClick={(e) => { e.stopPropagation(); setEditing({ key: r.key, draft: name }) }}
                            className="shrink-0 text-neutral-500 opacity-0 hover:text-neutral-200 group-hover:opacity-100 focus:opacity-100">
                            ✎
                          </button>
                        </div>
                      )}
                    </td>
                    <td className="px-2 py-2 max-w-[16rem] truncate text-neutral-300" title={r.dataset ?? undefined}>
                      {r.dataset ?? <span className="text-neutral-600">—</span>}
                    </td>
                    <td className="px-2 py-2 whitespace-nowrap">
                      {r.is_policy ? r.policy_type : <span className="text-neutral-500">{t('정책 아님')}</span>}
                      {r.action_space === 'delta_v1' && <span className="ml-1 text-emerald-300" title={t('델타 체크포인트 — 추론이 절대 위치로 되돌린다')}>Δ</span>}
                      {r.action_space === 'unknown' && <span className="ml-1 text-red-400" title={t('델타로 학습한 것으로 보이는데 학습 데이터셋이 없어 확인할 수 없습니다 — 시작하지 않습니다')}>Δ?</span>}
                    </td>
                    <td className="px-2 py-2 whitespace-nowrap text-neutral-300">{when(r.created)}</td>
                    <td className="px-2 py-2 whitespace-nowrap text-right font-mono text-neutral-300">
                      {r.steps != null ? <>{kSteps(r.steps)}<span className="text-neutral-500">·{r.batch_size ?? '?'}</span></> : '—'}
                    </td>
                    <td className="px-2 py-2 max-w-[14rem] truncate text-neutral-400" title={r.base?.label}>{baseText(r)}</td>
                    <td className="px-2 py-2 min-w-[15rem]">
                      <div className="flex flex-wrap gap-1">
                        {r.checkpoints.map((c) => (
                          <CheckpointChip key={c.id} c={c} row={r} on={!!value && c.ids.includes(value)}
                            disabled={!!blocked && !!onPick}
                            onClick={() => {
                              if (onPick) onPick(c, r)
                              else if (onOpen) onOpen(r, c.id)
                              else toggle(r)
                            }} />
                        ))}
                      </div>
                    </td>
                    {actions && (
                      <td className="px-2 py-1.5 whitespace-nowrap" onClick={(e) => e.stopPropagation()}>{actions(r)}</td>
                    )}
                  </tr>
                  {isOpen && (
                    <tr className="bg-neutral-900/60">
                      <td />
                      <td colSpan={actions ? 8 : 7} className="px-2 pb-3 pt-1">
                        <RowDetail row={r} baseText={baseText(r)} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
            {!shown.length && (
              <tr><td colSpan={actions ? 9 : 8} className="px-2 py-6 text-center text-neutral-500">
                {rows.length ? t('조건에 맞는 가중치가 없습니다') : t('로컬 모델이 없습니다. Hub에서 다운로드하거나 학습을 실행하세요.')}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** 체크포인트 칩 한 칸 — 표의 행과 저장소 상세 창이 같이 쓴다. `last` 는 가리키는 스텝과 한 칸이다. */
export function CheckpointChip({ c, row, on, disabled, onClick }: {
  c: WeightCheckpoint
  row: WeightRun
  on: boolean
  disabled?: boolean
  onClick: () => void
}) {
  const t = useT()
  // 체크포인트에 따로 붙인 이름 — 학습 이름과 같으면 되풀이하지 않는다
  const own = c.name && c.name !== row.name ? c.name : ''
  return (
    <button type="button" disabled={disabled}
      onClick={(e) => { e.stopPropagation(); onClick() }}
      title={[c.final ? t('마지막 체크포인트') : '', own].filter(Boolean).join(' · ') || undefined}
      className={`rounded px-1.5 py-0.5 font-mono whitespace-nowrap disabled:cursor-not-allowed
        ${on ? 'bg-blue-600 text-white'
          : c.final ? 'bg-neutral-700 text-neutral-100 hover:bg-neutral-600'
          : 'bg-neutral-800 text-neutral-400 hover:bg-neutral-700'}`}>
      {c.step != null ? kSteps(c.step) : (c.checkpoint === 'last' ? 'last' : t('모델'))}
      {c.final && row.kind === 'run' && <span className="ml-1 font-sans">{t('최종')}</span>}
      {own && <span className="ml-1 font-sans text-amber-300">•</span>}
    </button>
  )
}

/** 이어 학습의 출발점을 사람 말로 — 로컬 학습이면 그 학습의 이름(없으면 경로)과 스텝 */
function describeBase(r: WeightRun, byRun: Map<string, WeightRun>, nameOf: (r: WeightRun) => string,
                      fromScratch: string): string {
  if (!r.base) return r.kind === 'run' ? fromScratch : '—'
  if (r.base.run) {
    const from = byRun.get(r.base.run)
    const label = (from && nameOf(from)) || r.base.run
    const step = r.base.checkpoint && /^\d+$/.test(r.base.checkpoint) ? ` · ${kSteps(Number(r.base.checkpoint))}` : ''
    return `${label}${step}`
  }
  return r.base.label
}

/** 학습 한 줄의 요약 — 저장소의 상세 창이 맨 위에 쓴다 (▸ 로 펼친 것과 같은 내용). */
export function RunSummary({ row, rows }: { row: WeightRun; rows: WeightRun[] }) {
  const t = useT()
  const byRun = new Map(rows.filter((r) => r.run).map((r) => [r.run as string, r]))
  return <RowDetail row={row} baseText={describeBase(row, byRun, (r) => r.name, t('처음부터'))} />
}

/** 펼친 행 — 표에 못 넣은 것들. 없는 값은 줄째 안 그린다. */
function RowDetail({ row: r, baseText }: { row: WeightRun; baseText: string }) {
  const t = useT()
  const cams = Object.entries(r.train_cameras?.cameras ?? {})
  const items: [string, ReactNode][] = [
    [t('출발점'), baseText],
    [t('최종 loss'), r.final_loss != null ? r.final_loss.toFixed(4) : null],
    ['lr', r.lr != null ? String(r.lr) : null],
    [t('학습 카메라'), cams.length ? cams.map(([n, c]) => `${n} ${sizeText(c.width, c.height, c.fps)}`).join(' · ') : null],
    [t('크기'), formatBytes(r.size_bytes)],
    [t('경로'), <span className="font-mono break-all">{r.path}</span>],
  ]
  return (
    <div className="space-y-1.5">
      {r.description && <p className="whitespace-pre-wrap text-neutral-300">{r.description}</p>}
      <div className="grid grid-cols-1 gap-x-6 gap-y-0.5 sm:grid-cols-2">
        {items.filter(([, v]) => v != null && v !== '').map(([k, v]) => (
          <div key={k} className="flex gap-2">
            <span className="w-20 shrink-0 text-neutral-500">{k}</span>
            <span className="min-w-0 text-neutral-200">{v}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
