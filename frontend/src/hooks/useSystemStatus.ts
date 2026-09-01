import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 대시보드가 "지금 괜찮은가"를 답하는 데 필요한 것 한 벌.
 *
 * ## 왜 훅 하나로 묶나
 *
 * 화면마다 따로 부르면 **화면마다 다른 순간의 상태**를 그린다 — 서비스는 방금
 * 것인데 자원은 5초 전이면, 그 둘을 나란히 놓고 판단하는 사람이 틀린다.
 *
 * ## 왜 폴링인가
 *
 * WS 는 로그 스트리밍용이고, 여기 값들은 초 단위로 안 변한다. `useDeviceSummary`
 * 가 이미 같은 방식을 쓰고 있어 맞춘다.
 *
 * ⚠ **하나가 실패해도 나머지는 그린다.** 자원 조회가 막혔다고 서비스 상태까지
 * 못 보면, 정작 문제가 났을 때 대시보드가 통째로 빈다.
 */

export type Unit = {
  name: string; active: boolean; stale: boolean
  age_s: number | null; description: string
}
export type Gateway = { stale: boolean; age_s: number | null }
export type Gpu = {
  name: string; driver: string
  util_pct: number | null; mem_used_mb: number | null
  mem_total_mb: number | null; temp_c: number | null
}
export type Disk = { path: string; total_gb: number; free_gb: number; used_pct: number | null }
export type Estop = { bus_available: boolean; armed: boolean; last_trigger: number | null }

export type SystemStatus = {
  units: Unit[]
  gateway: Gateway | null
  gpus: Gpu[]
  disks: Disk[]
  estop: Estop | null
}

const EMPTY: SystemStatus = { units: [], gateway: null, gpus: [], disks: [], estop: null }
const POLL_MS = 4000

export function useSystemStatus() {
  const [s, setS] = useState<SystemStatus>(EMPTY)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      const [svc, res, estop] = await Promise.all([
        api.get<{ units: Unit[]; gateway: Gateway }>('/system/services').catch(() => null),
        api.get<{ gpus: Gpu[]; disks: Disk[] }>('/system/resources').catch(() => null),
        api.get<Estop>('/estop/status').catch(() => null),
      ])
      if (!alive) return
      setS((prev) => ({
        // 실패한 것은 **직전 값을 유지한다.** 한 번 못 받았다고 화면에서
        // 사라지면, 깜빡이는 것이 값 자체보다 더 눈에 띈다.
        units: svc?.units ?? prev.units,
        gateway: svc?.gateway ?? prev.gateway,
        gpus: res?.gpus ?? prev.gpus,
        disks: res?.disks ?? prev.disks,
        estop: estop ?? prev.estop,
      }))
    }
    void tick()
    const id = setInterval(() => { void tick() }, POLL_MS)
    return () => { alive = false; clearInterval(id) }
  }, [])

  return s
}

export function ageText(sec: number | null): string {
  if (sec === null) return '?'
  if (sec < 60) return `${sec}초`
  if (sec < 3600) return `${Math.round(sec / 60)}분`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}시간`
  return `${(sec / 86400).toFixed(1)}일`
}
