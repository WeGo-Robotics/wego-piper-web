/**
 * 오프라인 평가 작업 — 진행 폴링과 대기열 (feature/weight-analyzer.md §4).
 *
 * 추론 분석 화면이 들고 있어 **탭을 오가도** 진행과 대기열이 유지된다. 작업은 유닛으로 도니
 * 화면을 닫아도 계속되고, 다시 열면 `/weval/jobs/current` 로 이어 붙는다.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'
import type { JobStatus } from './weightTypes'

export type JobRequest = { checkpoint: string; dataset: string; episodes: number[]; stride: number }

export function useWevalJob(onError: (text: string) => void) {
  const [status, setStatus] = useState<JobStatus>({ state: 'idle' })
  const [queue, setQueue] = useState<JobRequest[]>([])
  // 결과가 새로 생길 때마다 올린다 — 탭들이 이걸 보고 목록을 다시 읽는다
  const [version, setVersion] = useState(0)
  const starting = useRef(false)

  const refresh = useCallback(async () => {
    try {
      const s = await api.get<JobStatus>('/weval/jobs/current')
      setStatus(s)
      return s
    } catch {
      return null
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  // 도는 동안만 폴링 — 놀 때 1.5초마다 묻는 것은 요청 적체의 씨앗이다
  useEffect(() => {
    if (status.state !== 'running') return
    const id = setInterval(() => { void refresh() }, 1500)
    return () => clearInterval(id)
  }, [status.state, refresh])

  const prev = useRef(status.state)
  useEffect(() => {
    if (prev.current === 'running' && status.state !== 'running') {
      setVersion((v) => v + 1)
      if (status.state === 'error' || status.state === 'stopped') {
        if (status.error) onError(status.error)
        setQueue([])
      }
    }
    prev.current = status.state
  }, [status, onError])

  const start = useCallback(async (req: JobRequest) => {
    starting.current = true
    try {
      const r = await api.post<{ key: string; cached: boolean }>('/weval/jobs', req)
      if (r.cached) setVersion((v) => v + 1)
      else await refresh()
      return r.key
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
      setQueue([])
      return null
    } finally {
      starting.current = false
    }
  }, [refresh, onError])

  // 대기열 — 하나가 끝나면 다음
  useEffect(() => {
    if (status.state === 'running' || queue.length === 0 || starting.current) return
    const [next, ...rest] = queue
    setQueue(rest)
    void start(next)
  }, [status.state, queue, start])

  const stop = useCallback(async () => {
    setQueue([])
    try { await api.post('/weval/jobs/stop') } catch { /* 이미 끝났다 */ }
    await refresh()
  }, [refresh])

  const enqueue = useCallback((reqs: JobRequest[]) => setQueue((q) => [...q, ...reqs]), [])

  return { status, queue, version, start, enqueue, stop }
}
