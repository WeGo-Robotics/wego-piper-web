/**
 * SO-101 리더 → Piper 팔로워 텔레옵 (feature/so101d.md §5).
 *
 * 두 모드(관절 매칭 / 말단 POSE)를 사람이 고르고 실측으로 비교한다 — 어느
 * 쪽을 정식으로 열지는 §5c 의 숫자가 정한다. 두 팔은 영점도 시작 자세도
 * 달라 [정합](클러치) 방식이다: 물리는 순간 양쪽 자세가 앵커가 되어 점프가
 * 구조적으로 0, [해제] 후 리더를 편한 자세로 옮겨 재정합하면 작업 공간을
 * 이어 쓴다.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'

type RelayStatus = {
  running: boolean; leader: string | null; follower: string | null
  sent: number; stale: boolean; mode: string; blocked: string
  engaged: boolean; cross: boolean
}

export default function So101TeleopPanel({ arm, side, followers, onClose }: {
  arm: string
  side?: string
  followers: { iface: string; label: string; side?: string | null }[]
  onClose: () => void
}) {
  // 좌우가 지정돼 있으면 같은 쪽 팔로워를 기본으로 민다
  const preferred = followers.find((f) => side && f.side === side) ?? followers[0]
  const [follower, setFollower] = useState(preferred?.iface ?? '')
  const [mode, setMode] = useState<'joint' | 'pose'>('joint')
  const [st, setSt] = useState<RelayStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const refresh = useCallback(() => {
    api.get<RelayStatus>('/robots/relay/status').then(setSt).catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    const iv = setInterval(refresh, 1000)
    return () => clearInterval(iv)
  }, [refresh])

  const call = async (fn: () => Promise<unknown>, label: string) => {
    setBusy(true)
    setError('')
    try { await fn(); refresh() } catch (e) {
      setError(e instanceof Error ? e.message : `${label} 실패`)
    } finally { setBusy(false) }
  }

  const running = !!st?.running && st.leader === arm

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-lg border border-neutral-600 bg-neutral-800 p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">SO-101 텔레옵 — {arm}</h3>
          <button onClick={onClose} className="text-xs text-neutral-400 hover:text-white">닫기</button>
        </div>

        {!running && (
          <div className="space-y-3">
            <label className="block text-xs text-neutral-300">
              팔로워 (Piper)
              <select value={follower} onChange={(e) => setFollower(e.target.value)}
                className="mt-1 w-full text-xs rounded bg-neutral-700 border border-neutral-600 px-2 py-1.5">
                {followers.length === 0 && <option value="">연결된 Piper 가 없습니다</option>}
                {followers.map((f) => (
                  <option key={f.iface} value={f.iface}>
                    {f.label}{f.side ? ` (${f.side === 'left' ? '왼쪽' : '오른쪽'})` : ''}
                  </option>
                ))}
              </select>
            </label>
            <div className="flex gap-2 text-xs">
              {([['joint', '관절 매칭'], ['pose', '말단 POSE']] as const).map(([m, label]) => (
                <button key={m} onClick={() => setMode(m)}
                  className={`flex-1 px-2 py-1.5 rounded border ${mode === m
                    ? 'border-blue-500 bg-blue-500/20 text-blue-200'
                    : 'border-neutral-600 text-neutral-400 hover:text-white'}`}>
                  {label}
                </button>
              ))}
            </div>
            <p className="text-[11px] text-neutral-500 leading-relaxed">
              {mode === 'joint'
                ? '축이 겹치는 관절끼리 변화량을 잇습니다. Piper 전완 롤(J4)은 정합 시점 값을 유지 — 조그로 미리 세팅해 두세요.'
                : '리더 손끝의 자세 변화를 팔로워 손끝에 얹습니다 (IK). 특이점 근처에서는 잠시 멈출 수 있습니다.'}
            </p>
            <button onClick={() => call(() => api.post('/robots/relay/start', {
                leader: arm, follower, mode,
                leader_arm: 'so101', follower_arm: 'piper',
              }), '시작')}
              disabled={busy || !follower}
              className="w-full px-3 py-1.5 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-40">
              시작 (자동 정합)
            </button>
          </div>
        )}

        {running && st && (
          <div className="space-y-3">
            <div className="text-xs text-neutral-300 space-y-1">
              <p>{st.leader} → {st.follower} · {st.mode === 'joint' ? '관절 매칭' : '말단 POSE'}</p>
              <p className={st.engaged ? 'text-green-400' : 'text-amber-400'}>
                {st.engaged ? `● 정합됨 — 전송 ${st.sent}회` : '○ 해제됨 — 팔로워 정지'}
              </p>
              {st.stale && <p className="text-amber-400">리더 상태가 낡았습니다 — 발행 확인</p>}
              {st.blocked && <p className="text-amber-300">{st.blocked}</p>}
            </div>
            <div className="flex gap-2">
              {st.engaged ? (
                <button onClick={() => call(() => api.post('/robots/relay/disengage', {}), '해제')}
                  disabled={busy}
                  className="flex-1 px-3 py-1.5 text-xs rounded bg-amber-600 hover:bg-amber-500 text-white disabled:opacity-40">
                  해제 (리더 옮기기)
                </button>
              ) : (
                <button onClick={() => call(() => api.post('/robots/relay/engage', {}), '정합')}
                  disabled={busy}
                  className="flex-1 px-3 py-1.5 text-xs rounded bg-green-600 hover:bg-green-500 text-white disabled:opacity-40">
                  정합
                </button>
              )}
              <button onClick={() => call(() => api.post('/robots/relay/stop', {}), '정지')}
                disabled={busy}
                className="flex-1 px-3 py-1.5 text-xs rounded bg-red-600 hover:bg-red-500 text-white disabled:opacity-40">
                정지
              </button>
            </div>
          </div>
        )}

        {error && <p className="text-xs text-red-400 whitespace-pre-wrap">{error}</p>}
      </div>
    </div>
  )
}
