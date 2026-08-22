type Props = {
  label: string
  value: number
  min: number
  max: number
  step: number
  onChange: (value: number) => void
  /**
   * 드래그를 **놓았을 때** 한 번 부른다. 주면 `onChange` 는 화면만 움직이는
   * 값이 되고, 실제 반영은 여기서 한다.
   *
   * ⚠ 없으면 지금까지와 똑같이 동작한다 — 추론 파라미터는 매 틱 반영이 맞다
   * (버스 큐에 넣는 것이라 싸고, 슬라이더를 끄는 동안 팔이 따라오는 게 요점이다).
   * 반대로 한 번이 비싸거나(장치 RPC) 되돌리기 어려운 값은 놓을 때 보내야 한다.
   */
  onCommit?: (value: number) => void
  unsafe?: boolean
  /** 숫자 옆에 붙는 단위 (`mm` 등). */
  unit?: string
}

export default function ParamSlider({
  label,
  value,
  min,
  max,
  step,
  onChange,
  onCommit,
  unsafe,
  unit,
}: Props) {
  const commit = () => onCommit?.(value)
  return (
    <div className={`space-y-1 ${unsafe ? 'opacity-50' : ''}`}>
      <div className="flex justify-between text-xs">
        <span className="text-neutral-400">
          {label}
          {unsafe && (
            <span className="ml-1 text-amber-400">(재시작 필요)</span>
          )}
        </span>
        <span className="flex items-center gap-1">
          <input
            type="number"
            value={value}
            min={min}
            max={max}
            step={step}
            onChange={(e) => onChange(Number(e.target.value))}
            // 숫자는 타이핑 도중에도 `onChange` 가 뜬다 — 다 치고 나가야 반영한다
            onBlur={commit}
            onKeyDown={(e) => { if (e.key === 'Enter') commit() }}
            disabled={unsafe}
            className="w-20 text-right bg-transparent border-b border-neutral-600 focus:border-blue-500 outline-none text-neutral-100"
          />
          {unit && <span className="text-neutral-500">{unit}</span>}
        </span>
      </div>
      <input
        type="range"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        // 드래그 중에는 안 보낸다. 놓을 때·키를 뗄 때 한 번.
        onPointerUp={commit}
        onKeyUp={commit}
        disabled={unsafe}
        className="w-full accent-blue-500"
      />
    </div>
  )
}
