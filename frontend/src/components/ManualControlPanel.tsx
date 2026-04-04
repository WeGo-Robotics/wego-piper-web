import { useState, useRef } from 'react'
import { api } from '../services/api'

const JOINTS = [
  { name: 'joint1.pos', label: 'Joint 1', min: -100, max: 100 },
  { name: 'joint2.pos', label: 'Joint 2', min: -100, max: 100 },
  { name: 'joint3.pos', label: 'Joint 3', min: -100, max: 100 },
  { name: 'joint4.pos', label: 'Joint 4', min: -100, max: 100 },
  { name: 'joint5.pos', label: 'Joint 5', min: -100, max: 100 },
  { name: 'joint6.pos', label: 'Joint 6', min: -100, max: 100 },
  { name: 'gripper.pos', label: 'Gripper', min: 0, max: 100 },
]

type Props = {
  currentJoints: number[]
  disabled: boolean
}

export default function ManualControlPanel({ currentJoints, disabled }: Props) {
  const [values, setValues] = useState<Record<string, number>>(() => {
    const init: Record<string, number> = {}
    JOINTS.forEach((j, i) => { init[j.name] = currentJoints[i] ?? 0 })
    return init
  })
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined)

  const handleChange = (name: string, value: number) => {
    const next = { ...values, [name]: value }
    setValues(next)
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      api.post('/params/manual-action', { action: next }).catch(() => {})
    }, 50)
  }

  // currentJoints가 바뀌면 슬라이더 동기화 (추론 재개 후 다시 돌아올 때)
  const syncFromRobot = () => {
    const synced: Record<string, number> = {}
    JOINTS.forEach((j, i) => { synced[j.name] = currentJoints[i] ?? 0 })
    setValues(synced)
  }

  return (
    <div className={`rounded-lg border p-4 space-y-3 ${disabled ? 'border-neutral-700 bg-neutral-800 opacity-40' : 'border-amber-500/50 bg-amber-500/5'}`}>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">수동 조작</h3>
        {!disabled && (
          <button onClick={syncFromRobot} className="text-[10px] text-neutral-400 hover:text-blue-400">
            로봇 위치 동기화
          </button>
        )}
      </div>
      {disabled && (
        <p className="text-xs text-neutral-500">일시정지 중에만 사용 가능</p>
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
              value={values[joint.name] ?? 0}
              onChange={(e) => handleChange(joint.name, Number(e.target.value))}
              disabled={disabled}
              className="flex-1 accent-amber-500"
            />
            <input
              type="number"
              value={Math.round(values[joint.name] ?? 0)}
              onChange={(e) => handleChange(joint.name, Number(e.target.value))}
              disabled={disabled}
              className="w-14 text-right px-1 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-amber-500 disabled:opacity-50"
            />
          </div>
        ))}
      </div>
    </div>
  )
}
