import { useCallback, useEffect, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 임대 GPU(Vast.ai) 준비 — **API 키와 게이트웨이 SSH 키** (feature/vast-training.md §9-1).
 *
 * ## 왜 Piper Studio 가 키를 직접 만드나
 *
 * ⚠ **배포판 게이트웨이는 컨테이너라 `~/.ssh` 가 없다.** 사람의 키를 빌려 쓰면
 * 저장소에서 띄웠을 때만 되는 기능이 된다. 그래서 게이트웨이가 자기 키를 갖고,
 * `/data/config/ssh/` (마운트)에 둬서 재설치를 넘어 살아남는다.
 *
 * ⚠ **비밀키는 화면에 오지 않는다.** 서버가 주는 것은 공개키와 지문뿐이다.
 *
 * ## 여기는 "빌리기 전까지" 다
 *
 * 인스턴스·비용·고아는 나중 `/cloud` 페이지다. 한 번 맞춰 두는 것과 매일 보는
 * 것을 가른다 — 저장소(HF) 탭과 같은 자리에 같은 이유로 둔다.
 */

type Cred = {
  configured: boolean
  /** `env` = 배포가 심은 것(여기서 못 지운다) · `file` = 이 화면에서 저장한 것 */
  source: 'env' | 'file' | null
  /** ⚠ **끝 네 자리만.** 전체를 보여 주는 화면은 만들지 않는다 — 그 화면이 곧 유출 경로다 */
  tail: string
  env_wins: boolean
  credit?: number | null
  detail?: string
}

type Key = {
  exists: boolean
  public_key: string | null
  fingerprint: string | null
  path: string
  comment: string
}

export default function CloudPanel() {
  const { notify } = useSystemMessage()
  const [key, setKey] = useState<Key | null>(null)
  const [cred, setCred] = useState<Cred | null>(null)
  // ⚠ 입력칸은 **비우고 시작한다.** 저장된 키를 여기에 채워 넣으면, 그 화면을 여는
  //   누구나 읽게 된다 — 게이트웨이에는 인증이 없다.
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.get<Key>('/cloud/ssh-key').then(setKey).catch(() => setKey(null))
    api.get<Cred>('/cloud/credentials').then(setCred).catch(() => setCred(null))
  }, [])
  useEffect(load, [load])

  const act = async (label: string, run: () => Promise<unknown>) => {
    setBusy(true)
    try { await run() } catch (e) {
      notify({ level: 'error', source: 'Vast.ai',
               text: e instanceof Error ? e.message : `${label} 실패` })
    } finally { setBusy(false) }
  }

  const create = () => act('키 만들기', async () => {
    setKey(await api.post<Key>('/cloud/ssh-key', {}))
    notify({ level: 'info', source: 'Vast.ai', text: '게이트웨이 SSH 키를 만들었습니다' })
  })

  const register = () => act('등록', async () => {
    const r = await api.post<{ registered: boolean; detail: string | null }>(
      '/cloud/ssh-key/register', {})
    // ⚠ "등록했다" 가 아니라 **계정 목록에서 같은 지문을 찾았나**로 말한다.
    //   못 찾았는데 성공이라 하면, 증상은 인스턴스를 빌린 뒤 접속 실패로 늦게 나타난다.
    notify({ level: r.registered ? 'info' : 'error', source: 'Vast.ai',
             text: r.registered ? 'Vast 계정에서 같은 지문을 확인했습니다'
                                : (r.detail || '등록을 확인하지 못했습니다') })
  })

  const saveKey = () => act('API 키 저장', async () => {
    // ⚠ 서버가 **먼저 써 보고** 저장한다 — 틀린 키를 "설정됨" 으로 두면 제일 헷갈린다
    const r = await api.put<Cred>('/cloud/credentials', { api_key: apiKey.trim() })
    setCred(r)
    setApiKey('')          // ⚠ 저장했으면 입력칸에 남기지 않는다
    notify({ level: 'info', source: 'Vast.ai',
             text: `API 키를 저장했습니다 (끝 ${r.tail})`
                   + (r.credit != null ? ` · 크레딧 $${r.credit.toFixed(2)}` : '') })
  })

  const clearKey = () => act('API 키 지우기', async () => {
    const r = await api.delete<Cred>('/cloud/credentials')
    setCred(r)
    notify({ level: r.detail ? 'warn' : 'info', source: 'Vast.ai',
             text: r.detail || 'API 키를 지웠습니다' })
  })

  const copy = async () => {
    if (!key?.public_key) return
    try {
      await navigator.clipboard.writeText(key.public_key)
      notify({ level: 'info', source: 'Vast.ai', text: '공개키를 복사했습니다' })
    } catch {
      notify({ level: 'error', source: 'Vast.ai', text: '복사하지 못했습니다 — 직접 선택해 주세요' })
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-neutral-700 bg-neutral-800 p-4">
      {/* ── API 키 — SSH 키보다 **먼저**다. 이게 없으면 나머지가 다 무의미하다 ── */}
      <div className="space-y-2 rounded border border-neutral-700 bg-neutral-900/40 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-medium text-neutral-200">Vast.ai API 키</h3>
          {cred?.configured ? (
            <span className="rounded bg-emerald-900/50 px-2 py-0.5 text-xs text-emerald-200">
              설정됨 · 끝 {cred.tail || '****'}
              {cred.source === 'env' && ' · 배포 설정(.env)'}
            </span>
          ) : (
            <span className="rounded bg-amber-900/50 px-2 py-0.5 text-xs text-amber-200">없음</span>
          )}
          <a href="https://cloud.vast.ai/account/" target="_blank" rel="noreferrer"
            className="ml-auto text-xs text-blue-400 hover:underline">vast.ai → Account → API Keys</a>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* ⚠ `type=password` — 어깨너머로 읽히지 않게. 저장된 값을 되돌려 채우지도 않는다 */}
          <input type="password" value={apiKey} autoComplete="off"
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={cred?.configured ? '새 키로 바꾸려면 붙여넣기' : '키를 붙여넣기'}
            className="min-w-0 flex-1 rounded border border-neutral-600 bg-neutral-800 px-2 py-1 font-mono text-sm" />
          <button onClick={saveKey} disabled={busy || !apiKey.trim()}
            className="rounded bg-blue-600 px-3 py-1 text-sm text-white hover:bg-blue-500 disabled:opacity-40">
            저장
          </button>
          {cred?.source === 'file' && (
            <button onClick={clearKey} disabled={busy}
              className="rounded border border-neutral-600 px-3 py-1 text-sm text-neutral-300 hover:border-neutral-400 disabled:opacity-40">
              지우기
            </button>
          )}
        </div>

        <p className="text-xs text-neutral-500">
          {/* ⚠ 저장 전에 **써 보고** 저장한다는 사실을 말해 준다 — 그래야 실패가 이해된다 */}
          저장하기 전에 그 키로 계정을 한 번 조회해 봅니다. 키는 화면으로 다시 나오지
          않습니다(끝 네 자리만).
          {cred?.env_wins && <span className="text-amber-300"> · 배포 설정(.env)의 키가 우선합니다</span>}
        </p>
      </div>

      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">임대 GPU (Vast.ai)</h2>
        <a href="https://cloud.vast.ai/" target="_blank" rel="noreferrer"
          className="text-xs text-neutral-400 underline hover:text-neutral-200">콘솔 열기</a>
      </div>
      <p className="text-sm text-neutral-400">
        학습만 남의 GPU 에서 돌립니다. 빌린 인스턴스에 들어가려면 <strong>이 게이트웨이의
        공개키</strong>가 Vast 계정에 등록돼 있어야 합니다.
      </p>

      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <label className="block text-xs text-neutral-400">게이트웨이 SSH 키</label>
          {key?.exists && <span className="h-2 w-2 rounded-full bg-green-400" />}
        </div>

        {key?.exists ? (
          <>
            <textarea readOnly value={key.public_key ?? ''} rows={2}
              className="w-full resize-none rounded border border-neutral-700 bg-neutral-900 px-3 py-2 font-mono text-[11px] text-neutral-300" />
            <p className="text-xs text-neutral-500">
              지문 <code>{key.fingerprint}</code>
            </p>
            <div className="flex gap-2">
              <button onClick={() => void copy()}
                className="rounded border border-neutral-600 px-3 py-1.5 text-sm text-neutral-300 hover:border-neutral-400">
                공개키 복사
              </button>
              <button onClick={() => void register()} disabled={busy}
                className="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50">
                {busy ? '확인 중…' : 'Vast 계정에 등록'}
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="text-sm text-neutral-400">아직 키가 없습니다.</p>
            <button onClick={() => void create()} disabled={busy}
              className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50">
              {busy ? '만드는 중…' : '키 만들기'}
            </button>
          </>
        )}

        {key && (
          // ⚠ 자리를 밝힌다 — 컨테이너와 개발 머신이 다른 자리를 본다(HF 토큰과 같은 이유).
          <p className="text-xs text-neutral-600">저장 위치: <code>{key.path}</code> (비밀키는 화면에 오지 않습니다)</p>
        )}
      </div>
    </section>
  )
}
