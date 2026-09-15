const BASE_URL = '/api'

// ⚠ **시간 제한이 없으면 탭이 죽는다.** 실기(2026-09-14): 게이트웨이 재시작 뒤 WS 가 안 붙자
//   heartbeat 가 0.5초마다 HTTP 로 떨어졌고, 그 요청들이 응답을 못 받은 채(개발 프록시가 물고 있음)
//   미리보기·조명·장치 폴링과 함께 수백 개 쌓였다. Chrome 이 탭당 한도를 넘기자 **정지 버튼의
//   요청까지 `ERR_INSUFFICIENT_RESOURCES` 로 아예 나가지 못했다** — 수집을 세울 수 없었다.
//   그래서 (1) 모든 요청에 시간 제한, (2) 같은 GET 이 아직 안 끝났으면 새로 안 보내고 그 약속을
//   같이 쓴다(폴링은 겹쳐 쌓이지 않는다). 오래 걸리는 호출은 호출부가 timeoutMs 를 늘린다.
const DEFAULT_TIMEOUT_MS = 30_000

export type ApiOptions = { timeoutMs?: number }

async function request<T>(path: string, options?: RequestInit, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), timeoutMs)
  let res: Response
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      signal: ctl.signal,
      ...options,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw new Error(`응답 없음 (${Math.round(timeoutMs / 1000)}초): ${path}`)
    throw e
  } finally {
    clearTimeout(timer)
  }
  if (!res.ok) {
    // FastAPI 에러 응답에서 detail 메시지 추출
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body.detail) detail = body.detail
    } catch {}
    throw new Error(detail)
  }
  return res.json()
}

// 같은 경로의 GET 이 아직 진행 중이면 그 약속을 돌려준다 — 폴링 타이머가 응답보다 빨라도 요청이 안 쌓인다
const inflightGets = new Map<string, Promise<unknown>>()

export const api = {
  get: <T>(path: string, opts?: ApiOptions): Promise<T> => {
    const cur = inflightGets.get(path) as Promise<T> | undefined
    if (cur) return cur
    const p = request<T>(path, undefined, opts?.timeoutMs).finally(() => inflightGets.delete(path))
    inflightGets.set(path, p)
    return p
  },
  post: <T>(path: string, body?: unknown, opts?: ApiOptions) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body) }, opts?.timeoutMs),
  put: <T>(path: string, body?: unknown, opts?: ApiOptions) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }, opts?.timeoutMs),
  delete: <T>(path: string, opts?: ApiOptions) => request<T>(path, { method: 'DELETE' }, opts?.timeoutMs),
}
