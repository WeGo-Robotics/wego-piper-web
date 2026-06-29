import { useEffect, useRef, useState, useCallback } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { SearchAddon } from '@xterm/addon-search'
import '@xterm/xterm/css/xterm.css'

type Props = {
  logs: string[]
  onClear?: () => void
  /** 높이 CSS 클래스 (기본: h-96) */
  height?: string
  /** 빈 로그일 때 표시할 텍스트 */
  placeholder?: string
  /** 헤더 버튼 숨기기 */
  hideControls?: boolean
  /** 검색 활성화 */
  searchable?: boolean
}

export default function LogViewer({
  logs, onClear, height = 'h-96', placeholder = '로그 대기 중...',
  hideControls = false, searchable = false,
}: Props) {
  const termRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<Terminal | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const searchRef = useRef<SearchAddon | null>(null)
  const prevLenRef = useRef(0)
  const [copied, setCopied] = useState(false)
  const [search, setSearch] = useState('')

  // xterm 초기화
  useEffect(() => {
    if (!termRef.current) return

    const term = new Terminal({
      fontSize: 12,
      fontFamily: 'Menlo, Monaco, "Cascadia Code", "Courier New", monospace',
      theme: {
        background: '#171717',  // neutral-900
        foreground: '#d4d4d4',  // neutral-300
        cursor: '#d4d4d4',
        selectionBackground: '#3b82f680', // blue-500/50
      },
      convertEol: true,
      scrollback: 10000,
      disableStdin: true,
      cursorStyle: 'bar',
      cursorBlink: false,
    })

    const fit = new FitAddon()
    const srch = new SearchAddon()
    term.loadAddon(fit)
    term.loadAddon(srch)
    term.open(termRef.current)
    fit.fit()

    xtermRef.current = term
    fitRef.current = fit
    searchRef.current = srch
    prevLenRef.current = 0

    // ResizeObserver로 자동 fit
    const ro = new ResizeObserver(() => fit.fit())
    ro.observe(termRef.current)

    return () => {
      ro.disconnect()
      term.dispose()
      xtermRef.current = null
      fitRef.current = null
      searchRef.current = null
      prevLenRef.current = 0
    }
  }, [])

  // 새 로그 라인 추가
  useEffect(() => {
    const term = xtermRef.current
    if (!term) return

    const prev = prevLenRef.current
    if (logs.length === 0 && prev > 0) {
      // 클리어됨
      term.clear()
      term.write(`\x1b[90m${placeholder}\x1b[0m`)
      prevLenRef.current = 0
      return
    }

    if (logs.length < prev) {
      // 로그가 줄었으면 (새 세션 등) 전체 다시 쓰기
      term.reset()
      for (const line of logs) {
        term.writeln(line)
      }
      prevLenRef.current = logs.length
      return
    }

    // 새로 추가된 라인만 쓰기
    if (prev === 0 && logs.length === 0) {
      term.write(`\x1b[90m${placeholder}\x1b[0m`)
    }
    for (let i = prev; i < logs.length; i++) {
      if (i === 0 && prev === 0) {
        // placeholder 덮어쓰기
        term.reset()
      }
      term.writeln(logs[i])
    }
    prevLenRef.current = logs.length
  }, [logs, placeholder])

  // 검색
  useEffect(() => {
    const srch = searchRef.current
    if (!srch) return
    if (search) {
      srch.findNext(search, { regex: false, caseSensitive: false, decorations: {
        matchOverviewRuler: '#fbbf24',
        activeMatchColorOverviewRuler: '#f59e0b',
        matchBackground: '#fbbf2440',
        activeMatchBackground: '#f59e0b80',
      }})
    } else {
      srch.clearDecorations()
    }
  }, [search])

  const handleCopy = useCallback(async () => {
    const term = xtermRef.current
    if (!term) return
    // 선택된 텍스트가 있으면 그것만, 없으면 전체
    const text = term.getSelection() || logs.join('\n')
    try {
      await navigator.clipboard.writeText(text)
    } catch {
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
  }, [logs])

  const handleSearchNext = () => searchRef.current?.findNext(search, { regex: false, caseSensitive: false })
  const handleSearchPrev = () => searchRef.current?.findPrevious(search, { regex: false, caseSensitive: false })

  return (
    <div className="space-y-0">
      {!hideControls && (
        <div className="flex items-center justify-between gap-1 mb-1">
          <div className="flex items-center gap-1">
            {searchable && (
              <>
                <input type="text" value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') { e.shiftKey ? handleSearchPrev() : handleSearchNext() } }}
                  placeholder="검색 (Enter/Shift+Enter)..."
                  className="px-2 py-0.5 text-[10px] rounded bg-neutral-800 border border-neutral-700 text-neutral-300 w-44 placeholder:text-neutral-500" />
              </>
            )}
            <span className="text-[10px] text-neutral-500">{logs.length} lines</span>
          </div>
          <div className="flex items-center gap-1">
            <button onClick={() => { const term = xtermRef.current; if (term) term.scrollToBottom() }}
              className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-400 hover:text-neutral-100">
              하단
            </button>
            <button onClick={handleCopy} disabled={logs.length === 0}
              className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-400 hover:text-neutral-100 disabled:opacity-30">
              {copied ? '복사됨' : '복사'}
            </button>
            {onClear && (
              <button onClick={onClear} disabled={logs.length === 0}
                className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-400 hover:text-neutral-100 disabled:opacity-30">
                지우기
              </button>
            )}
          </div>
        </div>
      )}
      <div
        ref={termRef}
        className={`rounded-lg border border-neutral-700 overflow-hidden ${height}`}
      />
    </div>
  )
}
