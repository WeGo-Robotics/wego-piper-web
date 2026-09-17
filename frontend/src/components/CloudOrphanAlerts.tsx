import { useEffect } from 'react'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../services/api'
import type { CloudOrphan } from '../types/ws'
import { useSystemMessage } from './SystemMessages'

/**
 * 빌린 GPU 가 **관리 밖에서 살아 있다** — 시스템 메시지로 흘려보낸다 (§6-3).
 *
 * `DeviceAlerts` 와 같은 자리, 같은 모양이다. 다른 점은 **이건 돈이다.**
 *
 * ## ⚠ 클라우드 페이지에 두지 않는 이유
 *
 * 인스턴스 탭은 이미 고아를 표시한다. 그런데 고아가 생기는 상황이 곧 **아무도 그 탭을
 * 안 보는 상황**이다 — 게이트웨이가 죽었다 살아났거나 배포로 재시작한 직후다. 그동안
 * 요금은 계속 나간다. 그래서 어느 페이지에 있든 알림함에 쌓이게 `Layout` 에 둔다.
 *
 * ## ⚠ 여기에 [파기] 버튼을 달지 않는다
 *
 * 자동 파기를 안 하는 이유(§10 결정 5)와 같다 — 라벨이 `piper-` 라고 해서 이 기계의
 * 것이라는 보장이 없다. 다른 게이트웨이가 돌리는 6시간짜리 학습일 수 있고, 토스트에서
 * 한 번 잘못 누르면 그게 사라진다. 끄는 것은 인스턴스 탭에서, 목록을 보고 한다.
 */

const PREFIX = 'cloud-orphan:'

export default function CloudOrphanAlerts() {
  const { notify, clear } = useSystemMessage()

  const apply = (orphans: CloudOrphan[]) => {
    clear(PREFIX)
    for (const o of orphans) {
      notify({
        // ⚠ id 를 인스턴스 번호로 고정한다. 스캔마다 새 id 를 주면 같은 기계가
        //   알림함에 10분마다 한 줄씩 쌓여, 고아 하나가 스무 줄이 된다.
        id: `${PREFIX}${o.id}`,
        level: 'error',
        text: o.text,
        source: '클라우드 GPU',
      })
    }
  }

  const { connected } = useWebSocket('/ws', {
    onMessage: (msg) => {
      if (msg.type === 'cloud_orphan_alert') apply(msg.data.orphans)
    },
  })

  // WS 는 **전이에서만** 온다. 스캔은 10분에 한 번이라 그 순간을 놓치면 다음 전이까지
  // 모른다 — 소켓이 (다시) 붙을 때마다 마지막 스캔 결과를 받아온다. 이 요청은 Vast 를
  // 부르지 않고 서버 메모리만 읽으므로 페이지마다 붙어도 싸다.
  useEffect(() => {
    if (!connected) return
    api.get<{ orphans: CloudOrphan[] }>('/cloud/orphans')
      .then((r) => apply(r.orphans ?? []))
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected])

  return null
}
