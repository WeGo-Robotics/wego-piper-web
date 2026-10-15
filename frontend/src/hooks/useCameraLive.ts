import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'
import { tr } from '../i18n'

/**
 * 카메라 페이지의 상시 보기 — 등록된 카메라는 들어가자마자 shm 에서 읽어 보여 주고,
 * 멈춰 있으면(유휴 종료·프레임 시간 초과) 바로 다시 연다.
 *
 * ⚠ **카드마다 `/stream` 을 걸지 않는다.** 스트림 하나는 연결 하나를 계속 쥐고, 브라우저는
 *   오리진당 6개(HTTP/1.1)뿐이다. 카메라 6대면 **E-stop 버튼의 요청이 큐에 서서 나가지 못한다.**
 *   그래서 한 연결(`/api/cameras/live`)로 전부 받고 조각의 `X-Camera` 헤더로 카드를 가른다.
 */

// ── 조각 파서 ──

// `Blob` 은 `ArrayBuffer` 위의 뷰만 받는다(공유 버퍼는 안 된다). 스트림 청크와 우리가 만드는 배열은
// 모두 그렇다 — 타입에 그걸 적어 둔다.
type Bytes = Uint8Array<ArrayBuffer>

const MAX_PART_BYTES = 16 * 1024 * 1024     // 이보다 큰 조각은 깨진 것이다 — 버퍼를 무한정 키우지 않는다

function headerEnd(buf: Bytes): number {
  for (let i = 0; i + 3 < buf.length; i++) {
    if (buf[i] === 13 && buf[i + 1] === 10 && buf[i + 2] === 13 && buf[i + 3] === 10) return i
  }
  return -1
}

function concat(parts: Bytes[], size: number): Bytes {
  if (parts.length === 1) return parts[0]
  const out = new Uint8Array(size)
  let at = 0
  for (const p of parts) { out.set(p, at); at += p.length }
  return out
}

/**
 * `multipart/x-mixed-replace` 조각을 `(카메라 id, JPEG)` 로 가르는 증분 파서.
 *
 * 조각 경계(`--piperframe`)를 찾지 않고 `Content-Length` 만큼 읽는다 — JPEG 바이트 안의 우연한
 * 일치에 속지 않는다. 한 장이 다 들어오기 전에는 청크를 합치지 않는다(`need`).
 */
export function createPartParser(onFrame: (camId: string, jpeg: Bytes) => void) {
  const text = new TextDecoder()
  let parts: Bytes[] = []
  let size = 0
  let need = 0

  return (chunk: Bytes) => {
    parts.push(chunk)
    size += chunk.length
    if (need && size < need) return
    let buf = concat(parts, size)
    need = 0
    for (;;) {
      const end = headerEnd(buf)
      if (end < 0) break
      const head = text.decode(buf.subarray(0, end))
      const len = /content-length:\s*(\d+)/i.exec(head)
      if (!len) { buf = buf.subarray(end + 4); continue }       // 모르는 조각 — 건너뛴다
      const n = Number(len[1])
      if (n > MAX_PART_BYTES) { buf = new Uint8Array(0); break }
      const bodyStart = end + 4
      const bodyEnd = bodyStart + n
      if (buf.length < bodyEnd) { need = bodyEnd; break }
      const cam = /x-camera:\s*(\S+)/i.exec(head)
      if (cam) {
        try { onFrame(decodeURIComponent(cam[1]), buf.subarray(bodyStart, bodyEnd)) }
        catch { /* 한 장이 깨져도 다음 장은 온다 */ }
      }
      buf = buf.subarray(bodyEnd)
    }
    parts = buf.length ? [buf] : []
    size = buf.length
  }
}

// ── 한 연결로 받기 ──

const RETRY_MIN_MS = 1_000
const RETRY_MAX_MS = 10_000
// 새 프레임을 `src` 로 건 뒤 옛 주소를 놓는 유예. 바로 놓으면 아직 그리는 중인 이미지가 끊긴다.
const REVOKE_AFTER_MS = 1_000

export type LiveFrames = {
  /** `<img>` 를 이 카메라의 프레임 자리로 등록한다. 돌려주는 함수가 등록을 푼다. */
  attach: (id: string, el: HTMLImageElement) => () => void
}

