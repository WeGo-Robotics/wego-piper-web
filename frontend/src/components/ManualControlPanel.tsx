import { useState, useRef } from 'react'
import { JOINTS } from '../config/joints'

/**
 * 관절 슬라이더. **목적지는 주입받는다.**
 *
 * 원래는 추론 경로(`/params/manual-action`)가 안에 박혀 있어서, 정책을 띄우고
 * 일시정지한 상태에서만 쓸 수 있었다. 같은 슬라이더가 웹 조그(추론 없이 팔만
 * 움직이기)에도 필요한데, 목적지가 박혀 있으면 복사본이 하나 더 생긴다 —
 * 그러면 관절 순서나 디바운스를 한쪽만 고치는 사고가 난다.
 *
 * ⚠ 기본 목적지를 두지 않는다. 어디로 보내는지가 이 컴포넌트에서 가장 중요한
 * 사실인데, 기본값이 있으면 호출부가 그걸 안 보고 지나간다.
 */

type Props = {
  currentJoints: number[]
  disabled: boolean
  /** 목표를 보낸다. **전 관절 절대 목표**(정규화)다. */
  onSend: (values: Record<string, number>) => void
  title?: string
  /** 못 쓰는 이유. `disabled` 일 때만 뜬다. */
  disabledHint?: string
}

export default function ManualControlPanel({
  currentJoints, disabled, onSend, title = '수동 조작',
  disabledHint = '일시정지 중에만 사용 가능',
}: Props) {
  const [values, setValues] = useState<Record<string, number>>(() => {
    const init: Record<string, number> = {}
    JOINTS.forEach((j, i) => { init[j.actionKey] = currentJoints[i] ?? 0 })
    return init
  })
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined)

  const handleChange = (name: string, value: number) => {
    const next = { ...values, [name]: value }
    setValues(next)
    clearTimeout(debounceRef.current)
    // 50ms 디바운스. 슬라이더는 초당 수십 번 바뀌는데 그때마다 보내면
    // 목표가 밀려 팔이 뒤늦게 따라온다.
    debounceRef.current = setTimeout(() => onSend(next), 50)
  }

  // currentJoints가 바뀌면 슬라이더 동기화 (추론 재개 후 다시 돌아올 때)
  const syncFromRobot = () => {
    const synced: Record<string, number> = {}
    JOINTS.forEach((j, i) => { synced[j.actionKey] = currentJoints[i] ?? 0 })
    setValues(synced)
  }

  return (
    <div className={`rounded-lg border p-4 space-y-3 ${disabled ? 'border-neutral-700 bg-neutral-800 opacity-40' : 'border-amber-500/50 bg-amber-500/5'}`}>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{title}</h3>
        {!disabled && (
          <button onClick={syncFromRobot} className="text-[10px] text-neutral-400 hover:text-blue-400">
            로봇 위치 동기화
          </button>
        )}
      </div>
      {disabled && (
        <p className="text-xs text-neutral-500">{disabledHint}</p>
      )}
      <div className="space-y-2">
        {JOINTS.map((joint) => (
          <div key={joint.name} className="flex items-center gap-2 text-xs">
            <span className="w-14 text-neutral-400">{joint.label}</span>
            <input
              type="range"
              min={joint.min}
              max={joint.max}
              step={1}
              value={values[joint.actionKey] ?? 0}
              onChange={(e) => handleChange(joint.actionKey, Number(e.target.value))}
              disabled={disabled}
              className="flex-1 accent-amber-500"
            />
            <input
              type="number"
              value={Math.round(values[joint.actionKey] ?? 0)}
              onChange={(e) => handleChange(joint.actionKey, Number(e.target.value))}
              disabled={disabled}
              className="w-14 text-right px-1 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-amber-500 disabled:opacity-50"
            />
          </div>
        ))}
      </div>
    </div>
  )
}
