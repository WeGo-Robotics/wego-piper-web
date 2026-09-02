import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 작업(수집·추론)이 쓸 카메라 프로파일 선택.
 *
 * 시작 요청에 실려 가서 **시작 직전에 한 번 적용**된다 — 노출·WB 가 학습
 * 데이터와 같아야 관측이 같은 분포가 된다. 선택은 페이지가 저장한다(용도별로
 * 다른 프로파일을 쓸 수 있어야 하니 전역 "활성"과는 별개다).
 *
 * 프로파일 관리(캡처·삭제)는 카메라 페이지의 몫 — 여기는 고르기만 한다.
 */

type PresetMeta = { name: string; note?: string; updated_at?: string }

export default function CameraProfilePicker({ value, onChange }: {
  value: string; onChange: (v: string) => void
}) {
  const [names, setNames] = useState<string[]>([])

  useEffect(() => {
    api.get<PresetMeta[]>('/presets/camera')
      .then((list) => setNames(list.map((p) => p.name)))
      .catch(() => {})
  }, [])

  // 저장해둔 선택이 지워진 프로파일이면 **숨기지 않고 그대로 보여준다** —
  // 시작할 때 백엔드가 거부하며 이름을 말해주는 쪽이, 조용히 해제되는 것보다 낫다.
  const options = value && !names.includes(value) ? [value, ...names] : names

  return (
    <div className="space-y-1 pt-2">
      <label className="text-xs text-neutral-400">카메라 프로파일 (시작 시 1회 적용)</label>
      <select value={value} onChange={(e) => onChange(e.target.value)}
        title="시작 직전에 이 프로파일의 노출·화이트밸런스를 카메라에 밀어 넣습니다.
카메라 페이지에서 캡처한 프로파일이 목록에 나옵니다."
        className="w-full px-2 py-1.5 rounded bg-neutral-900 border border-neutral-700
                   text-sm text-neutral-100">
        <option value="">적용 안 함</option>
        {options.map((n) => <option key={n} value={n}>{n}</option>)}
      </select>
    </div>
  )
}
