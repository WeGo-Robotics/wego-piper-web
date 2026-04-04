import { useEffect, useCallback } from 'react'
import { api } from '../services/api'

export default function EStopButton() {
  const triggerEstop = useCallback(async () => {
    try {
      await api.post('/estop/trigger')
    } catch {
      // 네트워크 에러여도 최선을 다해 시도
    }
  }, [])

  // Escape 키로 E-stop 트리거
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        triggerEstop()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [triggerEstop])

  // 주기적 heartbeat 전송
  useEffect(() => {
    const interval = setInterval(async () => {
      try {
        await api.post('/estop/heartbeat')
      } catch {
        // 연결 끊김 → watchdog이 타임아웃 처리
      }
    }, 500)
    return () => clearInterval(interval)
  }, [])

  return (
    <button
      onClick={triggerEstop}
      className="fixed bottom-6 right-6 z-50 w-20 h-20 rounded-full bg-red-600 hover:bg-red-500 active:bg-red-700 text-white font-bold text-sm shadow-lg shadow-red-900/50 transition-colors select-none"
      title="긴급 정지 (Escape)"
    >
      E-STOP
    </button>
  )
}
