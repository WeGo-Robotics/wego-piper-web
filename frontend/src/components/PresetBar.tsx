import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 프리셋 저장·불러오기 바 — 도메인만 바꿔 재사용한다.
 *
 * 이전에는 설정이 브라우저 localStorage 의 **이름 없는 프리셋 1개**였다.
 * 브라우저를 바꾸면 사라지고 팀에서 공유할 수도 없었다
 * (feature/parameter-presets.md).
 */

export type PresetMeta = {
  name: string
  scope: string
  policy_type: string
  note: string
  updated_at: string
}

type ApplyReport = {
  values: Record<string, unknown>
  missing: string[]
  unknown: string[]
  clamped: { key: string; saved: number; applied: number }[]
  policy_mismatch: { saved: string; current: string } | null
}

type Props = {
  domain: string
  /** 지금 화면의 값 — 저장 시 이걸 보낸다 */
  values: () => Record<string, unknown>
  /** 불러온 값을 화면에 적용 */
  onApply: (values: Record<string, unknown>) => void
  /** 정책 타입이 있는 도메인(학습·추론)이면 넘긴다 */
  policyType?: string
  /** 프리셋에 없는 키를 채울 기본값 */
  defaults?: Record<string, unknown>
  scope?: 'device' | 'shared'
  disabled?: boolean
  /** 선택된 프리셋 이름 알림 — 평가 기록에 남긴다 */
  onSelect?: (name: string) => void
  /**
   * 저장·적용을 도메인이 가로챈다. 돌려준 문자열이 그대로 상태줄에 뜬다.
   *
   * 카메라 프로파일이 이걸 쓴다: 저장할 값이 화면이 아니라 **장치**에 있고,
   * 적용도 게이트웨이가 아니라 데몬이 순서대로 해야 한다. 목록·선택·삭제는
   * 다른 도메인과 똑같아서 그대로 공유한다.
   */
  onSaveAs?: (name: string) => Promise<string>
  onApplyName?: (name: string) => Promise<string>
}

export default function PresetBar({
  domain, values, onApply, policyType = '', defaults = {},
  scope = 'device', disabled = false, onSelect, onSaveAs, onApplyName,
}: Props) {
  const [list, setList] = useState<PresetMeta[]>([])
  const [selected, setSelected] = useState('')
  const [msg, setMsg] = useState('')

  const refresh = useCallback(() => {
    api.get<PresetMeta[]>(`/presets/${domain}`).then(setList).catch(() => {})
  }, [domain])

  useEffect(() => { refresh() }, [refresh])

  const handleSave = async () => {
    const name = window.prompt('프리셋 이름', selected || '')
    if (!name) return
    try {
      if (onSaveAs) {
        setMsg(await onSaveAs(name))
      } else {
        await api.post(`/presets/${domain}`, {
          name, values: values(), scope, policy_type: policyType,
        })
        setMsg(`"${name}" 저장됨`)
      }
      setSelected(name)
      onSelect?.(name)
      refresh()
    } catch (e) {
      setMsg(`저장 실패: ${(e as Error).message}`)
    }
  }

  const handleApply = async (name: string) => {
    if (!name) return
    if (onApplyName) {
      try {
        onSelect?.(name)
        setMsg(await onApplyName(name))
      } catch (e) {
        setMsg(`적용 실패: ${(e as Error).message}`)
      }
      return
    }
    try {
      const r = await api.post<ApplyReport>(`/presets/${domain}/${name}/apply`, {
        policy_type: policyType, defaults,
      })
      onApply(r.values)
      onSelect?.(name)
      // 조용히 버리지 않는다 — 무엇이 무시됐고 채워졌는지 알린다
      const notes: string[] = []
      if (r.policy_mismatch) {
        notes.push(`정책이 다름(${r.policy_mismatch.saved} → ${r.policy_mismatch.current}), 공통 항목만 적용`)
      }
      if (r.clamped?.length) notes.push(`범위 조정: ${r.clamped.map((c) => `${c.key} ${c.saved}→${c.applied}`).join(', ')}`)
      if (r.unknown.length) notes.push(`무시됨: ${r.unknown.join(', ')}`)
      if (r.missing.length) notes.push(`기본값으로 채움: ${r.missing.join(', ')}`)
      setMsg(notes.length ? `"${name}" 적용 — ${notes.join(' / ')}` : `"${name}" 적용됨`)
    } catch (e) {
      setMsg(`불러오기 실패: ${(e as Error).message}`)
    }
  }

  const handleDelete = async () => {
    if (!selected) return
    try {
      await api.delete(`/presets/${domain}/${selected}`)
      setSelected('')
      onSelect?.('')
      setMsg(`"${selected}" 삭제됨`)
      refresh()
    } catch (e) {
      setMsg(`삭제 실패: ${(e as Error).message}`)
    }
  }

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 text-xs">
        <span className="text-neutral-400">프리셋</span>
        <select
          value={selected}
          onChange={(e) => { setSelected(e.target.value); handleApply(e.target.value) }}
          disabled={disabled}
          className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 disabled:opacity-50">
          <option value="">— 선택 —</option>
          {list.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name}{p.policy_type ? ` (${p.policy_type})` : ''}
            </option>
          ))}
        </select>
        <button onClick={handleSave} disabled={disabled}
          className="px-2 py-1 rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-40">저장</button>
        <button onClick={handleDelete} disabled={disabled || !selected}
          className="px-2 py-1 rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-40">삭제</button>
      </div>
      {msg && <p className="text-[10px] text-neutral-400">{msg}</p>}
    </div>
  )
}
