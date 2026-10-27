import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../services/api'
import { useWebSocket } from '../hooks/useWebSocket'
import type { AgentChatEvent } from '../types/ws'
import { useSystemMessage } from './SystemMessages'
import { useT } from '../i18n'

/**
 * 우측 채팅 — 게이트웨이 기계의 AI CLI(설정 → 에이전트에서 고른 것)와 한 턴씩 (feature/agent-cli.md §4-6).
 *
 * ## 모달이 아니다
 *
 * `fixed` 패널이 아니라 레이아웃의 **오른쪽 열**이다. 본문을 가리지 않고, 어느 페이지로 가도 남는다.
 * 이벤트 루프를 막는 것(`alert`·`confirm`)은 없다 — E-stop heartbeat 가 계속 나가야 한다.
 *
 * ## 보여 주는 것
 *
 * 말풍선(사람·에이전트), 도구 호출(접힌 한 줄), 글 조각 스트리밍, 턴의 비용·시간. 모드(관찰/조종)는
 * 서버가 도구 목록으로 강제한다 — 화면은 고르기만 한다. 코드 수정은 어느 모드에도 없다.
 *
 * ## 못 쓰는 환경
 *
 * 컨테이너 게이트웨이, CLI 없음, 채팅을 모르는 CLI(aider)는 서버가 이유를 준다 — 그대로 보여 주고 입력을 막는다.
 */

type Msg = { role: 'user' | 'assistant' | 'tool' | 'tool_result'; text?: string; name?: string; input?: unknown
  turn: number; is_error?: boolean; at?: number }
type Status = {
  available: boolean; reason: string | null; cli: string; cli_name: string; version: string | null
  multi_turn: boolean; ext_enabled: boolean; mode: 'observe' | 'control' | 'develop'; session_id: string | null
  running: boolean; turn: number; messages: Msg[]; budget_usd: number; timeout_s: number
  last: { turn: number; rc: number; duration_ms: number } | null
}

const CHAT_KEY = 'piper.agentchat.open'
const WIDTH_KEY = 'piper.agentchat.width'
const DEFAULT_W = 416          // 26rem — 처음 모양 그대로
const MIN_W = 320
const MAX_W = 1100
const MAIN_MIN_W = 480         // 본문이 이보다 좁아지면 페이지가 망가진다 — 그 전에 멈춘다

function clampWidth(w: number): number {
  const viewportMax = Math.max(MIN_W, window.innerWidth - MAIN_MIN_W)
  return Math.round(Math.min(MAX_W, viewportMax, Math.max(MIN_W, w)))
}
const AgentChatContext = createContext<{ open: boolean; toggle: () => void }>({ open: false, toggle: () => {} })

/** 열림 상태 하나를 상태바의 토글과 오른쪽 열이 같이 본다. 열림 여부는 기억한다 — 페이지를 옮겨도, 다시 와도. */
export function AgentChatProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(() => { try { return localStorage.getItem(CHAT_KEY) === '1' } catch { return false } })
  const toggle = useCallback(() => {
    setOpen((o) => { try { localStorage.setItem(CHAT_KEY, o ? '0' : '1') } catch { /* 저장 못 해도 열고 닫힌다 */ } return !o })
  }, [])
  return <AgentChatContext.Provider value={{ open, toggle }}>{children}</AgentChatContext.Provider>
}

export function useAgentChat() {
  return useContext(AgentChatContext)
}

/** 상태바에 앉는 토글 — 벨과 같은 종류다(명령이 아니라 패널을 연다). 상태바 파일은 읽기 전용으로 남긴다. */
export function AgentChatToggle() {
  const t = useT()
  const { open, toggle } = useAgentChat()
  return (
    <button onClick={toggle} aria-pressed={open}
            className={`flex shrink-0 items-center px-1 text-base leading-none ${open ? 'opacity-100' : 'opacity-80 hover:opacity-100'}`}
            title={open ? t('에이전트 채팅 닫기') : t('에이전트 채팅 열기')} aria-label={t('에이전트 채팅')}>
      <span aria-hidden>🤖</span>
    </button>
  )
}

