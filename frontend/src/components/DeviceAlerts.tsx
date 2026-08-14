import { useEffect, useState } from 'react'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../services/api'
import type { DeviceAlert } from '../types/ws'

/**
 * 장치가 사라졌을 때 뜨는 배너 — 어느 페이지에 있든 보인다.
 *
 * USB 가 빠지거나 컨트롤러가 죽으면 목록이 마지막 상태에 머물렀고, 추론만 뒤늦게
 * "세그먼트가 없습니다"로 죽었다. 화면과 에러가 다른 말을 하면 원인을 못 찾는다.
 *
 * ⚠ **문구를 여기서 만들지 않는다.** 백엔드가 "장치가 빠졌다"와 "데몬이 내려갔다"를
 * 갈라서 문장까지 준다 — 화면이 조립하면 한쪽만 고쳐져 어긋난다(`usb_warning` 과 같은 규칙).
 *
 * ⚠ **닫아도 사라진 사실이 없어지지는 않는다.** 닫기는 이번 경보만 감추고, 장치가
 * 돌아왔다가 다시 빠지면 새 경보로 다시 뜬다.
 */
export default function DeviceAlerts() {
  const [alerts, setAlerts] = useState<DeviceAlert[]>([])
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())

  const { connected } = useWebSocket('/ws', {
    onMessage: (msg) => {
      if (msg.type !== 'device_alert') return
      setAlerts(msg.data.alerts)
      // 복구된 것은 닫힘 표시도 지운다 — 다시 빠지면 다시 보여야 한다
      if (msg.data.cleared.length > 0) {
        setDismissed((prev) => {
          const next = new Set(prev)
          for (const c of msg.data.cleared) next.delete(c.id)
          return next
        })
      }
    },
  })

  // WS 는 **전이에서만** 온다 — 그 순간을 놓쳤으면 알 길이 없다. 그래서 소켓이
  // (다시) 붙을 때마다 현재 목록을 받아온다.
  //
  // ⚠ 처음 한 번만 받아오면 **재연결 구간을 통째로 놓친다.** 백엔드를 재시작하는
  // 배포가 정확히 그 구간을 만들고, 그 사이에 장치가 빠지면 화면은 영영 모른다.
  useEffect(() => {
    if (!connected) return
    api.get<{ alerts: DeviceAlert[] }>('/devices/alerts')
      .then((r) => setAlerts(r.alerts))
      .catch(() => {})
  }, [connected])

  const visible = alerts.filter((a) => !dismissed.has(a.id))
  if (visible.length === 0) return null

  return (
    <div className="max-w-7xl mx-auto px-4 pt-3 space-y-2">
      {visible.map((a) => (
        <div
          key={a.id}
          className={`flex items-start gap-3 rounded-lg border px-3 py-2 text-sm ${
            a.reason !== 'device_gone'
              ? 'border-amber-500/40 bg-amber-500/10 text-amber-200'
              : 'border-red-500/40 bg-red-500/10 text-red-200'
          }`}
        >
          <span aria-hidden>⚠</span>
          <p className="flex-1">{a.text}</p>
          <button
            onClick={() => setDismissed((prev) => new Set(prev).add(a.id))}
            className="text-xs opacity-60 hover:opacity-100"
            title="이 경보만 숨깁니다 — 장치가 돌아온 것은 아닙니다"
          >
            닫기
          </button>
        </div>
      ))}
    </div>
  )
}
