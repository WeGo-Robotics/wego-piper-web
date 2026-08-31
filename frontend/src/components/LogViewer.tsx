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
  // 직전 렌더의 logs 배열. 길이만 기억하면 링버퍼(`slice(-MAX)`)가 꽉 찬 뒤에는
  // 길이가 고정돼 "새 줄 없음"으로 오판하고 화면이 얼어붙는다 (학습 step 1K 근처에서 재현).
  const prevLogsRef = useRef<string[]>([])
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
    prevLogsRef.current = []

    // ResizeObserver로 자동 fit
    const ro = new ResizeObserver(() => fit.fit())
    ro.observe(termRef.current)

    return () => {
      ro.disconnect()
      term.dispose()
      xtermRef.current = null
      fitRef.current = null
      searchRef.current = null
      prevLogsRef.current = []
    }
  }, [])

  // 새 로그 라인 추가
  useEffect(() => {
    const term = xtermRef.current
    if (!term) return

    const prev = prevLogsRef.current
    prevLogsRef.current = logs
    if (logs === prev) return

    if (logs.length === 0) {
      if (prev.length > 0) {
        // 클리어됨
        term.clear()
      }
      term.write(`\x1b[90m${placeholder}\x1b[0m`)
      return
    }

    // 호출부는 `[...prev, line].slice(-MAX)` 로 앞을 잘라낸다. 그러면 prev 의 꼬리가
    // logs 의 머리와 겹치고, 겹친 구간 뒤가 새 줄이다. 겹침이 전혀 없으면(새 세션·
    // 과거 로그 로드) 전부 다시 쓴다.
    let overlap = -1
    for (let k = 0; k <= prev.length; k++) {
      const m = prev.length - k
      if (m > logs.length) continue
      let same = true
      for (let i = 0; i < m; i++) {
        if (prev[k + i] !== logs[i]) { same = false; break }
      }
      if (same) { overlap = m; break }
    }

    if (overlap <= 0) {
      // 이어지는 내용이 아니다 (또는 placeholder 만 떠 있다) → 전체 다시 쓰기
      term.reset()
      for (const line of logs) term.writeln(line)
      return
    }
    for (let i = overlap; i < logs.length; i++) term.writeln(logs[i])
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
