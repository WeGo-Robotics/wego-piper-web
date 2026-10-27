import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'
import { copyText } from '../services/clipboard'
import { useSystemMessage } from './SystemMessages'
import { useT, fmt } from '../i18n'

/**
 * 설정 → 에이전트 — 로컬 AI CLI(Claude Code·Codex·Gemini …)를 **이 게이트웨이에 붙이는 안내** (feature/agent-cli.md §4-7).
 *
 * ## 게이트웨이는 CLI 를 돌리지 않는다
 *
 * 여기는 채팅창이 아니다. 에이전트는 사람의 터미널에서 돌고, MCP 서버(`piper-mcp`)가 그 자식으로 떠서
 * 외부 API 로 이 게이트웨이에 말한다 — 에이전트가 죽으면 서버가 죽고 heartbeat 가 끊겨 로봇이 선다.
 * 웹에서 에이전트를 띄우는 것은 설계 §4-6 에서 보류했다(호스트 CLI 로그인·과금·코드 수정 권한).
 *
 * 그래서 이 탭이 하는 일은 넷이다: 전제(외부 API 토큰)가 켜져 있나, 붙었나(마지막 외부 호출),
 * **어떤 CLI 가 깔려 있나**(게이트웨이 기계 + unitd 가 본 호스트), 고른 CLI 에 맞는 붙이는 명령.
 * 고르는 것은 **안내를 바꾸는 것**이지 실행 대상을 정하는 것이 아니다 — 선택은 게이트웨이가 파일로 기억한다.
 *
 * ## 토큰 값은 안 보여 준다
 *
 * 켜짐/꺼짐만 온다. 명령에는 자리표시자(<TOKEN>)를 둔다 — 값은 게이트웨이 기계의 `backend/.env` 에서 사람이 옮긴다.
 * 주소는 **이 브라우저가 쓰는 주소**(`window.location.origin`)다 — 에이전트가 도는 기계에서 닿는 주소가 바로 그것이다.
 */

type Cli = {
  id: string; name: string; vendor: string; bin: string
  installed: boolean; path: string | null; version: string | null
  where: 'gateway' | 'host' | null
}
type Recipe = {
  name: string; vendor: string
  mcp: { add: string | null; config_path: string; config: string } | null
  ask: string
  notes: string[]
}
type Link = {
  ext_enabled: boolean
  last_ext_call: { at: number; method: string; path: string } | null
  mcp: { tools: string[]; resources: string[]; episode_template: string } | null
  install: string
  docs: string
  clis: Cli[]
  preferred: string
  preferred_saved: boolean
  recipes: Record<string, Recipe>
  gateway_in_container: boolean
  host_supported: boolean
}

