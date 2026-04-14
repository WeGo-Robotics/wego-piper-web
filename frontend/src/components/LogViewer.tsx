import { useEffect, useRef, useState } from 'react'

type Props = {
  logs: string[]
  onClear?: () => void
}

export default function LogViewer({ logs, onClear }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    const el = containerRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  }, [logs.length])

  const handleCopy = async () => {
    const text = logs.join('\n')
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // fallback: textarea를 이용한 복사 (HTTP에서도 동작)
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="space-y-0">
      <div className="flex items-center justify-end gap-1 mb-1">
        <button
          onClick={handleCopy}
          disabled={logs.length === 0}
          className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-400 hover:text-neutral-100 disabled:opacity-30"
        >
          {copied ? '복사됨' : '복사'}
        </button>
        {onClear && (
          <button
            onClick={onClear}
            disabled={logs.length === 0}
            className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-400 hover:text-neutral-100 disabled:opacity-30"
          >
            지우기
          </button>
        )}
      </div>
      <div
        ref={containerRef}
        className="rounded-lg border border-neutral-700 bg-neutral-900 p-3 h-96 overflow-auto font-mono text-xs"
      >
        {logs.length === 0 ? (
          <span className="text-neutral-500">로그 대기 중...</span>
        ) : (
          logs.map((line, i) => (
            <div
              key={i}
              className={`leading-5 ${
                line.toLowerCase().includes('error')
                  ? 'text-red-400'
                  : line.toLowerCase().includes('warn')
                    ? 'text-amber-400'
                    : 'text-neutral-300'
              }`}
            >
              {line}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
