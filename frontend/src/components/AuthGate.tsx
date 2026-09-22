import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../services/api'

/**
 * 로그인 관문 — **비밀번호를 정한 게이트웨이에서만 뜬다.**
 *
 * ## ⚠ 게이트웨이가 죽었을 때는 잠그지 않는다
 *
 * 상태를 못 물어봤다고 로그인 화면을 띄우면, 비밀번호를 정확히 쳐도 안 되는 **막다른
 * 길**이 된다. 게이트웨이가 살아 있고 인증이 켜져 있으면 `/auth/status` 는 반드시
 * 답한다(관문이 비켜 가는 경로다) — 그러니 못 물어본 것은 "꺼짐" 이 아니라 "서버가
 * 없음" 이고, 그건 각 화면이 평소처럼 오류로 말하는 편이 낫다.
 *
 * ## ⚠ E-stop 은 이 뒤에서도 돈다
 *
 * heartbeat 와 정지는 관문이 열어 둔 경로다. 로그인 화면이 떠 있는 동안에도 멈추는
 * 길은 막히지 않는다 — 인증을 붙인 대가로 돌던 추론을 끊으면 안 된다.
 */
type Status = { enabled: boolean; authenticated: boolean; broken: boolean; min_length: number }

const OPEN: Status = { enabled: false, authenticated: true, broken: false, min_length: 4 }

export default function AuthGate({ children }: { children: ReactNode }) {
  const [st, setSt] = useState<Status | null>(null)
  const [pw, setPw] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const check = useCallback(() => {
    api.get<Status>('/auth/status')
      .then(setSt)
      .catch(() => setSt(OPEN))   // 서버가 없다 — 위 머리말
  }, [])

  useEffect(check, [check])

  // 세션이 끊기면(만료·비밀번호 변경) 어느 화면에서든 여기로 돌아온다.
  useEffect(() => {
    const denied = () => setSt((s) => (s ? { ...s, authenticated: false } : s))
    window.addEventListener('piper:unauthorized', denied)
    return () => window.removeEventListener('piper:unauthorized', denied)
  }, [])

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      await api.post('/auth/login', { password: pw })
      setPw('')
      check()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '로그인하지 못했습니다')
    } finally {
      setBusy(false)
    }
  }

  if (st === null) {
    return (
      <div className="min-h-screen bg-neutral-900 flex items-center justify-center text-sm text-neutral-500">
        확인 중…
      </div>
    )
  }
  if (!st.enabled || st.authenticated) return <>{children}</>

  return (
    <div className="min-h-screen bg-neutral-900 flex items-center justify-center p-4">
      <form onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-lg border border-neutral-700 bg-neutral-800 p-6">
        <div>
          <h1 className="text-lg font-semibold text-neutral-100">Piper Studio</h1>
          <p className="mt-1 text-sm text-neutral-400">계속하려면 비밀번호를 입력하세요.</p>
        </div>

        {st.broken ? (
          /* ⚠ 여기서 비밀번호를 쳐 봐야 소용없다 — 왜 안 되는지와 푸는 법을 말한다. */
          <p className="rounded border border-red-500/50 bg-red-950/40 p-2 text-xs text-red-200">
            인증 설정 파일이 깨져서 <strong>아무도 들어올 수 없습니다.</strong>{' '}
            호스트에서 <code className="text-red-100">~/.config/piper-web/auth.json</code> 를
            지우면 인증이 꺼진 상태로 돌아갑니다.
          </p>
        ) : (
          <>
            <input type="password" value={pw} onChange={(e) => setPw(e.target.value)}
              autoFocus autoComplete="current-password" placeholder="비밀번호"
              className="w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 focus:border-blue-500 focus:outline-none" />
            {err && <p className="text-xs text-red-400">{err}</p>}
            <button type="submit" disabled={busy || !pw}
              className="w-full rounded bg-blue-700 px-4 py-2 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-50">
              {busy ? '확인 중…' : '들어가기'}
            </button>
          </>
        )}
      </form>
    </div>
  )
}
