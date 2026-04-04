type Props = {
  label: string
  value: number
  min: number
  max: number
  step: number
  onChange: (value: number) => void
  unsafe?: boolean
}

export default function ParamSlider({
  label,
  value,
  min,
  max,
  step,
  onChange,
  unsafe,
}: Props) {
  return (
    <div className={`space-y-1 ${unsafe ? 'opacity-50' : ''}`}>
      <div className="flex justify-between text-xs">
        <span className="text-neutral-400">
          {label}
          {unsafe && (
            <span className="ml-1 text-amber-400">(재시작 필요)</span>
          )}
        </span>
        <input
          type="number"
          value={value}
          min={min}
          max={max}
          step={step}
          onChange={(e) => onChange(Number(e.target.value))}
          disabled={unsafe}
          className="w-20 text-right bg-transparent border-b border-neutral-600 focus:border-blue-500 outline-none text-neutral-100"
        />
      </div>
      <input
        type="range"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        disabled={unsafe}
        className="w-full accent-blue-500"
      />
    </div>
  )
}
