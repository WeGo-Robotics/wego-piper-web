import { useEffect, useRef, useCallback, useState } from 'react'
import type { WsMessage } from '../types/ws'

export type { WsMessage } from '../types/ws'

type UseWebSocketOptions = {
  onMessage?: (msg: WsMessage) => void
  reconnectInterval?: number
}

export function useWebSocket(url: string, options: UseWebSocketOptions = {}) {
  const { onMessage, reconnectInterval = 3000 } = options
  const wsRef = useRef<WebSocket | null>(null)
  const [connected, setConnected] = useState(false)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage
  const mountedRef = useRef(true)

  const connect = useCallback(() => {
    // 이미 연결됐거나 연결 중이면 새 소켓을 만들지 않음.
    // (OPEN만 체크하면 CONNECTING 상태에서 중복 연결이 생겨 로그가 두 번 들어온다)
    const existing = wsRef.current
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) {
      return
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}${url}`
    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      if (mountedRef.current) setConnected(true)
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as WsMessage
        onMessageRef.current?.(msg)
      } catch {
        // ignore non-JSON messages
      }
    }

    ws.onclose = () => {
      setConnected(false)
      // 이 소켓이 여전히 현재 소켓일 때만 정리/재연결.
      // (이미 새 소켓으로 교체됐으면 stale onclose가 재연결을 트리거하지 않도록)
      if (wsRef.current === ws) {
        wsRef.current = null
        if (mountedRef.current) {
          reconnectTimer.current = setTimeout(connect, reconnectInterval)
        }
      }
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [url, reconnectInterval])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      clearTimeout(reconnectTimer.current)
      const ws = wsRef.current
      wsRef.current = null
      if (ws) {
        // 언마운트 시 onclose 재연결 로직이 돌지 않도록 분리 후 종료
        ws.onclose = null
        ws.close()
      }
    }
  }, [connect])

  const send = useCallback((msg: WsMessage) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg))
    }
  }, [])

  return { connected, send }
}