export default function AgentChatDrawer() {
  const t = useT()
  const { open, toggle: onClose } = useAgentChat()
  const { notify } = useSystemMessage()
  const [st, setSt] = useState<Status | null>(null)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [draft, setDraft] = useState('')
  const [running, setRunning] = useState(false)
  const [partial, setPartial] = useState<{ turn: number; text: string } | null>(null)
  const [lastResult, setLastResult] = useState<AgentChatEvent | null>(null)
  const [mcp, setMcp] = useState<{ name: string; status: string }[] | null>(null)
  const endRef = useRef<HTMLDivElement | null>(null)

  // 너비 — 왼쪽 가장자리를 끌어 바꾼다. 기억한다. 더블클릭이면 기본 너비, 키보드면 화살표로 24px 씩.
  const [width, setWidth] = useState(() => {
    try { const v = Number(localStorage.getItem(WIDTH_KEY)); return v >= MIN_W ? clampWidth(v) : DEFAULT_W } catch { return DEFAULT_W }
  })
  const drag = useRef<{ startX: number; startW: number } | null>(null)
  const saveWidth = (w: number) => { try { localStorage.setItem(WIDTH_KEY, String(w)) } catch { /* 못 적어도 이번 세션엔 적용된다 */ } }
  const onHandleDown = (e: React.PointerEvent<HTMLDivElement>) => {
    drag.current = { startX: e.clientX, startW: width }
    e.currentTarget.setPointerCapture(e.pointerId)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }
  const onHandleMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    setWidth(clampWidth(drag.current.startW + (drag.current.startX - e.clientX)))   // 왼쪽으로 끌면 넓어진다
  }
  const onHandleUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return
    drag.current = null
    try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* 이미 풀렸다 */ }
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    saveWidth(clampWidth(width))
  }
  const resetWidth = () => { setWidth(DEFAULT_W); saveWidth(DEFAULT_W) }
  const onHandleKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.key === 'ArrowLeft' ? 24 : e.key === 'ArrowRight' ? -24 : 0
    if (!step) return
    e.preventDefault()
    const w = clampWidth(width + step)
    setWidth(w)
    saveWidth(w)
  }

  const load = useCallback(() => {
    api.get<Status>('/agent/chat', { timeoutMs: 60_000 })
      .then((s) => { setSt(s); setMsgs(s.messages); setRunning(s.running) })
      .catch(() => {})
  }, [])
  useEffect(() => { if (open) load() }, [open, load])

  // 사건은 WS 로 온다 — 턴 번호로 묶어 늦게 온 조각이 다음 말풍선에 붙지 않게 한다
  useWebSocket('/ws', {
    onMessage: (m) => {
      if (m.type !== 'agent_chat') return
      const e = m.data
      if (e.kind === 'status') { setRunning(!!e.running); if (!e.running) { setPartial(null); load() } }
      else if (e.kind === 'init') setMcp(e.mcp ?? null)
      else if (e.kind === 'delta') setPartial((p) => ({ turn: e.turn, text: (p && p.turn === e.turn ? p.text : '') + (e.text ?? '') }))
      else if (e.kind === 'text') { setPartial(null); setMsgs((prev) => [...prev, { role: 'assistant', text: e.text, turn: e.turn }]) }
      else if (e.kind === 'tool_use') setMsgs((prev) => [...prev, { role: 'tool', name: e.name, input: e.input, turn: e.turn }])
      else if (e.kind === 'tool_result') setMsgs((prev) => [...prev, { role: 'tool_result', text: e.text, turn: e.turn, is_error: e.is_error }])
      else if (e.kind === 'error') { setPartial(null); setMsgs((prev) => [...prev, { role: 'assistant', text: e.text, turn: e.turn, is_error: true }]) }
      else if (e.kind === 'result') setLastResult(e)
    },
  })

  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }) }, [msgs, partial, open])

  const send = async () => {
    const text = draft.trim()
    if (!text || running) return
    setDraft('')
    setLastResult(null)
    setMsgs((prev) => [...prev, { role: 'user', text, turn: (st?.turn ?? 0) + 1 }])
    try {
      await api.post('/agent/chat', { text })
      setRunning(true)
    } catch (e) {
      notify({ level: 'error', source: t('채팅'), text: e instanceof Error ? e.message : t('보내지 못했습니다') })
      load()
    }
  }
  const stop = () => api.post('/agent/chat/stop').catch(() => {})
  const reset = async () => { await api.post('/agent/chat/reset').catch(() => {}); setMsgs([]); setPartial(null); setLastResult(null); setMcp(null); load() }
  const setMode = async (mode: 'observe' | 'control' | 'develop') => {
    try { await api.put('/agent/chat/mode', { mode }); load() }
    catch (e) { notify({ level: 'error', source: t('채팅'), text: e instanceof Error ? e.message : t('모드를 바꾸지 못했습니다') }) }
  }

  if (!open) return null
  const piper = mcp?.find((m) => m.name === 'piper')

  return (
    <aside style={{ width }} className="relative flex shrink-0 flex-col border-l border-neutral-800 bg-neutral-900" aria-label={t('에이전트 채팅')}>
      {/* 너비 손잡이 — 왼쪽 가장자리. 끌기·더블클릭(기본 너비)·화살표 키 */}
      <div role="separator" aria-orientation="vertical" aria-label={t('채팅창 너비 조절')} tabIndex={0}
           aria-valuenow={width} aria-valuemin={MIN_W} aria-valuemax={MAX_W}
           onPointerDown={onHandleDown} onPointerMove={onHandleMove} onPointerUp={onHandleUp} onPointerCancel={onHandleUp}
           onDoubleClick={resetWidth} onKeyDown={onHandleKey}
           title={t('끌어서 너비 조절 · 더블클릭하면 기본 너비')}
           className="absolute left-0 top-0 z-10 h-full w-1.5 -translate-x-1/2 cursor-col-resize
                      hover:bg-blue-500/60 focus:bg-blue-500/60 focus:outline-none active:bg-blue-500" />
      <div className="flex items-center gap-2 border-b border-neutral-800 px-3 py-2">
        <span className="text-sm font-semibold">🤖 {st?.cli_name ?? t('에이전트')}</span>
        {st?.version && <span className="text-[10px] text-neutral-500 truncate">{st.version}</span>}
        <span className="flex-1" />
        {st?.available && (
          <div className="flex rounded border border-neutral-700 text-[11px]" role="radiogroup" aria-label={t('모드')}>
            {(['observe', 'control', 'develop'] as const).map((m) => (
              <button key={m} onClick={() => setMode(m)} disabled={running}
                className={`px-2 py-0.5 ${st.mode === m
                  ? (m === 'control' ? 'bg-amber-700 text-white' : m === 'develop' ? 'bg-violet-700 text-white' : 'bg-neutral-700 text-white')
                  : 'text-neutral-400 hover:text-neutral-200'}`}
                title={m === 'observe' ? t('관찰 — 저장소 읽기와 로봇 상태 조회만')
                  : m === 'control' ? t('조종 — 미션(dry_run 기본)·플러그인·검출기·E-stop 까지. 코드 수정은 여전히 없음')
                  : t('개발 — plugins/ 아래만 고치고 piper-plugin 으로 검사·드라이런. 설치는 사람이')}>
                {m === 'observe' ? t('관찰') : m === 'control' ? t('조종') : t('개발')}
              </button>
            ))}
          </div>
        )}
        <button onClick={reset} title={t('새 대화 — 세션과 말풍선을 비웁니다')}
          className="rounded px-2 py-0.5 text-xs text-neutral-300 hover:bg-neutral-800">{t('새 대화')}</button>
        <button onClick={onClose} aria-label={t('닫기')} className="rounded px-2 py-0.5 text-xs text-neutral-300 hover:bg-neutral-800">✕</button>
      </div>

      {st && !st.available && (
        <div className="m-3 rounded-lg border border-amber-600/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          {st.reason}
        </div>
      )}
      {st?.available && !st.ext_enabled && (
        <div className="mx-3 mt-2 rounded border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-[11px] text-neutral-400">
          {t('외부 API 가 꺼져 있어 로봇 도구 없이 답합니다 — 설정 → 에이전트를 보세요.')}
        </div>
      )}
      {st?.available && st.mode === 'develop' && (
        <div className="mx-3 mt-2 rounded border border-violet-700/60 bg-violet-950/30 px-3 py-1.5 text-[11px] text-violet-200">
          {t('개발 모드 — 에이전트가 plugins/ 아래 파일을 고치고, 자기가 쓴 코드를 이 기계에서 드라이런합니다(게이트웨이에는 못 붙습니다). 설치와 실물 실행은 사람이 플러그인 페이지에서 합니다.')}
        </div>
      )}
      {piper && piper.status !== 'connected' && (
        <div className="mx-3 mt-2 rounded border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-[11px] text-amber-300">
          {t('로봇 MCP 서버가 붙지 못했습니다 ({status}) — piper_plugin[mcp] 가 깔려 있나 보세요.', { status: piper.status })}
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-y-auto px-3 py-2 space-y-2 text-sm">
        {msgs.length === 0 && !partial && (
          <p className="px-1 py-6 text-center text-xs text-neutral-500">
            {t('"추론이 왜 죽었나", "에피소드 3 왜 빗나갔나" 처럼 물어보세요. 진단 꾸러미·에피소드 묶음을 스스로 읽습니다.')}
          </p>
        )}
        {msgs.map((m, i) => <Bubble key={`${m.turn}-${i}`} m={m} t={t} />)}
        {partial && (
          <div className="mr-4 rounded-lg border border-neutral-700 bg-neutral-800 px-3 py-2 text-neutral-200">
            <Md text={partial.text} /><span className="animate-pulse">▍</span>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="border-t border-neutral-800 px-3 py-2 space-y-1.5">
        <div className="flex items-center justify-between text-[10px] text-neutral-500">
          <span>
            {running ? t('답하는 중…') : lastResult
              ? t('비용 ${cost} · {sec}초', { cost: (lastResult.cost_usd ?? 0).toFixed(3), sec: ((lastResult.duration_ms ?? 0) / 1000).toFixed(1) })
              : st ? t('턴마다 예산 ${budget} · {timeout}초 상한', { budget: st.budget_usd, timeout: st.timeout_s }) : ''}
          </span>
          {running && <button onClick={stop} className="rounded bg-red-700 px-2 py-0.5 text-[11px] text-white hover:bg-red-600">{t('중지')}</button>}
        </div>
        <div className="flex gap-2">
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={2}
            disabled={!st?.available || running}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            placeholder={st?.available ? t('무엇이 궁금한가요? (Enter 전송, Shift+Enter 줄바꿈)') : t('이 게이트웨이에서는 채팅을 쓸 수 없습니다')}
            className="flex-1 resize-none rounded border border-neutral-700 bg-neutral-800 px-2 py-1.5 text-sm text-neutral-100 focus:outline-none focus:border-blue-500 disabled:opacity-50" />
          <button onClick={send} disabled={!st?.available || running || !draft.trim()}
            className="self-end rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-500 disabled:opacity-50">{t('보내기')}</button>
        </div>
      </div>
    </aside>
  )
}

function Bubble({ m, t }: { m: Msg; t: ReturnType<typeof useT> }) {
  if (m.role === 'user') {
    return <div className="ml-8 rounded-lg bg-blue-900/50 border border-blue-800 px-3 py-2 whitespace-pre-wrap break-words">{m.text}</div>
  }
  if (m.role === 'tool') {
    return (
      <details className="rounded border border-neutral-800 bg-neutral-950/60 px-2 py-1 text-[11px] text-neutral-400">
        <summary className="cursor-pointer font-mono truncate">🔧 {(m.name ?? '').replace(/^mcp__piper__/, 'piper:')}</summary>
        <pre className="mt-1 whitespace-pre-wrap break-all font-mono text-[10px] text-neutral-500">{JSON.stringify(m.input, null, 1)}</pre>
      </details>
    )
  }
  if (m.role === 'tool_result') {
    return (
      <details className={`rounded border px-2 py-1 text-[11px] ${m.is_error ? 'border-red-900 text-red-300' : 'border-neutral-800 text-neutral-500'}`}>
        <summary className="cursor-pointer">{m.is_error ? t('도구 실패') : t('도구 결과')}</summary>
        <pre className="mt-1 whitespace-pre-wrap break-all font-mono text-[10px]">{m.text}</pre>
      </details>
    )
  }
  return (
    <div className={`mr-4 rounded-lg border px-3 py-2 ${
      m.is_error ? 'border-red-800 bg-red-950/40 text-red-200' : 'border-neutral-700 bg-neutral-800 text-neutral-100'}`}>
      {m.is_error ? <span className="whitespace-pre-wrap break-words">{m.text}</span> : <Md text={m.text ?? ''} />}
    </div>
  )
}

/**
 * 에이전트 말풍선의 마크다운 (GFM — 표·취소선·작업 목록). HTML 은 그리지 않는다 — react-markdown 의 기본이
 * 그렇고, 에이전트 출력은 신뢰하지 않는다. Tailwind 가 제목·목록 기본 꾸밈을 지우므로 요소마다 클래스를 준다.
 * 링크는 새 창 — 메인 탭이 이동하면 heartbeat 가 끊긴다.
 */
function Md({ text }: { text: string }) {
  return (
    <div className="space-y-2 text-sm leading-6 break-words">
      <Markdown remarkPlugins={[remarkGfm]} components={{
        p: ({ children }) => <p className="whitespace-pre-wrap">{children}</p>,
        h1: ({ children }) => <h3 className="text-base font-semibold">{children}</h3>,
        h2: ({ children }) => <h3 className="text-base font-semibold">{children}</h3>,
        h3: ({ children }) => <h4 className="text-sm font-semibold">{children}</h4>,
        ul: ({ children }) => <ul className="list-disc pl-5 space-y-0.5">{children}</ul>,
        ol: ({ children }) => <ol className="list-decimal pl-5 space-y-0.5">{children}</ol>,
        li: ({ children }) => <li className="[&>p]:inline">{children}</li>,
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noreferrer" className="text-blue-400 underline underline-offset-2 break-all">{children}</a>
        ),
        strong: ({ children }) => <strong className="font-semibold text-white">{children}</strong>,
        blockquote: ({ children }) => <blockquote className="border-l-2 border-neutral-600 pl-3 text-neutral-300">{children}</blockquote>,
        hr: () => <hr className="border-neutral-700" />,
        code: ({ className, children }) => {
          const block = /language-/.test(className ?? '') || String(children).includes('\n')
          return block
            ? <code className="block overflow-x-auto rounded bg-neutral-950 px-2 py-1.5 font-mono text-[11px] leading-5 text-neutral-200">{children}</code>
            : <code className="rounded bg-neutral-950 px-1 py-0.5 font-mono text-[12px] text-amber-200">{children}</code>
        },
        pre: ({ children }) => <pre className="overflow-x-auto">{children}</pre>,
        table: ({ children }) => <div className="overflow-x-auto"><table className="min-w-full border-collapse text-xs">{children}</table></div>,
        th: ({ children }) => <th className="border border-neutral-700 bg-neutral-900 px-2 py-1 text-left font-semibold">{children}</th>,
        td: ({ children }) => <td className="border border-neutral-700 px-2 py-1 align-top">{children}</td>,
      }}>{text}</Markdown>
    </div>
  )
}
