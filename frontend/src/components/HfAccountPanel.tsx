import { useCallback, useEffect, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * HuggingFace 계정 — 로그인·토큰 교체.
 *
 * ## 왜 필요한가
 *
 * ⚠ **컨테이너 배포에는 로그인 경로가 아예 없었다.** 개발 머신은 호스트 토큰
 * (`~/.cache/huggingface/token`)을 보는데 컨테이너의 `HF_HOME` 은 `/data/hf` 라
 * 거기엔 토큰이 없다. 호스트에서 `huggingface-cli login` 을 해도 컨테이너는 그
 * 파일을 못 본다 — 이걸 모르면 "로그인했는데 왜 안 되지" 로 한참 헤맨다.
 * 그래서 **토큰이 놓이는 자리를 화면에 적어 준다.**
 *
 * ## 없으면 막히는 것
 *
 * 데이터셋 업로드, 학습 결과 푸시(`--policy.repo_id`), 비공개 저장소 내려받기.
 * ⚠ 두 번째가 가장 비싸다 — 업로드가 **학습 마지막**에 일어나므로 토큰이 없거나
 * 읽기 전용이면 몇 시간 돌린 뒤 그 단계에서 실패한다.
 */

type Account = {
  logged_in: boolean
  username: string
  fullname: string
  avatar_url: string
  orgs: string[]
  token_name: string
  token_role: string
  token_path: string
  error?: string
}

export default function HfAccountPanel() {
  const { notify, confirm } = useSystemMessage()
  const [acc, setAcc] = useState<Account | null>(null)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.get<Account>('/hub/whoami').then(setAcc).catch(() => setAcc(null))
  }, [])
  useEffect(load, [load])

  const login = async () => {
    if (!token.trim()) return
    setBusy(true)
    try {
      const r = await api.post<Account>('/hub/login', { token })
      setAcc(r)
      // ⚠ 입력란을 **즉시 비운다.** 화면에 남겨 두면 어깨너머로 읽히고,
      //   실수로 복사되어 다른 곳에 붙는다.
      setToken('')
      notify({ level: 'info', source: 'HuggingFace', text: `로그인: ${r.username}` })
    } catch (e) {
      notify({ level: 'error', source: 'HuggingFace',
               text: e instanceof Error ? e.message : '로그인 실패' })
    } finally { setBusy(false) }
  }

  const logout = async () => {
    if (!(await confirm('HuggingFace 토큰을 지웁니다. 업로드와 비공개 저장소 접근이 막힙니다.',
                          { danger: true })))
      return
    setBusy(true)
    try {
      await api.post('/hub/logout', {})
      load()
      notify({ level: 'info', source: 'HuggingFace', text: '토큰을 지웠습니다' })
    } catch (e) {
      notify({ level: 'error', source: 'HuggingFace',
               text: e instanceof Error ? e.message : '로그아웃 실패' })
    } finally { setBusy(false) }
  }

  const readOnly = acc?.logged_in && acc.token_role === 'read'

  return (
    <section className="space-y-3 rounded-lg border border-neutral-700 bg-neutral-800 p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">HuggingFace 계정</h2>
        {acc?.logged_in && (
          <button onClick={() => void logout()} disabled={busy}
            className="rounded border border-neutral-600 px-3 py-1 text-sm text-neutral-400 hover:border-red-500 hover:text-red-400 disabled:opacity-50">
            로그아웃
          </button>
        )}
      </div>

      {acc?.logged_in ? (
        <div className="flex items-center gap-3">
          {acc.avatar_url && <img src={acc.avatar_url} alt="" className="h-9 w-9 rounded-full" />}
          <div className="min-w-0 text-sm">
            <p className="font-medium">{acc.fullname || acc.username}</p>
            <p className="text-xs text-neutral-400">
              {acc.username}
              {acc.token_name && (
                <span className="ml-1.5 rounded bg-neutral-700 px-1 py-px text-[10px] text-neutral-300"
                      title="이 기계가 쓰는 HuggingFace 액세스 토큰의 이름 (HF 설정에서 붙인 것)">
                  토큰 {acc.token_name}
                </span>
              )}
            </p>
            {acc.orgs.length > 0 && (
              <p className="mt-0.5 text-xs text-neutral-500">조직: {acc.orgs.join(', ')}</p>
            )}
          </div>
          <span className={`ml-auto h-2 w-2 rounded-full ${readOnly ? 'bg-amber-400' : 'bg-green-400'}`} />
        </div>
      ) : (
        <p className="text-sm text-neutral-400">
          로그인되어 있지 않습니다 — 업로드와 비공개 저장소 접근이 막힙니다.
        </p>
      )}

      {readOnly && (
        // ⚠ 읽기 전용 토큰으로도 로그인은 성공한다. 그 상태로 학습을 걸면
        //   **몇 시간 뒤 마지막 업로드 단계에서** 실패한다. 지금 말해 준다.
        <p className="rounded border border-amber-600/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          이 토큰은 <strong>읽기 전용</strong>입니다. 데이터셋 업로드와 학습 결과 푸시가
          실패합니다 — 학습은 <strong>끝나고 나서</strong> 실패하므로 미리 바꾸는 편이 낫습니다.
        </p>
      )}

      <div className="space-y-1.5">
        <label className="block text-xs text-neutral-400">
          {acc?.logged_in ? '토큰 바꾸기' : '액세스 토큰'}
        </label>
        <div className="flex gap-2">
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void login() }}
            placeholder="hf_..." autoComplete="off" spellCheck={false}
            className="flex-1 rounded border border-neutral-700 bg-neutral-900 px-3 py-2 font-mono text-sm text-neutral-100 focus:border-blue-500 focus:outline-none" />
          <button onClick={() => void login()} disabled={busy || !token.trim()}
            className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50">
            {busy ? '확인 중…' : '저장'}
          </button>
        </div>
        <p className="text-xs text-neutral-500">
          huggingface.co → Settings → Access Tokens 에서 만듭니다.
          업로드하려면 <strong>write</strong> 권한이 필요합니다.
        </p>
        {acc?.token_path && (
          // ⚠ 자리를 밝힌다. 호스트에서 `huggingface-cli login` 을 해도 컨테이너는
          //   다른 자리를 보므로, 어디에 저장되는지가 보여야 한다.
          <p className="text-xs text-neutral-600">저장 위치: <code>{acc.token_path}</code></p>
        )}
      </div>
    </section>
  )
}
