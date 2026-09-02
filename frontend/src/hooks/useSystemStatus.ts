import { useEffect, useRef, useState } from 'react'
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

/**
 * 서버가 4초마다 뜨는 추이 견본 한 점. `t` 는 epoch **초**다.
 *
 * ⚠ 추이는 **서버가 쌓는다**(`app/services/trends.py`). 브라우저가 쌓으면
 * 페이지를 연 순간부터만 보이는데, 그래프를 보러 오는 이유는 대개
 * "아까 무슨 일이 있었나"다. 첫 응답이 창 전체(15분)를 싣고 오고,
 * 이후 폴링은 `since` 로 새 견본만 받는다.
 */
export type TrendGpu = { name: string; util_pct: number | null; mem_pct: number | null }
export type TrendSample = {
  t: number
  cpu_pct: number | null
  /** 추론이 안 돌면 null — 그래프가 거기서 끊긴다. */
  fps: number | null
  gpus: TrendGpu[]
}

export type SystemStatus = {
  units: Unit[]
  gateway: Gateway | null
  gpus: Gpu[]
  disks: Disk[]
  estop: Estop | null
  cpu: number | null
  /** 최근 15분 견본 — 로딩 직후부터 차 있다. */
  samples: TrendSample[]
}

const EMPTY: SystemStatus = {
  units: [], gateway: null, gpus: [], disks: [], estop: null, cpu: null, samples: [],
}
const POLL_MS = 4000
export const TREND_WINDOW_S = 15 * 60

type ResourcesResp = {
  gpus: Gpu[]; disks: Disk[]; cpu_pct: number | null; samples: TrendSample[]
}

export function useSystemStatus() {
  const [s, setS] = useState<SystemStatus>(EMPTY)
  // 마지막으로 받은 견본의 t — 다음 폴링은 이 이후 것만 달라고 한다.
  // null 이면 첫 요청이고, 서버가 창 전체를 보낸다.
  const sinceRef = useRef<number | null>(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      const since = sinceRef.current
      const [svc, res, estop] = await Promise.all([
        api.get<{ units: Unit[]; gateway: Gateway }>('/system/services').catch(() => null),
        api.get<ResourcesResp>(
          `/system/resources${since !== null ? `?since=${since}` : ''}`).catch(() => null),
        api.get<Estop>('/estop/status').catch(() => null),
      ])
      if (!alive) return
      const add = res?.samples ?? []
      if (add.length) sinceRef.current = add[add.length - 1].t
      setS((prev) => {
        let samples = prev.samples
        if (add.length) {
          // 첫 응답(since 없음)은 창 전체라 **갈아끼운다** — 이어붙이면 겹친다
          const merged = since === null ? add : [...prev.samples, ...add]
          const cut = merged[merged.length - 1].t - TREND_WINDOW_S
          samples = merged.filter((p) => p.t >= cut)
        }
        return {
          // 실패한 것은 **직전 값을 유지한다.** 한 번 못 받았다고 화면에서
          // 사라지면, 깜빡이는 것이 값 자체보다 더 눈에 띈다.
          units: svc?.units ?? prev.units,
          gateway: svc?.gateway ?? prev.gateway,
          gpus: res?.gpus ?? prev.gpus,
          disks: res?.disks ?? prev.disks,
          estop: estop ?? prev.estop,
          cpu: res ? res.cpu_pct : prev.cpu,
          samples,
        }
      })
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
