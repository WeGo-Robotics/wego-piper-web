import { useMemo } from 'react'
import type { Model } from '../types/models'

/**
 * 학습 → 체크포인트, 두 단계로 고른다.
 *
 * ## 왜 나눴나
 *
 * 한 학습이 체크포인트를 10~20개 남긴다. 목록 하나에 다 넣으면 금세 수십 개가
 * 되고(실측: 학습 10개에 체크포인트 72개), 이름만으로는 어느 학습의 몇 번째인지
 * 읽기 어렵다. 먼저 학습을 고르면 그 안에서 고를 것이 10개 남짓이 된다.
 *
 * ⚠ **묶는 규칙은 백엔드가 정한다.** `id` 를 화면에서 `/` 로 쪼개면 HF 허브
 * 모델(`PekingU/rtdetr_v2_r18vd`)까지 "PekingU 학습" 으로 묶인다. `run` 이 있는
 * 것만 학습 산출물이다.
 */

type Props = {
  models: Model[]
  value: string
  onChange: (id: string) => void
  /** 정책이 아닌 것도 고를 수 있게 (검출 모델 등) */
  includeNonPolicy?: boolean
}

function ckptLabel(m: Model): string {
  if (m.checkpoint === 'last') return 'last (마지막)'
  return m.step != null ? `${m.step.toLocaleString()} 스텝` : (m.checkpoint ?? m.id)
}

export default function CheckpointPicker({ models, value, onChange, includeNonPolicy }: Props) {
  const usable = useMemo(
    () => models.filter((m) => includeNonPolicy || m.is_policy !== false),
    [models, includeNonPolicy])

  const runs = useMemo(() => {
    const by = new Map<string, Model[]>()
    for (const m of usable) {
      // `run` 이 없는 것(허브 모델·직접 놓은 것)은 한 묶음으로 모은다
      const key = m.run ?? ''
      const list = by.get(key)
      if (list) list.push(m)
      else by.set(key, [m])
    }
    for (const list of by.values()) {
      // ⚠ `last` 가 맨 위다 — 대개 그걸 쓴다. 나머지는 **최신 스텝부터**.
      //   문자열 정렬이면 `9000` 이 `10000` 보다 뒤로 간다.
      list.sort((a, b) => {
        if (a.checkpoint === 'last') return -1
        if (b.checkpoint === 'last') return 1
        return (b.step ?? 0) - (a.step ?? 0)
      })
    }
    return [...by.entries()]
      .map(([run, list]) => ({ run, list, modified: list[0]?.modified ?? '' }))
      .sort((a, b) => {
        if (!a.run) return 1          // 묶이지 않은 것은 맨 아래
        if (!b.run) return -1
        return b.modified.localeCompare(a.modified)   // 최근 학습부터
      })
  }, [usable])

  const selected = usable.find((m) => m.id === value)
  const currentRun = selected?.run ?? (selected ? '' : (runs[0]?.run ?? ''))
  const ckpts = runs.find((r) => r.run === currentRun)?.list ?? []

  const pickRun = (run: string) => {
    const list = runs.find((r) => r.run === run)?.list ?? []
    // ⚠ 학습을 바꾸면 **그 학습의 첫 체크포인트**로 바로 옮긴다. 비워 두면
    //   "골랐는데 아무 일도 안 일어나는" 상태가 되고, 그게 고장으로 읽힌다.
    onChange(list[0]?.id ?? '')
  }

  return (
    <div className="space-y-2">
      <div>
        <label className="mb-1 block text-xs text-neutral-400">학습</label>
        <select value={currentRun} onChange={(e) => pickRun(e.target.value)}
          className="w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 focus:border-blue-500 focus:outline-none">
          <option value="" disabled={!runs.some((r) => !r.run)}>
            {runs.length ? '학습 선택…' : '학습 결과 없음'}
          </option>
          {runs.map((r) => (
            <option key={r.run || '_other'} value={r.run}>
              {r.run || '그 외 (직접 받은 모델)'} · {r.list.length}개
              {r.list[0]?.policy_type && r.run ? ` · ${r.list[0].policy_type}` : ''}
            </option>
          ))}
        </select>
      </div>
      <div>
        <label className="mb-1 block text-xs text-neutral-400">체크포인트</label>
        <select value={value} onChange={(e) => onChange(e.target.value)}
          disabled={!ckpts.length}
          className="w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 focus:border-blue-500 focus:outline-none disabled:opacity-50">
          <option value="">{ckpts.length ? '체크포인트 선택…' : '먼저 학습을 고르세요'}</option>
          {ckpts.map((m) => (
            <option key={m.id} value={m.id}>{ckptLabel(m)}</option>
          ))}
        </select>
      </div>
    </div>
  )
}
