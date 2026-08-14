import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 지금 쓸 수 있는 로봇·카메라가 몇 개인가 (feature/layout-redesign.md §5).
 *
 * ## 왜 폴링인가
 *
 * WS `device_alert` 는 **전이에서만** 온다 — 경보가 뜨고 지는 순간만 알려주므로
 * 경보 없는 정상 변화(카메라를 새로 연결하는 것)를 못 본다. 그것만 보면 개수가
 * 조용히 틀린다.
 *
 * ## 왜 그래도 싼가
 *
 * 백엔드가 **세지 않는다.** `device_watch` 가 2초마다 돌리는 조사가 이미 관리자에
 * 내려놓은 boolean 을 읽어 합칠 뿐이다. 장치를 안 건드리고 디스크도 안 본다.
 *
 * 주기 WS 브로드캐스트가 더 깔끔하지만 그건 WS 계약에 타입을 하나 더 얹는
 * 일이라 refactor #12 뒤로 미뤘다. 그때 이 파일에서 `setInterval` 만 지우면 된다.
 */

export type DeviceCount = { ok: number; warn: number }
export type DeviceSummary = {
  robots: DeviceCount
  cameras: DeviceCount
  alerts: number
}

const EMPTY: DeviceSummary = {
  robots: { ok: 0, warn: 0 },
  cameras: { ok: 0, warn: 0 },
  alerts: 0,
}

const POLL_MS = 5000

export function useDeviceSummary() {
  const [summary, setSummary] = useState<DeviceSummary>(EMPTY)

  useEffect(() => {
    let alive = true
    const tick = () => {
      api.get<DeviceSummary>('/devices/summary')
        .then((s) => { if (alive) setSummary(s) })
        .catch(() => {})
    }
    tick()
    const id = setInterval(tick, POLL_MS)
    return () => { alive = false; clearInterval(id) }
  }, [])

  return summary
}
