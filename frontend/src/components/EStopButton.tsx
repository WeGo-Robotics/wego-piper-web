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

  // 주기적 heartbeat 전송.
  //
  // ⚠ 함께 **브라우저가 스스로 잰 간격**을 보낸다. estopd 가 2.1s 공백을 보고
  //   녹화를 죽인 적이 여러 번인데, 게이트웨이는 그 사이 0.3s 넘게 걸린 적이
  //   없었다. 늦은 곳이 여기인지 전송 구간인지 나누려면 양쪽 값이 다 필요하다.
  //   `hidden` 은 탭이 백그라운드였는지 — 브라우저는 안 보이는 탭의 타이머를
  //   늦춘다. 이 값들은 **진단용이라 실패해도 heartbeat 는 계속 나가야 한다.**
  useEffect(() => {
    let last = performance.now()
    const interval = setInterval(async () => {
      const now = performance.now()
      const gap = Math.round(now - last)
      last = now
      try {
        await api.post('/estop/heartbeat', { gap, hidden: document.hidden })
      } catch {
        // 연결 끊김 → watchdog이 타임아웃 처리
      }
    }, 500)
    return () => clearInterval(interval)
  }, [])

  return (
    <button
      onClick={triggerEstop}
      className="fixed bottom-4 right-4 z-50 w-14 h-14 rounded-full bg-red-600 hover:bg-red-500 active:bg-red-700 text-white font-bold text-[10px] shadow-lg shadow-red-900/50 transition-colors select-none pointer-events-auto"
      title="긴급 정지 (Escape)"
    >
      E-STOP
    </button>
  )
}
