import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'

/**
 * 허브 다운로드 진행 상황 — **폴링하는 자리는 여기 하나다.**
 *
 * ## ⚠ 두 화면이 각자 판정하다 틀렸다
 *
 * `TrainingPage` 는 끝을 `status !== 'running' && status !== 'started'` 로 봤는데 서버가
 * 주는 값은 `downloading`·`completed` 라, **첫 폴링에서 바로 빠져나왔다** — 기다리는
 * 척만 했다. `HubBrowser` 는 아예 안 물어보고 "다운로드 중..." 을 영원히 띄웠다.
 * 끝을 판정하는 규칙이 둘이면 언젠가 갈린다.
 *
 * ## ⚠ 받는 중일 때만 폴링한다
 *
 * 쉬는 동안에도 1초마다 물으면 요청이 쌓인다 — 이 저장소가 겪은 적체 사고가 있다.
 */
export type DlStatus = 'idle' | 'downloading' | 'verifying' | 'completed' | 'incomplete' | 'error'

export type HubDownload = {
  status: DlStatus
  repo_id: string
  files_done: number
  files_total: number
  bytes_done: number
  bytes_total: number
  percent: number
  speed_bps: number
  eta_s: number | null
  path: string | null
  /** 받다 만 저장소에서 **빠진 파일들.** 비어 있어야 다 온 것이다. */
  missing: string[]
  error: string | null
}

const POLL_MS = 1000
const ACTIVE: DlStatus[] = ['downloading', 'verifying']

export const dlActive = (s?: DlStatus) => !!s && ACTIVE.includes(s)

/** `1.2GB`, `640MB` — 막대 옆에 한 줄로 쓰는 용도. */
export const bytes = (n: number) =>
  n >= 1 << 30 ? `${(n / (1 << 30)).toFixed(1)}GB`
    : n >= 1 << 20 ? `${Math.round(n / (1 << 20))}MB`
      : `${Math.max(1, Math.round(n / 1024))}KB`

export const eta = (s: number | null) =>
  s == null ? '' : s >= 60 ? `${Math.floor(s / 60)}분 ${s % 60}초` : `${s}초`

export function useHubDownload() {
  const [all, setAll] = useState<Record<string, HubDownload>>({})
  const watching = useRef<Set<string>>(new Set())

  const poll = useCallback(async () => {
    const ids = [...watching.current]
    if (ids.length === 0) return
    const got = await Promise.all(ids.map((id) =>
      api.get<HubDownload>(`/hub/download/status?repo_id=${encodeURIComponent(id)}`)
        .catch(() => null)))
    setAll((prev) => {
      const next = { ...prev }
      got.forEach((d, i) => {
        if (!d) return
        next[ids[i]] = d
        // 끝났으면 그만 본다 — 결과는 화면에 남는다.
        if (!dlActive(d.status)) watching.current.delete(ids[i])
      })
      return next
    })
  }, [])

  useEffect(() => {
    const t = setInterval(() => { void poll() }, POLL_MS)
    return () => clearInterval(t)
  }, [poll])

  /** 받기 시작. 시작 응답도 상태와 **같은 모양**이라 그대로 넣는다. */
  const start = useCallback(async (repoId: string, repoType: 'model' | 'dataset') => {
    watching.current.add(repoId)
    setAll((p) => ({ ...p, [repoId]: { ...(p[repoId] as HubDownload), status: 'downloading', repo_id: repoId } }))
    try {
      const d = await api.post<HubDownload>('/hub/download', { repo_id: repoId, repo_type: repoType })
      setAll((p) => ({ ...p, [repoId]: d }))
    } catch (e) {
      watching.current.delete(repoId)
      setAll((p) => ({
        ...p,
        [repoId]: {
          ...(p[repoId] as HubDownload), status: 'error', repo_id: repoId,
          error: e instanceof Error ? e.message : '시작하지 못했습니다',
        },
      }))
    }
  }, [])

  return { all, start }
}
