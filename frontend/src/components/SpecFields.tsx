import type { SpecField } from '../hooks/usePolicyUi'

/**
 * 스펙이 준 필드를 그린다 — 정책마다 다른 것은 **스펙에만** 있다.
 *
 * 스펙은 "어떤 필드가 있고 범위는 얼마인가"를 말하고, 배치는 여기가 정한다.
 * 그 선을 지키는 게 이 구조의 전부다: YAML 이 열 수·컴포넌트까지 정하기
 * 시작하면 타입 검사도 디버깅도 안 되는 JSX 를 데이터로 쓰게 된다.
 */

type Props = {
  fields: SpecField[]
  values: Record<string, number | boolean>
  onChange: (key: string, value: number | boolean) => void
}

export default function SpecFields({ fields, values, onChange }: Props) {
  if (fields.length === 0) return null
  return (
    <div className="grid grid-cols-2 gap-2">
      {fields.map((f) => (
        <div key={f.key} className="space-y-0.5">
          {f.kind === 'bool' ? (
            <label className="flex items-center gap-2 text-xs cursor-pointer">
              <input type="checkbox" checked={Boolean(values[f.key] ?? f.default)}
                onChange={(e) => onChange(f.key, e.target.checked)}
                className="accent-blue-500" />
              <span className="text-neutral-400">{f.label}</span>
            </label>
          ) : (
            <>
              <label className="text-xs text-neutral-400">{f.label}</label>
              <input type="number" value={Number(values[f.key] ?? f.default)}
                // ⚠ `min`/`max` 가 없으면 속성을 안 넘긴다. `undefined` 로 두면
                // 브라우저가 제한을 안 걸지만, 0 으로 떨어뜨리면 입력이 막힌다.
                min={f.min ?? undefined} max={f.max ?? undefined} step={f.step ?? 1}
                onChange={(e) => onChange(f.key, Number(e.target.value))}
                className="w-full px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100" />
            </>
          )}
          {/* 상류와 일부러 다른 기본값이면 왜 그런지 보여준다 —
              "LeRobot 문서와 다른데?" 를 코드까지 안 가고 답한다. */}
          {f.override_reason && (
            <p className="text-[10px] text-neutral-500" title={f.override_reason}>
              기본값이 LeRobot 과 다름
            </p>
          )}
        </div>
      ))}
    </div>
  )
}
