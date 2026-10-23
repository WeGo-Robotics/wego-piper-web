/**
 * 에피소드 프레임 고르기 — 엔코더 프로브 슬롯에 **학습 데이터의 프레임**을 넣는다
 * (feature/weight-analyzer.md 단계 5, W6).
 *
 * 카메라 캡처는 "지금" 장면뿐이고 파일 추가는 손으로 뽑아 와야 한다. 정책이 실제로 배운 프레임을
 * 그대로 넣어야 "학습 장면에서 인코더가 무엇을 보나" 를 볼 수 있다. 이미지는 `/weval/frames` 가
 * **학습과 같은 디코더**로 꺼낸 PNG 다 — 브라우저 `<video>` 캡처와 달리 한 프레임도 안 어긋난다.
 *
 * 에피소드·프레임은 `0, 3, 5-7` 처럼 여러 개를 쓰고, 조합 전부를 한 번에 꺼낸다(데이터셋을 여는 것이
 * 비싸서). 한 슬롯은 한 번에 MAX_IMAGES 장까지 인코딩하므로 그 이상은 막는다.
 */
import { useMemo, useState } from 'react'
import { api } from '../services/api'
import { useT } from '../i18n'
import type { Dataset } from '../types/models'
import type { FramesResponse } from './weightTypes'

// 엔코더 프로브 한 번의 인코딩 한도 — backend/app/services/encoder_probe.py MAX_BATCH 와 같다
const MAX_IMAGES = 24
const STORE_KEY = 'encoder-episode-dataset'

/** `0, 3, 5-7` → [0, 3, 5, 6, 7]. 못 읽는 조각은 버린다. */
function parseList(text: string): number[] {
  const out: number[] = []
  for (const part of text.split(',')) {
    const m = part.trim().match(/^(\d+)\s*(?:-\s*(\d+))?$/)
    if (!m) continue
    const a = Number(m[1]), b = m[2] != null ? Number(m[2]) : a
    for (let v = Math.min(a, b); v <= Math.max(a, b) && out.length < 1000; v++) out.push(v)
  }
  return [...new Set(out)]
}

const camerasOf = (d: Dataset | undefined) =>
  Object.keys(d?.features ?? {}).filter((k) => k.startsWith('observation.images.'))
    .map((k) => k.slice('observation.images.'.length))

async function toDataUrl(url: string): Promise<string> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`${res.status}`)
  const blob = await res.blob()
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(r.error)
    r.readAsDataURL(blob)
  })
}

export default function EpisodeFramePicker({ datasets, room, onAdd }: {
  datasets: Dataset[]
  /** 이 슬롯에 더 넣을 수 있는 장 수 */
  room: number
  onAdd: (items: { name: string; src: string }[]) => void
}) {
  const t = useT()
  const choices = useMemo(() => datasets.filter((d) => !d.baked), [datasets])
  const [ds, setDs] = useState(() => {
    try { return localStorage.getItem(STORE_KEY) ?? '' } catch { return '' }
  })
  const [eps, setEps] = useState('0')
  const [frs, setFrs] = useState('0')
  const [cam, setCam] = useState('')            // '' = 모든 카메라
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const dataset = choices.find((d) => d.id === ds)
  const cams = camerasOf(dataset)
  const episodes = parseList(eps)
  const frames = parseList(frs)
  const count = episodes.length * frames.length * (cam ? 1 : Math.max(1, cams.length))
  const limit = Math.min(MAX_IMAGES, room)

  const pickDs = (id: string) => {
    setDs(id)
    setCam('')
    try { localStorage.setItem(STORE_KEY, id) } catch { /* 사생활 모드 */ }
  }

  const add = async () => {
    setBusy(true)
    setError('')
    try {
      const r = await api.post<FramesResponse>('/weval/frames', { dataset: ds, episodes, frames }, { timeoutMs: 300_000 })
      const use = cam ? [cam] : r.cameras
      const short = ds.slice(ds.lastIndexOf('/') + 1)
      const items: { name: string; src: string }[] = []
      for (const it of r.items) {
        if (!it.dir) continue
        for (const c of use) {
          items.push({ name: `${short} ep${it.episode} f${it.frame} ${c}`,
                       src: await toDataUrl(`/api/weval/frames/${r.sid}/${it.dir}/${c}.png`) })
        }
      }
      onAdd(items)
      const missing = r.items.filter((it) => !it.dir)
      if (missing.length) {
        setError(t('없는 프레임 {n}개는 건너뛰었다: {list}', {
          n: missing.length, list: missing.map((m) => `ep${m.episode} f${m.frame}`).join(', '),
        }))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const input = 'w-24 rounded bg-neutral-900 border border-neutral-700 px-1.5 py-1 text-xs'
  return (
    <div className="rounded border border-neutral-700 bg-neutral-900/60 p-2 space-y-1.5">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <select value={ds} onChange={(e) => pickDs(e.target.value)}
                className="max-w-[16rem] rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs text-neutral-100">
          <option value="">{t('데이터셋 선택…')}</option>
          {choices.map((d) => <option key={d.id} value={d.id}>{d.id} ({d.total_episodes})</option>)}
        </select>
        <span className="text-neutral-500">{t('에피소드')}</span>
        <input value={eps} onChange={(e) => setEps(e.target.value)} className={input} placeholder="0, 3, 5-7" />
        <span className="text-neutral-500">{t('프레임')}</span>
        <input value={frs} onChange={(e) => setFrs(e.target.value)} className={input} placeholder="0, 60" />
        <select value={cam} onChange={(e) => setCam(e.target.value)} disabled={!dataset}
                className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs text-neutral-100">
          <option value="">{t('모든 카메라')}</option>
          {cams.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <button onClick={() => void add()}
                disabled={busy || !dataset || !episodes.length || !frames.length || count > limit}
                className="px-2 py-1 rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-40">
          {busy ? t('꺼내는 중…') : t('{n}장 넣기', { n: count })}
        </button>
      </div>
      <div className="text-[10px] text-neutral-500">
        {count > limit
          ? <span className="text-amber-400">{t('한 슬롯에 한 번에 {limit}장까지 — 에피소드·프레임·카메라를 줄이세요', { limit })}</span>
          : t('학습과 같은 디코더로 꺼낸다 — 정책이 실제로 배운 그 프레임이다. 0, 3, 5-7 처럼 여러 개를 쓸 수 있다')}
      </div>
      {error && <div className="text-[11px] text-red-400">{error}</div>}
    </div>
  )
}
