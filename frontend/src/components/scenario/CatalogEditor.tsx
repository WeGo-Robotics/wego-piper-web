import { useT } from '../../i18n'
import type { CatalogItem } from './types'

/**
 * 물체 목록 — 종류마다 이름·색·크기·**실물 개수**.
 *
 * 목록 순서가 "종류 순서" 규칙의 우선순위다(위가 먼저). id 는 영문 — 계획·사이드카에 남는
 * 이름이라 화면 언어가 바뀌어도 그대로여야 한다.
 */

type Props = { items: CatalogItem[]; onChange: (items: CatalogItem[]) => void }

const input = 'w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100'

export default function CatalogEditor({ items, onChange }: Props) {
  const t = useT()
  const set = (i: number, patch: Partial<CatalogItem>) =>
    onChange(items.map((it, k) => (k === i ? { ...it, ...patch } : it)))
  const move = (i: number, d: number) => {
    const j = i + d
    if (j < 0 || j >= items.length) return
    const next = [...items]
    ;[next[i], next[j]] = [next[j], next[i]]
    onChange(next)
  }
  const add = () => {
    let n = items.length + 1
    while (items.some(i => i.id === `obj${n}`)) n++
    onChange([...items, { id: `obj${n}`, label: t('물체 {n}', { n }), color: '#888888', size_mm: 70, count: 1 }])
  }

  return (
    <div className="space-y-1.5">
      <div className="grid grid-cols-[2rem_1fr_6rem_4.5rem_4rem_3.5rem] gap-1.5 text-[11px] text-neutral-500">
        <span />
        <span>{t('이름')}</span>
        <span>id</span>
        <span>{t('크기 mm')}</span>
        <span>{t('실물 개수')}</span>
        <span />
      </div>
      {items.map((it, i) => (
        <div key={i} className="grid grid-cols-[2rem_1fr_6rem_4.5rem_4rem_3.5rem] gap-1.5 items-center">
          <input type="color" value={it.color} onChange={e => set(i, { color: e.target.value })}
            className="w-8 h-7 rounded bg-transparent border border-neutral-700" />
          <input value={it.label} onChange={e => set(i, { label: e.target.value })} className={input} />
          <input value={it.id} className={`${input} font-mono text-xs`}
            onChange={e => set(i, { id: e.target.value.replace(/[^\w-]/g, '').slice(0, 32) })} />
          <input type="number" min={5} max={500} value={it.size_mm} className={input}
            onChange={e => set(i, { size_mm: Number(e.target.value) || 70 })} />
          <input type="number" min={1} max={50} value={it.count} className={input}
            onChange={e => set(i, { count: Math.max(1, Number(e.target.value) || 1) })} />
          <div className="flex gap-1 text-xs text-neutral-500">
            <button onClick={() => move(i, -1)} title={t('위로')} className="hover:text-white">↑</button>
            <button onClick={() => move(i, 1)} title={t('아래로')} className="hover:text-white">↓</button>
            <button onClick={() => onChange(items.filter((_, k) => k !== i))} title={t('삭제')}
              className="hover:text-red-400">✕</button>
          </div>
        </div>
      ))}
      <button onClick={add} className="px-2 py-1 rounded text-xs border border-dashed border-neutral-600 text-neutral-400 hover:text-white">
        {t('+ 물체')}
      </button>
    </div>
  )
}