export default function AgentLinkPanel() {
  const t = useT()
  const { notify } = useSystemMessage()
  const [link, setLink] = useState<Link | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    // --version 프로세스와 unitd 왕복이 들어 평소 GET 보다 느리다
    api.get<Link>('/system/agent-link', { timeoutMs: 60_000 })
      .then((r) => { setLink(r); setErr('') })
      .catch((e) => setErr(e instanceof Error ? e.message : t('읽지 못했습니다')))
  }, [t])
  useEffect(load, [load])

  const choose = async (id: string) => {
    if (!link || link.preferred === id) return
    const prev = link
    setLink({ ...link, preferred: id, preferred_saved: true })
    setBusy(true)
    try {
      await api.put('/system/agent-link', { preferred: id })
    } catch (e) {
      setLink(prev)
      notify({ level: 'error', source: t('에이전트'), text: e instanceof Error ? e.message : t('저장 실패') })
    } finally { setBusy(false) }
  }

  const origin = window.location.origin
  const question = t('추론이 왜 죽었나')
  const fill = (s: string) => s.replaceAll('{origin}', origin).replaceAll('{question}', question)

  const copy = async (text: string, what: string) => {
    try {
      await copyText(text)
      notify({ level: 'info', source: t('에이전트'), text: t('{what}을(를) 복사했습니다', { what }) })
    } catch {
      notify({ level: 'error', source: t('에이전트'), text: t('복사하지 못했습니다 — 직접 선택해 주세요') })
    }
  }

  const Block = ({ title, text, note }: { title: string; text: string; note?: string }) => (
    <div className="rounded-lg border border-neutral-700 bg-neutral-900 p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{title}</span>
        <button onClick={() => copy(text, title)}
          className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600">{t('복사')}</button>
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-neutral-200">{text}</pre>
      {note && <p className="text-[11px] text-neutral-500">{note}</p>}
    </div>
  )

  const last = link?.last_ext_call
  const installed = link?.clis.filter((c) => c.installed) ?? []
  const rec = link ? link.recipes[link.preferred] : null

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5 space-y-4">
        <div>
          <h2 className="text-lg font-semibold">{t('에이전트 연결')}</h2>
          <p className="text-xs text-neutral-400 mt-1">
            {t('로컬 AI CLI(Claude Code 등)는 사람의 터미널에서 돌고, MCP 서버가 외부 API 로 이 게이트웨이에 붙습니다. 게이트웨이가 CLI 를 띄우지는 않습니다 — 에이전트가 죽으면 heartbeat 가 끊겨 로봇이 서는 것이 설계입니다.')}
          </p>
        </div>

        {err && <p className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">{err}</p>}

        {/* 전제 — 외부 API 토큰. 꺼져 있으면 MCP 도 503 이라 이 아래 명령이 전부 헛것이다 */}
        {link && !link.ext_enabled && (
          <div className="rounded-lg border border-amber-600/50 bg-amber-950/30 px-4 py-3 text-xs text-amber-200 space-y-1">
            <div className="font-semibold">{t('⚠ 외부 API 가 꺼져 있습니다 — 에이전트가 붙을 수 없습니다')}</div>
            <div>{t('게이트웨이 기계의 backend/.env 에 PIPER_API_TOKEN=<긴 무작위 문자열> 을 넣고, 서비스 탭에서 게이트웨이를 재시작하세요. 켜는 것이 명시적 행위입니다.')}</div>
          </div>
        )}
        {link && link.ext_enabled && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            <span className="text-emerald-300">{t('외부 API 켜짐')}</span>
            <span className="text-neutral-400">
              {last
                ? t('마지막 외부 호출 {when} · {method} {path}', {
                    when: fmt.time(last.at * 1000, { hour12: false }), method: last.method, path: last.path })
                : t('게이트웨이가 뜬 뒤 외부 호출은 아직 없습니다')}
            </span>
          </div>
        )}
      </div>

      {link && (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5 space-y-3">
          <div>
            <h2 className="text-lg font-semibold">{t('설치된 AI CLI')}</h2>
            <p className="text-xs text-neutral-400 mt-1">
              {t('깔린 CLI {n}개 — 고른 것에 맞춰 아래 안내가 바뀝니다. 게이트웨이가 실행하지는 않습니다.', { n: installed.length })}
              {' '}
              {link.host_supported
                ? t('게이트웨이 기계와 호스트(unitd) 양쪽을 봤습니다.')
                : link.gateway_in_container
                  ? t('컨테이너 게이트웨이는 호스트의 CLI 를 못 봅니다 — 호스트의 unitd 가 새 버전이면 대신 알려 줍니다.')
                  : t('이 게이트웨이 기계에서 봤습니다.')}
            </p>
          </div>
          {installed.length === 0 && (
            <p className="rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-xs text-neutral-400">
              {t('AI CLI 를 찾지 못했습니다 — 에이전트가 다른 기계에서 돈다면 정상입니다. 그 기계에서 아래 안내를 쓰세요.')}
            </p>
          )}
          <div className="grid gap-1.5 sm:grid-cols-2">
            {link.clis.map((c) => (
              <label key={c.id}
                className={`flex items-start gap-3 rounded-lg border px-3 py-2 cursor-pointer transition-colors ${
                  link.preferred === c.id ? 'border-blue-500 bg-neutral-900' : 'border-neutral-700 bg-neutral-900/60 hover:border-neutral-500'}`}>
                <input type="radio" name="agent-cli" className="mt-1 accent-blue-500" disabled={busy}
                       checked={link.preferred === c.id} onChange={() => choose(c.id)} />
                <span className="flex-1 min-w-0">
                  <span className="text-sm">{c.name} <span className="text-xs text-neutral-500">{c.vendor} · {c.bin}</span></span>
                  <span className="block text-xs mt-0.5 truncate">
                    {c.installed ? (
                      <span className="text-emerald-300" title={c.path ?? undefined}>
                        {t('설치됨')}{c.version ? ` ${c.version}` : ''} · {c.where === 'host' ? t('호스트') : t('이 기계')}
                      </span>
                    ) : <span className="text-neutral-500">{t('없음')}</span>}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </div>
      )}

      {link && rec && (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5 space-y-3">
          <div>
            <h2 className="text-lg font-semibold">{t('붙이는 법 — {name}', { name: rec.name })}</h2>
            <p className="text-xs text-neutral-400 mt-1">
              {t('에이전트가 도는 기계에서 합니다. 주소는 이 브라우저가 쓰는 주소이고, 토큰 값은 여기 보이지 않습니다 — backend/.env 에서 옮기세요.')}
            </p>
          </div>
          <Block title={t('1. 설치')} text={link.install}
                 note={t('저장소 체크아웃이 있으면 pip install -e "plugin/[mcp]" 도 같습니다.')} />
          {rec.mcp ? (<>
            {rec.mcp.add && <Block title={t('2. MCP 등록')} text={fill(rec.mcp.add)} />}
            <Block title={rec.mcp.add ? t('설정 파일로 둘 때 — {path}', { path: rec.mcp.config_path })
                                      : t('2. 설정 파일 — {path}', { path: rec.mcp.config_path })}
                   text={fill(rec.mcp.config)} />
          </>) : (
            <div className="rounded-lg border border-amber-600/50 bg-amber-950/30 px-4 py-3 text-xs text-amber-200">
              {t('이 CLI 는 MCP 를 지원하지 않습니다 — 꾸러미와 에피소드 묶음을 파일로 넘깁니다.')}
            </div>
          )}
          <Block title={t('3. 꾸러미를 바로 묻기')} text={fill(rec.ask)}
                 note={t('알림함 🔔 과 로그 화면의 [에이전트용 컨텍스트 복사] 와 같은 본문입니다.')} />
          {rec.notes.length > 0 && (
            <ul className="list-disc pl-5 text-xs text-neutral-400 space-y-0.5">
              {rec.notes.map((n) => <li key={n}>{n}</li>)}
            </ul>
          )}
        </div>
      )}

      {link?.mcp && (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5 space-y-3">
          <div>
            <h2 className="text-lg font-semibold">{t('MCP 가 주는 것')}</h2>
            <p className="text-xs text-neutral-400 mt-1">
              {t('미션 수준뿐입니다 — 장치 준비는 사람 몫이고 전제가 안 되면 409 가 옵니다. 미션 시작은 dry_run 이 기본이며, heartbeat 는 서버가 자기가 시작한 미션에만 보냅니다.')}
            </p>
          </div>
          <div>
            <div className="text-xs text-neutral-400 mb-1">{t('도구 {n}개', { n: link.mcp.tools.length })}</div>
            <div className="flex flex-wrap gap-1">
              {link.mcp.tools.map((n) => (
                <span key={n} className="rounded bg-neutral-900 border border-neutral-700 px-2 py-0.5 font-mono text-[11px] text-neutral-200">{n}</span>
              ))}
            </div>
          </div>
          <div>
            <div className="text-xs text-neutral-400 mb-1">{t('리소스')}</div>
            <div className="flex flex-wrap gap-1">
              {[...link.mcp.resources, link.mcp.episode_template].map((u) => (
                <span key={u} className="rounded bg-neutral-900 border border-neutral-700 px-2 py-0.5 font-mono text-[11px] text-neutral-300">{u}</span>
              ))}
            </div>
          </div>
          <p className="text-[11px] text-neutral-500">
            {t('설계와 스킬(플러그인 만들기 · 에피소드 보기)은 저장소의 {docs} 에 있습니다.', { docs: link.docs })}
          </p>
        </div>
      )}
    </div>
  )
}
