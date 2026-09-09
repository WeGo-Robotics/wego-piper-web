import { useWebSocket } from '../hooks/useWebSocket'
import type { LoadAlert } from '../types/ws'
import { useSystemMessage } from './SystemMessages'

/**
 * 관절 과부하 경보를 **시스템 메시지로 흘려보낸다.** 자기 UI 는 없다 —
 * `DeviceAlerts` 와 같은 자리, 같은 모양이다.
 *
 * ## ⚠ `DeviceAlerts` 와 결정적으로 다른 점: 지우지 않는다
 *
 * 저쪽은 매번 `clear(PREFIX)` 로 목록을 갈아끼운다. 장치 경보는 **상태**라
 * 조건이 풀리면 메시지도 사라져야 맞기 때문이다.
 *
 * 과부하는 **사건**이다. 실기(2026-09-09)에서 J2 는 19초 동안 임계 위에 있다가
 * 내려왔고, 2분 뒤에는 이미 정상이었다. 상태로 다뤘다면 그 경보는 잠깐 떴다
 * 스스로 사라졌을 것이고 — 자리를 비웠던 사람에게는 아무 일도 없던 것이 된다.
 * 알아야 할 것은 "지금 눌리고 있다" 가 아니라 **"눌렸었다"** 라서, 지우는 것은
 * 사람이 정한다.
 *
 * ⚠ **문구는 백엔드가 만든다.** 경보가 선 순간의 토크·지속시간은 그 뒤 표본에
 * 덮여 사라지므로, 화면이 나중에 문장을 조립하면 경보 때와 다른 숫자를 말한다.
 */
export default function LoadAlerts() {
  const { notify } = useSystemMessage()

  useWebSocket('/ws', {
    onMessage: (msg) => {
      if (msg.type !== 'robot_load_alert') return
      for (const a of msg.data.alerts as LoadAlert[]) {
        notify({
          // id 에 사건 시각이 들어 있어 같은 관절의 다음 과부하는 새 줄이 된다.
          id: `load:${a.id}`,
          // 슬립이 났으면 그 구간의 관절 기록이 통째로 틀어진다 — 데이터가
          // 조용히 오염되는 쪽이라 경고가 아니라 오류로 띄운다.
          level: 'error',
          text: a.text,
          source: '부하',
        })
      }
    },
  })

  return null
}
