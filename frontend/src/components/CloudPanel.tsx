import { useCallback, useEffect, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 임대 GPU(Vast.ai) 준비 — 게이트웨이 키 (feature/vast-training.md §9-1).
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
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.get<Key>('/cloud/ssh-key').then(setKey).catch(() => setKey(null))
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
