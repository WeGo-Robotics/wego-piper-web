import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'
import { useSystemMessage } from './SystemMessages'

/**
 * 게이트웨이 비밀번호 — **켜고 끄는 유일한 자리.**
 *
 * ## ⚠ 꺼져 있으면 조용히 두지 않는다
 *
 * 안 켜는 것은 선택일 수 있지만, **모르고 열려 있는 것**은 아니다. 같은 LAN 에서
 * `:8000` 에 닿는 누구나 데이터셋을 지우고 팔을 움직이고, Vast 키가 저장돼 있으면
 * GPU 를 빌려 돈을 쓴다. 그래서 꺼진 상태를 경고로 그린다.
 *
 * ## ⚠ 끄는 데도 지금 비밀번호를 받는다
 *
 * 로그인만으로 끄게 두면, 자리를 비운 사이 열린 화면 앞에 앉은 사람이 문을 열어 둘 수
 * 있다. 바꾸는 것도 같은 이유로 지금 비밀번호를 받는다.
 */
type Status = { enabled: boolean; authenticated: boolean; broken: boolean; min_length: number }

export default function AccessPanel() {
  const { notify } = useSystemMessage()
  const [st, setSt] = useState<Status | null>(null)
  const [cur, setCur] = useState('')
  const [next, setNext] = useState('')
  const [again, setAgain] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.get<Status>('/auth/status').then(setSt).catch(() => setSt(null))
  }, [])
  useEffect(load, [load])

  const clear = () => { setCur(''); setNext(''); setAgain('') }
  const fail = (e: unknown, what: string) =>
    notify({ level: 'error', source: '보안',
             text: e instanceof Error ? e.message : `${what} 실패` })

  const save = async () => {
    if (next !== again) {
      notify({ level: 'error', source: '보안', text: '새 비밀번호가 서로 다릅니다' })
      return
    }
    setBusy(true)
    try {
      await api.put('/auth/password', { current: cur, new: next })
      clear()
      load()
      notify({ level: 'info', source: '보안',
               text: '비밀번호를 저장했습니다 — 다른 기기의 로그인은 모두 끊겼습니다' })
    } catch (e) { fail(e, '저장') } finally { setBusy(false) }
  }

  const turnOff = async () => {
    setBusy(true)
    try {
      await api.delete('/auth/password', { body: { current: cur } })
      clear()
      load()
      notify({ level: 'warn', source: '보안',
               text: '인증을 껐습니다 — 같은 네트워크의 누구나 들어올 수 있습니다' })
    } catch (e) { fail(e, '해제') } finally { setBusy(false) }
  }

  const logout = async () => {
    try {
      await api.post('/auth/logout')
      window.location.reload()
    } catch (e) { fail(e, '로그아웃') }
  }

  if (!st) return <p className="text-sm text-neutral-500">상태를 읽는 중…</p>

  const field = (label: string, v: string, set: (s: string) => void, auto: string) => (
    <label className="flex items-center gap-3 text-sm">
      <span className="w-24 shrink-0 text-neutral-400">{label}</span>
      <input type="password" value={v} onChange={(e) => set(e.target.value)} autoComplete={auto}
        className="w-56 rounded border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 focus:border-blue-500 focus:outline-none" />
    </label>
  )

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h3 className="text-base font-semibold text-neutral-100">로그인</h3>
        <span className={`rounded px-2 py-0.5 text-xs ${st.enabled
          ? 'bg-green-900/50 text-green-300' : 'bg-amber-900/50 text-amber-200'}`}>
          {st.enabled ? '켜짐' : '꺼짐'}
        </span>
        {st.enabled && (
          <button onClick={() => void logout()}
            className="ml-auto rounded border border-neutral-600 px-3 py-1 text-xs text-neutral-300 hover:border-neutral-400">
            로그아웃
          </button>
        )}
      </div>

      {!st.enabled && (
        <p className="rounded border border-amber-600/40 bg-amber-950/30 p-2 text-xs text-amber-200/90">
          지금은 <strong>같은 네트워크의 누구나</strong> 이 화면에 들어와 데이터셋을 지우고
          팔을 움직일 수 있습니다. 클라우드 API 키를 저장해 뒀다면 <strong>GPU 를 빌려 돈을
          쓸 수도</strong> 있습니다.
        </p>
      )}

      <p className="text-xs text-neutral-500">
        ⚠ 비상정지와 heartbeat 는 로그인과 무관하게 항상 열려 있습니다 — 멈추는 길을 막으면
        돌고 있던 추론이 강제 종료됩니다.
      </p>

      <div className="space-y-2">
        {st.enabled && field('지금 비밀번호', cur, setCur, 'current-password')}
        {field('새 비밀번호', next, setNext, 'new-password')}
        {field('한 번 더', again, setAgain, 'new-password')}
        <p className="pl-27 text-xs text-neutral-500">{st.min_length}자 이상</p>
      </div>

      <div className="flex gap-2">
        <button onClick={() => void save()} disabled={busy || next.length < st.min_length}
          className="rounded bg-blue-700 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-50">
          {st.enabled ? '비밀번호 바꾸기' : '로그인 켜기'}
        </button>
        {st.enabled && (
          <button onClick={() => void turnOff()} disabled={busy || !cur}
            className="rounded border border-red-600/60 px-4 py-1.5 text-sm text-red-300 hover:border-red-400 disabled:opacity-40">
            인증 끄기
          </button>
        )}
      </div>
    </div>
  )
}