/**
 * `ids` 의 프레임을 한 연결로 받아 등록된 `<img>` 의 `src` 에 **직접** 건다.
 *
 * React 상태를 거치지 않는다 — 프레임이 초당 수십 장이라 상태로 올리면 페이지 전체(1,400줄)가
 * 그만큼 다시 그려진다. 그래서 `<img>` 에 `src` 속성을 주지 않고(`LiveCameraFrame`), 여기서만 쓴다.
 *
 * 끊기면 스스로 다시 건다. 탭이 가려지면 닫는다 — 안 보는 화면이 연결(과 인코딩)을 쥘 이유가 없고,
 * 읽는 곳이 없으면 유휴 종료 옵션이 켜진 카메라는 닫힌다(돌아오면 `useReviveStopped` 가 연다).
 */
export function useLiveFrames(ids: string[]): LiveFrames {
  const sinks = useRef(new Map<string, { el: HTMLImageElement; url: string | null }>())
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden')
  // 목록이 같으면 같은 연결을 쓴다 — 정렬해서 순서 차이로 다시 걸지 않는다
  const key = [...new Set(ids)].sort().join('\n')

  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', on)
    return () => document.removeEventListener('visibilitychange', on)
  }, [])

  useEffect(() => {
    if (!key || !visible) return
    const list = key.split('\n')
    let dead = false
    let ctl: AbortController | null = null
    let timer: ReturnType<typeof setTimeout> | null = null
    let wait = RETRY_MIN_MS

    const deliver = (id: string, jpeg: Bytes) => {
      const sink = sinks.current.get(id)
      if (!sink) return                      // 카드가 아직 안 붙었다 — 다음 장이 곧 온다
      const url = URL.createObjectURL(new Blob([jpeg], { type: 'image/jpeg' }))
      const prev = sink.url
      sink.url = url
      sink.el.src = url
      if (prev) setTimeout(() => URL.revokeObjectURL(prev), REVOKE_AFTER_MS)
    }

    const open = async () => {
      ctl = new AbortController()
      try {
        const qs = list.map((i) => `ids=${encodeURIComponent(i)}`).join('&')
        const res = await fetch(`/api/cameras/live?${qs}`, { signal: ctl.signal })
        if (!res.ok || !res.body) throw new Error(String(res.status))
        wait = RETRY_MIN_MS
        const feed = createPartParser(deliver)
        const reader = res.body.getReader()
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          if (value) feed(value)
        }
      } catch { /* 끊겼다 — 아래에서 다시 건다. 끝나는 것은 우리가 닫을 때뿐이다 */ }
      if (dead) return
      timer = setTimeout(open, wait)
      wait = Math.min(wait * 2, RETRY_MAX_MS)
    }
    open()

    return () => {
      dead = true
      ctl?.abort()
      if (timer) clearTimeout(timer)
    }
  }, [key, visible])

  const attach = useCallback((id: string, el: HTMLImageElement) => {
    const sink = { el, url: null as string | null }
    sinks.current.set(id, sink)
    return () => {
      if (sinks.current.get(id) === sink) sinks.current.delete(id)
      if (sink.url) URL.revokeObjectURL(sink.url)
    }
  }, [])

  return { attach }
}

// ── 멈춰 있으면 바로 살린다 ──

export type ReviveCandidate = {
  id: string; ready: boolean; connected: boolean
  present?: boolean; streaming?: boolean
}

/** 시도 사이에 기다릴 시간(ms) — 인덱스 = 지금까지의 시도 수. 처음은 **바로** 건다. */
const REVIVE_WAIT_MS = [0, 10_000, 30_000]
/** 연결은 됐는데 영상이 안 오는 채로 마지막 시도 뒤 이만큼 지나면 포기한 것으로 말한다. */
const REVIVE_GIVE_UP_MS = 15_000
// RealSense 는 열리는 데 수 초가 걸린다 — 기본 30초 제한이면 정상 연결을 실패로 읽을 수 있다
const CONNECT_TIMEOUT_MS = 60_000

/** 멈춘 카메라 판정. `streaming` 이 없는(옛 백엔드) 값은 멈춘 것으로 보지 않는다. */
export const isStopped = (c: ReviveCandidate) =>
  c.ready && c.present !== false && (!c.connected || c.streaming === false)

