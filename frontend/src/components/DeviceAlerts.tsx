import { useEffect } from 'react'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../services/api'
import type { DeviceAlert } from '../types/ws'
import { useSystemMessage } from './SystemMessages'

/**
 * 장치 경보를 **시스템 메시지로 흘려보낸다.** 자기 UI 는 없다.
 *
 * 예전에는 자기 배너를 `Layout` 안 문서 흐름에 그렸는데, 수집 페이지처럼 긴 화면에서는
 * 스크롤을 올려야 보였다 — 정작 그 경보가 필요한 순간에 안 보였다.
 * 이제 어디에 어떻게 그릴지는 `SystemMessageHost` 한 곳이 정한다.
 *
 * ⚠ **문구는 백엔드가 만든다.** "장치가 빠졌다"와 "데몬이 내려갔다"와 "꽂혀 있는데
 * 발행이 멈췄다"를 가르는 판정이 거기 있다 — 화면이 문장을 조립하면 한쪽만 고쳐져
 * 어긋난다(`usb_warning` 과 같은 규칙).
 */

const PREFIX = 'device:'

export default function DeviceAlerts() {
  const { notify, clear } = useSystemMessage()

  const apply = (alerts: DeviceAlert[]) => {
    // 사라진 경보는 걷어낸다 — 장치가 돌아왔는데 경고가 남으면 아무도 안 믿는다
    clear(PREFIX)
    for (const a of alerts) {
      notify({
        id: `${PREFIX}${a.id}`,
        // `daemon_down`·`stalled` 는 사람이 고칠 수 있는 것이라 경고,
        // 장치가 없어진 것은 오류다. 판정은 백엔드가 `reason` 으로 준다.
        level: a.reason === 'device_gone' ? 'error' : 'warn',
        text: a.text,
        source: a.kind === 'robot' ? '로봇' : '카메라',
      })
    }
  }

  const { connected } = useWebSocket('/ws', {
    onMessage: (msg) => {
      if (msg.type === 'device_alert') apply(msg.data.alerts)
    },
  })

  // WS 는 **전이에서만** 온다 — 그 순간을 놓쳤으면 알 길이 없다. 그래서 소켓이
  // (다시) 붙을 때마다 현재 목록을 받아온다. 처음 한 번만 받으면 재연결 구간을
  // 통째로 놓치는데, 백엔드를 재시작하는 배포가 정확히 그 구간을 만든다.
  useEffect(() => {
    if (!connected) return
    api.get<{ alerts: DeviceAlert[] }>('/devices/alerts')
      .then((r) => apply(r.alerts))
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected])

  return null
}
