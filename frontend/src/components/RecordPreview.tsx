import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 녹화 중 카메라 미리보기. 200ms 프레임 폴링을 이 컴포넌트 내부 상태로 가둬,
 * 부모(RecordingPage)와 무거운 로그 뷰어(xterm)가 200ms마다 리렌더되지 않게 한다.
 */
export default function RecordPreview() {
  const [cams, setCams] = useState<string[]>([])
  const [ts, setTs] = useState(0)

  // 미리보기 가능한 카메라 목록 폴링 (1s)
  useEffect(() => {
    const fetchNames = () => api.get<{ cameras: string[] }>('/recording/preview')
      .then(r => setCams(r.cameras)).catch(() => {})
    fetchNames()
    const iv = setInterval(fetchNames, 1000)
    return () => clearInterval(iv)
  }, [])

  // 프레임 갱신 (200ms) — 카메라가 있을 때만
  useEffect(() => {
    if (cams.length === 0) return
    const iv = setInterval(() => setTs(Date.now()), 200)
    return () => clearInterval(iv)
  }, [cams.length])

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
      <h3 className="text-sm font-semibold mb-2">카메라 미리보기</h3>
      {cams.length === 0 ? (
        <p className="text-xs text-neutral-500">프레임 대기 중… (녹화 루프 시작 후 표시됩니다)</p>
      ) : (
        <div className={`grid gap-2 ${cams.length > 1 ? 'grid-cols-2' : 'grid-cols-1'}`}>
          {cams.map(name => (
            <div key={name} className="space-y-1">
              <div className="text-[10px] text-neutral-400 font-mono">{name}</div>
              <img
                src={`/api/recording/preview/${encodeURIComponent(name)}?t=${ts}`}
                alt={name}
                className="w-full aspect-[4/3] object-cover bg-neutral-900 rounded"
              />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
