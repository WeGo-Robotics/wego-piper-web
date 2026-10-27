import { useState } from 'react'
import { api } from '../services/api'
import type { Dataset } from '../types/models'
import { useSystemMessage } from './SystemMessages'
import { useT, fmt } from '../i18n'

/**
 * 데이터셋 목록 — **표**와 **썸네일** 두 보기. 에피소드 페이지의 [선택하기] 창
 * (`DatasetPickerModal`)과 저장소의 데이터셋 화면이 같이 쓴다.
 *
 * select 한 줄에는 `org/이름 (에피소드 수)` 밖에 안 들어가, 사람이 붙인 이름도 길이도
 * 카메라도 안 보였다(실측: 데이터셋 51개). 표는 나란히 비교할 때, 썸네일은 "그 사과
 * 데이터" 를 그림으로 찾을 때 쓴다. 가중치 표(`WeightTable`)와 같은 조작이다 — 누르면
 * 열고(`onOpen` — 고르기든 상세든 부르는 쪽이 정한다), ✎ 로 이름을 고치고, 표는 열 머리로 정렬한다.
 */

type SortKey = 'name' | 'episodes' | 'length' | 'created' | 'size'
type Kind = '' | 'original' | 'baked' | 'delta' | 'sim' | 'hub'
type View = 'table' | 'thumbs'

type Props = {
  datasets: Dataset[]
  /** 지금 고른(열린) 것 — 강조한다 */
  value?: string
  /** 행·카드를 누르면 */
  onOpen: (d: Dataset) => void
  /** 이름을 고친 뒤 — 부모가 목록을 다시 읽는다 */
  onRenamed?: () => void
}

const VIEW_KEY = 'dataset-picker-view'

