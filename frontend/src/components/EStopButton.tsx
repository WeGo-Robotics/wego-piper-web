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
    let rtt = 0        // 직전 요청이 **오가는 데** 걸린 시간
    let seq = 0        // 보낸 순번 — 서버가 빠진 번호로 유실을 안다
    const interval = setInterval(async () => {
      const now = performance.now()
      const gap = Math.round(now - last)
      last = now
      const sent = now
      seq += 1
      try {
        // ⚠ `rtt` 는 직전 값을 보낸다 — 이번 요청의 왕복 시간은 이번 요청을
        //   보낸 뒤에야 알 수 있다. 한 tick 늦지만 그래도 원인이 갈린다:
        //   타이머는 정시(gap≈500)인데 서버가 본 간격이 벌어졌다면, 요청이
        //   **브라우저 안에서 대기**했다는 뜻이다. HTTP/1.1 은 오리진당 연결이
        //   6개뿐이고, 수집 화면은 카메라 프리뷰를 200ms 마다 긁는다.
        // ⚠ `seq` 가 판별의 핵심이다. 타이머는 정시(gap≈500)인데 서버가 본
        //   **도착** 간격은 2초씩 벌어졌다. 번호가 이어져 있으면 요청이 늦게
        //   도착한 것이고, 번호가 건너뛰면 그 사이 요청은 **아예 못 갔다**.
        //   둘은 고치는 곳이 다르다.
        await api.post('/estop/heartbeat', { gap, hidden: document.hidden, rtt, seq })
      } catch {
        // 연결 끊김 → watchdog이 타임아웃 처리
      } finally {
        rtt = Math.round(performance.now() - sent)
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