/**
 * 등록된 카메라가 멈춰 있으면(유휴 종료 · 프레임 시간 초과 · 데몬 재시작) 다시 연결한다.
 *
 * - 페이지에 들어오는 즉시, 이후엔 목록이 갱신될 때마다 본다.
 * - **순서대로** 연다 — RealSense 두 대를 동시에 열면 USB 대역폭을 나눠 초기화가 늘어진다.
 * - 못 연 카메라를 쉬지 않고 두드리지 않는다: 시도 사이를 늘리고(10초·30초) 세 번에서 멈춘다.
 *   멈춘 장치를 반복해서 여는 것이 D405 가 D-state 로 물리는 대표 경로였다. 이후는 사람이
 *   `retry` 로 건다.
 * - 뽑힌 카메라(`present === false`)는 건드리지 않는다.
 */
export function useReviveStopped(
  cams: ReviveCandidate[], enabled: boolean, onSettled?: () => void,
) {
  const [inflight, setInflight] = useState<string[]>([])
  const [failed, setFailed] = useState<Record<string, string>>({})
  const [kick, setKick] = useState(0)
  const tries = useRef<Record<string, { n: number; at: number }>>({})
  const running = useRef(false)
  const alive = useRef(true)
  const settled = useRef(onSettled)
  settled.current = onSettled

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  useEffect(() => {
    if (!enabled) return
    const now = Date.now()

    // 영상이 다시 오는 카메라는 기록을 지운다 — 다음에 멈추면 처음부터 다시 시도한다.
    const healthy = cams.filter((c) => c.ready && c.connected && c.streaming !== false).map((c) => c.id)
    for (const id of healthy) delete tries.current[id]
    if (healthy.some((id) => id in failed)) {
      setFailed((prev) => Object.fromEntries(Object.entries(prev).filter(([id]) => !healthy.includes(id))))
    }

    const stopped = cams.filter(isStopped)

    // 연결 요청은 성공했는데 끝내 영상이 안 오는 경우 — 사유가 없어서 사람이 기다리게 된다.
    for (const c of stopped) {
      const t = tries.current[c.id]
      if (t && t.n >= REVIVE_WAIT_MS.length && now - t.at >= REVIVE_GIVE_UP_MS && !(c.id in failed)) {
        setFailed((prev) => ({ ...prev, [c.id]: tr('연결은 됐지만 영상이 오지 않습니다') }))
      }
    }

    if (running.current) return
    const due = stopped.filter((c) => {
      const t = tries.current[c.id]
      return !t || (t.n < REVIVE_WAIT_MS.length && now - t.at >= REVIVE_WAIT_MS[t.n])
    })
    if (due.length === 0) return

    running.current = true
    void (async () => {
      try {
        for (const c of due) {
          const t = tries.current[c.id] ?? { n: 0, at: 0 }
          tries.current[c.id] = { n: t.n + 1, at: Date.now() }
          if (alive.current) setInflight((prev) => [...prev, c.id])
          try {
            await api.post('/cameras/connect', { id: c.id }, { timeoutMs: CONNECT_TIMEOUT_MS })
            // 성공해도 사유를 지우지 않는다 — 영상이 실제로 오는 것(healthy)이 지우는 조건이다.
          } catch (e) {
            const reason = e instanceof Error ? e.message : String(e)
            if (alive.current) setFailed((prev) => ({ ...prev, [c.id]: reason }))
          } finally {
            if (alive.current) setInflight((prev) => prev.filter((x) => x !== c.id))
          }
        }
      } finally {
        running.current = false
        if (alive.current) {
          settled.current?.()      // 목록을 바로 받는다 — 다음 폴링(3초)을 기다리지 않는다
          setKick((k) => k + 1)
        }
      }
    })()
    // `failed` 는 읽기만 한다(위의 사유 채우기) — 의존성에 넣으면 사유가 바뀔 때마다 다시 돈다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cams, enabled, kick])

  /** 사람이 다시 건다 — 시도 기록을 지우고 처음부터. */
  const retry = useCallback((id: string) => {
    delete tries.current[id]
    setFailed((prev) => Object.fromEntries(Object.entries(prev).filter(([k]) => k !== id)))
    setKick((k) => k + 1)
  }, [])

  return {
    /** 이 카메라를 지금 여는 중인가 */
    connecting: (id: string) => inflight.includes(id),
    /** 못 살린 사유. 없으면 `null` (아직 시도 중이거나 영상을 기다리는 중) */
    failure: (id: string): string | null => failed[id] ?? null,
    retry,
  }
}
