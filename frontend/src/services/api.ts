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
    // ⚠ 401 은 **화면 하나의 문제가 아니라 전체의 문제**다. 호출부마다 처리하면 어떤
    //   화면은 로그인으로 가고 어떤 화면은 빨간 글씨만 뜬다. 여기서 한 번 알리고
    //   `AuthGate` 가 받는다.
    // ⚠ 로그인 요청 자체는 뺀다 — 비밀번호를 틀린 것은 "세션이 끊겼다" 가 아니다.
    if (res.status === 401 && !path.startsWith('/auth/')) {
      window.dispatchEvent(new CustomEvent('piper:unauthorized'))
    }
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
  // ⚠ DELETE 에 본문을 허용한다 — 인증 해제가 "지금 비밀번호" 를 받아야 하기 때문이다.
  //   안 받으면 로그인만으로 끌 수 있고, 그러면 자리를 비운 사이 누구나 문을 열어 둘 수 있다.
  delete: <T>(path: string, opts?: ApiOptions & { body?: unknown }) =>
    request<T>(path, {
      method: 'DELETE',
      ...(opts?.body !== undefined ? { body: JSON.stringify(opts.body) } : {}),
    }, opts?.timeoutMs),
}
