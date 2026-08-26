import { useEffect, useCallback, useRef } from 'react'
import { api } from '../services/api'
import { useWebSocket } from '../hooks/useWebSocket'

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

  const { connected: wsUp, send: wsSend } = useWebSocket('/ws')
  // ⚠ 타이머는 한 번만 걸린다. `wsUp` 을 콜백에 그대로 가두면 **끊긴 뒤에도
  //   계속 WS 로 보내는** 상태가 되므로 최신 값을 ref 로 본다.
  const wsRef = useRef({ up: wsUp, send: wsSend })
  wsRef.current = { up: wsUp, send: wsSend }

  // ── E-stop heartbeat ──
  //
  // ⚠ **WS 로 보낸다.** 예전에는 `POST /api/estop/heartbeat` 였는데, HTTP/1.1 은
  //   오리진당 연결이 6개뿐이라 카메라 프리뷰와 같은 줄에 서 있었다. 실측에서
  //   타이머는 정시(500ms)에 만들었고 유실도 없었는데 서버가 본 도착 간격이
  //   2.35초였다 — 만들어진 뒤 나가기 전에 대기한 것이고, 그 사이 E-stop 이
  //   돌아 녹화가 죽었다. WS 는 그 6개와 **다른 풀**을 쓴다.
  //
  //   HTTP 경로는 남긴다: WS 가 끊겼을 때 heartbeat 까지 같이 멈추면 "브라우저가
  //   사라졌다" 와 구분이 안 된다.
  //
  // 같이 보내는 값들은 진단용이다 — 전부 선택이고, 없어도 heartbeat 는 유효하다.
  //   gap    타이머가 실제로 몇 ms 만에 다시 불렸나
  //   seq    보낸 순번. 빠지면 그 요청은 아예 못 간 것이다
  //   rtt    직전 요청의 왕복 (`rttSeq` 로 어느 것인지 밝힌다)
  //   via    어느 경로로 갔나 — WS 로 옮긴 뒤에도 갭이 나면 위 진단이 틀린 것이다
  useEffect(() => {
    let last = performance.now()
    let seq = 0
    let done = { seq: 0, ms: 0 }
    const interval = setInterval(async () => {
      const now = performance.now()
      const gap = Math.round(now - last)
      last = now
      const sent = now
      const mySeq = ++seq
      const info = { gap, hidden: document.hidden, seq: mySeq,
                     rtt: done.ms, rttSeq: done.seq }
      try {
        if (wsRef.current.up) {
          wsRef.current.send({ type: 'heartbeat', ...info, via: 'ws' })
        } else {
          await api.post('/estop/heartbeat', { ...info, via: 'http' })
        }
      } catch {
        // 연결 끊김 → watchdog 이 타임아웃 처리
      } finally {
        // 늦게 끝난 요청이 나중 요청의 기록을 덮지 않게 번호로 비교한다
        const ms = Math.round(performance.now() - sent)
        if (mySeq > done.seq) done = { seq: mySeq, ms }
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