function formatBytes(bytes: number): string {
  if (!bytes) return '—'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

/** 녹화 길이 — 프레임 ÷ fps */
function seconds(d: Dataset): number | null {
  return d.fps ? d.total_frames / d.fps : null
}

function duration(sec: number | null): string {
  if (sec == null) return '—'
  // 먼저 반올림한다 — 59.7초가 `0:60` 이 되지 않게
  const total = Math.round(sec)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

const cameras = (d: Dataset) =>
  Object.keys(d.features ?? {}).filter((k) => k.startsWith('observation.images.')).map((k) => k.slice(19))

const stateDim = (d: Dataset) =>
  ((d.features?.['observation.state'] as { shape?: number[] } | undefined)?.shape ?? [])[0] ?? null

export default function DatasetBrowser({ datasets, value, onOpen, onRenamed }: Props) {
  const t = useT()
  const { notify } = useSystemMessage()
  const [view, setView] = useState<View>(() => (localStorage.getItem(VIEW_KEY) === 'thumbs' ? 'thumbs' : 'table'))
  const [search, setSearch] = useState('')
  const [kind, setKind] = useState<Kind>('')
  const [namedOnly, setNamedOnly] = useState(false)
  const [sortKey, setSortKey] = useState<SortKey>('created')
  const [sortAsc, setSortAsc] = useState(false)
  const [editing, setEditing] = useState<{ id: string; draft: string } | null>(null)
  const [saving, setSaving] = useState(false)
  // 저장 직후 다시 읽기 전까지 고친 이름 — 서버 값이 바뀌면 버린다 (WeightTable 과 같은 규칙)
  const [renamed, setRenamed] = useState<Record<string, { name: string; was: string }>>({})

  const switchView = (v: View) => {
    setView(v)
    localStorage.setItem(VIEW_KEY, v)
  }

  const nameOf = (d: Dataset) => {
    const server = d.notes?.name ?? ''
    const o = renamed[d.id]
    return o && o.was === server ? o.name : server
  }
  const kindOf = (d: Dataset): Kind[] => [
    ...(d.baked ? ['baked' as const] : []),
    ...(d.delta ? ['delta' as const] : []),
    ...(d.scene ? ['sim' as const] : []),
    ...(d.source === 'hub' ? ['hub' as const] : []),
  ]

  const q = search.trim().toLowerCase()
  const shown = datasets
    .filter((d) => !kind || (kind === 'original' ? !d.baked && !d.delta : kindOf(d).includes(kind)))
    .filter((d) => !namedOnly || nameOf(d))
    .filter((d) => !q || [nameOf(d), d.notes?.description, d.id, d.scene?.name, ...cameras(d)]
      .some((v) => v?.toLowerCase().includes(q)))
    .sort((a, b) => {
      let cmp = 0
      if (sortKey === 'name') cmp = (nameOf(a) || a.id).localeCompare(nameOf(b) || b.id)
      else if (sortKey === 'episodes') cmp = a.total_episodes - b.total_episodes
      else if (sortKey === 'length') cmp = (seconds(a) ?? 0) - (seconds(b) ?? 0)
      else if (sortKey === 'created') cmp = (a.created ?? a.modified).localeCompare(b.created ?? b.modified)
      else if (sortKey === 'size') cmp = a.size_bytes - b.size_bytes
      return sortAsc ? cmp : -cmp
    })

  const sortBy = (k: SortKey) => {
    if (sortKey === k) setSortAsc(!sortAsc)
    else { setSortKey(k); setSortAsc(k === 'name') }
  }

  const pick = (d: Dataset) => {
    if (editing?.id === d.id) return
    onOpen(d)
  }

  const saveName = async (d: Dataset) => {
    if (!editing) return
    const name = editing.draft.trim()
    setSaving(true)
    try {
      // 설명은 사이드카에 있던 것 그대로 — 이름만 고친다
      await api.put(`/datasets/${d.id}/notes`, { name, description: d.notes?.description ?? '' })
      setRenamed((m) => ({ ...m, [d.id]: { name, was: d.notes?.name ?? '' } }))
      setEditing(null)
      onRenamed?.()
    } catch (e) {
      notify({ level: 'error', source: t('데이터셋'), text: e instanceof Error ? e.message : t('저장 실패') })
    } finally { setSaving(false) }
  }

  /** 이름 칸 — 보기 둘이 같이 쓴다. 고치는 중이면 입력칸, 아니면 이름 + ✎ */
  const nameCell = (d: Dataset) => {
    const name = nameOf(d)
    if (editing?.id === d.id) {
      return (
        <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
          {/* ⚠ Esc 로 취소하지 않는다 — Esc 는 전역 E-stop 이다 */}
          <input autoFocus value={editing.draft}
            onChange={(e) => setEditing({ id: d.id, draft: e.target.value })}
            onKeyDown={(e) => { if (e.key === 'Enter') void saveName(d) }}
            placeholder={t('이름 (예: 볼트 조립 1차 — 주간 조명)')}
            className="min-w-0 flex-1 rounded border border-blue-500 bg-neutral-950 px-1.5 py-0.5 text-xs text-neutral-100 focus:outline-none" />
          <button type="button" disabled={saving} onClick={() => void saveName(d)}
            className="rounded bg-blue-600 px-1.5 py-0.5 text-white hover:bg-blue-500 disabled:opacity-50">
            {saving ? '…' : t('저장')}
          </button>
          <button type="button" onClick={() => setEditing(null)}
            className="rounded bg-neutral-700 px-1.5 py-0.5 text-neutral-300 hover:bg-neutral-600">
            {t('취소')}
          </button>
        </div>
      )
    }
    return (
      <div className="group flex items-start gap-1">
        <div className="min-w-0">
          <p className={`truncate ${name ? 'font-medium text-neutral-100' : 'text-neutral-300'}`}
            title={d.notes?.description || undefined}>
            {name || d.id}
          </p>
          {/* 이름이 있으면 ID 를 밑에 — 허브·폴더에서는 이걸로 찾는다 */}
          {name && <p className="truncate text-[10px] text-neutral-500">{d.id}</p>}
        </div>
        <button type="button" title={t('이름 고치기')}
          onClick={(e) => { e.stopPropagation(); setEditing({ id: d.id, draft: name }) }}
          className="shrink-0 text-neutral-500 opacity-0 hover:text-neutral-200 group-hover:opacity-100 focus:opacity-100">
          ✎
        </button>
      </div>
    )
  }

  const badges = (d: Dataset) => (
    <>
      {d.baked && <span className="mr-1 rounded bg-violet-500/15 px-1 text-violet-300"
        title={d.baked.source ? t('{source} 에서 구운 사본', { source: d.baked.source }) : undefined}>{t('구운 사본')}</span>}
      {d.delta && <span className="mr-1 rounded bg-emerald-500/15 px-1 text-emerald-300"
        title={d.delta.source ? t('{source} 에서 만든 델타 사본', { source: d.delta.source }) : undefined}>Δ {t('델타')}</span>}
      {d.scene && <span className="mr-1 rounded bg-sky-500/15 px-1 text-sky-300">{t('시뮬')} · {d.scene.name}</span>}
      {d.source === 'hub' && <span className="mr-1 rounded bg-neutral-700 px-1 text-neutral-300">{t('허브')}</span>}
    </>
  )

  const th = (k: SortKey, label: string, cls = '') => (
    <th className={`px-2 py-2 text-left font-medium cursor-pointer select-none whitespace-nowrap hover:text-neutral-200 ${cls}`}
      onClick={() => sortBy(k)}>
      {label}{sortKey === k ? (sortAsc ? ' ↑' : ' ↓') : ''}
    </th>
  )
  const control = 'rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-xs text-neutral-100'
  const empty = datasets.length ? t('조건에 맞는 데이터셋이 없습니다') : t('데이터셋이 없습니다')

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {/* 보기 — 도구줄 맨 앞 */}
        <div className="flex gap-0.5 rounded bg-neutral-800 p-0.5">
          {([['table', t('표')], ['thumbs', t('썸네일')]] as const).map(([v, label]) => (
            <button key={v} type="button" onClick={() => switchView(v)}
              className={`rounded px-2 py-0.5 text-xs ${view === v ? 'bg-neutral-600 text-white' : 'text-neutral-400 hover:text-white'}`}>
              {label}
            </button>
          ))}
        </div>
        <input type="text" value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder={t('검색 — 이름·설명·ID·카메라·가상환경')}
          className={`${control} min-w-[14rem] flex-1 py-1.5 text-sm placeholder:text-neutral-500`} />
        <select value={kind} onChange={(e) => setKind(e.target.value as Kind)} className={control}>
          <option value="">{t('종류 전체')}</option>
          <option value="original">{t('원본만')}</option>
          <option value="baked">{t('구운 사본')}</option>
          <option value="delta">{t('델타 사본')}</option>
          <option value="sim">{t('시뮬')}</option>
          <option value="hub">{t('허브에서 받은 것')}</option>
        </select>
        {/* 썸네일 보기에는 열 머리가 없다 — 정렬을 여기서 고른다 */}
        {view === 'thumbs' && (
          <select value={`${sortKey}:${sortAsc ? 'asc' : 'desc'}`} className={control}
            onChange={(e) => { const [k, dir] = e.target.value.split(':'); setSortKey(k as SortKey); setSortAsc(dir === 'asc') }}>
            <option value="created:desc">{t('최근 것부터')}</option>
            <option value="created:asc">{t('오래된 것부터')}</option>
            <option value="name:asc">{t('이름순')}</option>
            <option value="episodes:desc">{t('에피소드 많은 순')}</option>
            <option value="length:desc">{t('긴 것부터')}</option>
          </select>
        )}
        <label className="flex items-center gap-1 text-xs text-neutral-400">
          <input type="checkbox" checked={namedOnly} onChange={(e) => setNamedOnly(e.target.checked)} />
          {t('이름 있는 것만')}
        </label>
        <span className="ml-auto text-xs text-neutral-500">{shown.length}/{datasets.length}</span>
      </div>

      {view === 'thumbs' ? (
        shown.length ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {shown.map((d) => (
              <div key={d.id} onClick={() => pick(d)}
                className={`cursor-pointer overflow-hidden rounded-lg border bg-neutral-800/60 text-xs transition-colors
                  ${d.id === value ? 'border-blue-500 ring-1 ring-blue-500' : 'border-neutral-700 hover:border-neutral-500'}`}>
                <Thumb d={d} />
                <div className="space-y-1 p-2">
                  {nameCell(d)}
                  <p className="text-neutral-400">
                    {t('{n}개 에피소드', { n: d.total_episodes })} · {duration(seconds(d))}
                    <span className="text-neutral-500"> · {cameras(d).join(' · ')}</span>
                  </p>
                  <p className="flex flex-wrap items-center gap-y-1 text-[10px] text-neutral-500">
                    {badges(d)}
                    {fmt.date(d.created ?? d.modified)}
                  </p>
                </div>
              </div>
            ))}
          </div>
        ) : <p className="py-6 text-center text-xs text-neutral-500">{empty}</p>
      ) : (
        <div className="overflow-x-auto rounded border border-neutral-700">
          <table className="w-full text-xs">
            <thead className="bg-neutral-900 text-neutral-400">
              <tr>
                {th('name', t('이름'))}
                {th('episodes', t('에피소드'), 'text-right')}
                {th('length', t('길이'), 'text-right')}
                <th className="px-2 py-2 text-left font-medium whitespace-nowrap">{t('카메라')}</th>
                <th className="px-2 py-2 text-right font-medium whitespace-nowrap"
                  title={t('관측 상태 차원 — 한 팔이면 7(관절 6 + 그리퍼), 두 팔이면 14')}>{t('상태')}</th>
                <th className="px-2 py-2 text-left font-medium whitespace-nowrap">{t('종류')}</th>
                {th('created', t('만든 날짜'))}
                {th('size', t('크기'), 'text-right')}
              </tr>
            </thead>
            <tbody>
              {shown.map((d) => {
                const sec = seconds(d)
                return (
                  <tr key={d.id} onClick={() => pick(d)}
                    className={`border-t border-neutral-800 align-top cursor-pointer hover:bg-neutral-800/60
                      ${d.id === value ? 'bg-blue-500/10' : ''}`}>
                    <td className="px-2 py-2 min-w-[14rem] max-w-[24rem]">{nameCell(d)}</td>
                    <td className="px-2 py-2 text-right font-mono text-neutral-200">{d.total_episodes}</td>
                    <td className="px-2 py-2 text-right font-mono text-neutral-300 whitespace-nowrap"
                      title={d.total_episodes && sec != null
                        ? t('에피소드당 평균 {s}초', { s: Math.round(sec / d.total_episodes) }) : undefined}>
                      {duration(sec)}
                    </td>
                    <td className="px-2 py-2 text-neutral-300">{cameras(d).join(' · ') || '—'}</td>
                    <td className="px-2 py-2 text-right font-mono text-neutral-400">{stateDim(d) ?? '—'}</td>
                    <td className="px-2 py-2 whitespace-nowrap">
                      {badges(d)}
                      {!d.baked && !d.delta && !d.scene && d.source !== 'hub' && <span className="text-neutral-600">{t('원본')}</span>}
                    </td>
                    <td className="px-2 py-2 whitespace-nowrap text-neutral-300">
                      {fmt.dateTime(d.created ?? d.modified, { year: '2-digit', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })}
                    </td>
                    <td className="px-2 py-2 text-right whitespace-nowrap text-neutral-400">{formatBytes(d.size_bytes)}</td>
                  </tr>
                )
              })}
              {!shown.length && (
                <tr><td colSpan={8} className="px-2 py-6 text-center text-neutral-500">{empty}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/** 카드 그림 — 게이트웨이가 만든 작은 JPEG(`/datasets/{id}/thumbnail`). 영상이 없으면 빈 칸. */
function Thumb({ d }: { d: Dataset }) {
  const t = useT()
  const [failed, setFailed] = useState(false)
  return (
    <div className="flex aspect-[4/3] items-center justify-center bg-neutral-950">
      {failed
        ? <span className="text-[10px] text-neutral-600">{t('미리보기 없음')}</span>
        // ⚠ lazy — 화면에 보이는 카드만 받는다. 수정 시각을 붙여 다시 녹화하면 새 그림을 받는다
        : <img src={`/api/datasets/${d.id}/thumbnail?v=${encodeURIComponent(d.modified)}`} alt=""
            loading="lazy" onError={() => setFailed(true)} className="h-full w-full object-cover" />}
    </div>
  )
}
