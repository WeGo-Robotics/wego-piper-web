import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'
import type { StreamInfo } from '../types/camera'

/**
 * 등록된 카메라의 스트림 디렉토리 — 수집·추론 화면의 카메라 선택기가 쓴다.
 *
 * ⚠ **폴링하지 않는다.** 이 화면들은 수집·추론 중에도 떠 있고, 요청이 쌓이면 정지 버튼의
 *   요청까지 못 나간다(api.ts 머리 주석). 마운트·탭 복귀·연결 직후에만 다시 읽는다.
 */
export function useCameraStreams() {
  const [streams, setStreams] = useState<StreamInfo[]>([])
  const [loaded, setLoaded] = useState(false)
  const [connecting, setConnecting] = useState<string[]>([])
  const alive = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const r = await api.get<{ streams?: StreamInfo[] }>('/cameras/streams')
      if (alive.current) setStreams(r.streams ?? [])
    } catch { /* 목록을 못 받아도 화면은 계속 쓴다 — 값은 그대로 보인다 */ }
    finally { if (alive.current) setLoaded(true) }
  }, [])

  useEffect(() => {
    alive.current = true
    refresh()
    // 다른 페이지(카메라)에서 연결·해제했을 수 있다 — 탭으로 돌아오면 다시 읽는다.
    const onVisible = () => { if (document.visibilityState === 'visible') refresh() }
    document.addEventListener('visibilitychange', onVisible)
    return () => { alive.current = false; document.removeEventListener('visibilitychange', onVisible) }
  }, [refresh])

  /** 장치 id 들을 연결한다. 실패한 것은 던지지 않고 사유와 함께 돌려준다 — 한 대가 안 열려도 나머지는 연결한다. */
  const connect = useCallback(async (deviceIds: string[]) => {
    setConnecting((prev) => [...prev, ...deviceIds])
    const failed: { id: string; reason: string }[] = []
    try {
      // 순서대로 — RealSense 두 대를 동시에 열면 USB 대역폭을 나눠 초기화가 늘어진다
      for (const id of deviceIds) {
        try { await api.post('/cameras/connect', { id }) }
        catch (e) { failed.push({ id, reason: e instanceof Error ? e.message : String(e) }) }
      }
    } finally {
      if (alive.current) setConnecting((prev) => prev.filter((x) => !deviceIds.includes(x)))
      await refresh()
    }
    return failed
  }, [refresh])

  return { streams, loaded, refresh, connect, connecting }
}
