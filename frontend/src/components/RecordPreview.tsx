import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 녹화 중 카메라 미리보기. 200ms 프레임 폴링을 이 컴포넌트 내부 상태로 가둬,
 * 부모(RecordingPage)와 무거운 로그 뷰어(xterm)가 200ms마다 리렌더되지 않게 한다.
 */
export default function RecordPreview() {
  const [cams, setCams] = useState<string[]>([])

  // 미리보기 가능한 카메라 목록 폴링 (1s)
  useEffect(() => {
    const fetchNames = () => api.get<{ cameras: string[] }>('/recording/preview')
      .then(r => setCams(r.cameras)).catch(() => {})
    fetchNames()
    const iv = setInterval(fetchNames, 1000)
    return () => clearInterval(iv)
  }, [])

  // ⚠ 예전에는 200ms 마다 `?t=` 를 바꿔 새로 걸었다 = 카메라당 초당 5장.
  //   서버가 느려서 끊긴 게 아니라(한 장에 1.2ms) **초당 5장이 설계값이었다.**
  //   지금은 카메라당 연결 하나로 계속 받는다 — 프레임레이트가 오르고,
  //   요청 수가 줄어 E-stop heartbeat 를 굶기던 연결 경합도 같이 준다.

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
      <h3 className="text-sm font-semibold mb-2">카메라 미리보기</h3>
      {cams.length === 0 ? (
        <p className="text-xs text-neutral-500">프레임 대기 중… (녹화 루프 시작 후 표시됩니다)</p>
      ) : (
        // ⚠ 2열 격자였다. 카메라가 셋이면 둘째 줄에 하나만 남고, 넷이면 아래 두 장이
        //   접혀 **동시에 볼 수 없었다** — 녹화 중 미리보기의 요점이 그건데.
        //   한 줄로 세우고, 좁아지면 늘어놓은 채로 옆으로 민다.
        <div className="flex gap-2 overflow-x-auto pb-1">
          {cams.map(name => (
            <div key={name} className="space-y-1 flex-1 min-w-[7rem]">
              <div className="text-[10px] text-neutral-400 font-mono truncate">{name}</div>
              <img
                src={`/api/recording/preview-stream/${encodeURIComponent(name)}`}
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
