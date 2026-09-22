import { useEffect, useState } from 'react'

/**
 * API 문서 — Swagger UI 로 가는 문.
 *
 * ## 왜 화면에 두나
 *
 * `/docs` 는 게이트웨이가 늘 서빙하지만 **앞단을 지나야 도달한다**. nginx 의 SPA
 * 폴백(`location /`)이 미등록 경로를 전부 index.html 로 돌리므로, `/docs` 를 따로
 * 프록시하지 않는 앞단에서는 200 과 함께 **빈 화면**이 온다 — 백엔드는 멀쩡하고
 * 앞단만 가로챈 모양이라 원인이 안 보인다. 컨테이너가 옛 `nginx.conf` 로 도는
 * 동안 딱 그 상태가 된다.
 *
 * 그래서 링크만 걸지 않고 **실제로 열어 보고** 결과를 말해 준다. 링크가 죽어 있는데
 * 아무 말이 없는 것보다, 왜 죽었는지 적힌 편이 낫다.
 */

type Reach = 'checking' | 'ok' | 'shadowed' | 'down'

export default function ApiDocsPanel() {
  const [reach, setReach] = useState<Reach>('checking')

  useEffect(() => {
    let alive = true
    fetch('/docs', { headers: { Accept: 'text/html' } })
      .then(async (r) => {
        if (!alive) return
        if (!r.ok) { setReach('down'); return }
        // SPA 폴백이 가로챘는지는 상태코드로 못 가린다 — 둘 다 200 이다. 내용을 본다.
        setReach((await r.text()).includes('swagger-ui') ? 'ok' : 'shadowed')
      })
      .catch(() => { if (alive) setReach('down') })
    return () => { alive = false }
  }, [])

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5 space-y-4">
      <div>
        <h2 className="text-lg font-semibold">REST API 문서</h2>
        <p className="text-xs text-neutral-400 mt-1">
          게이트웨이의 전체 API 표면을 Swagger UI 로 봅니다. 경로·요청 바디·응답을
          코드에서 직접 뽑아 만들므로 이 화면이 <b>정본</b>입니다.
        </p>
      </div>

      <div className="space-y-2">
        <a href="/docs" target="_blank" rel="noreferrer"
          className="flex items-center gap-3 rounded-lg border border-neutral-700 bg-neutral-900 px-4 py-2.5 hover:border-blue-500 transition-colors">
          <span className="flex-1">
            <span className="font-mono text-sm text-blue-400">/docs</span>
            <span className="block text-xs text-neutral-400 mt-0.5">Swagger UI — 읽고 그 자리에서 호출까지</span>
          </span>
          <span className="text-neutral-500 text-sm flex-shrink-0">↗</span>
        </a>

        <a href="/redoc" target="_blank" rel="noreferrer"
          className="flex items-center gap-3 rounded-lg border border-neutral-700 bg-neutral-900 px-4 py-2.5 hover:border-blue-500 transition-colors">
          <span className="flex-1">
            <span className="font-mono text-sm text-blue-400">/redoc</span>
            <span className="block text-xs text-neutral-400 mt-0.5">ReDoc — 읽기 전용, 길게 훑기 좋다</span>
          </span>
          <span className="text-neutral-500 text-sm flex-shrink-0">↗</span>
        </a>

        <a href="/openapi.json" target="_blank" rel="noreferrer"
          className="flex items-center gap-3 rounded-lg border border-neutral-700 bg-neutral-900 px-4 py-2.5 hover:border-blue-500 transition-colors">
          <span className="flex-1">
            <span className="font-mono text-sm text-blue-400">/openapi.json</span>
            <span className="block text-xs text-neutral-400 mt-0.5">OpenAPI 스키마 원본 — 클라이언트 생성용</span>
          </span>
          <span className="text-neutral-500 text-sm flex-shrink-0">↗</span>
        </a>
      </div>

      {reach === 'shadowed' && (
        <div className="rounded-lg border border-amber-600/50 bg-amber-950/30 px-4 py-3 text-xs text-amber-200 space-y-1">
          <div className="font-semibold">⚠ 이 주소에서는 아직 안 열립니다</div>
          <p>
            앞단(nginx)이 <code className="font-mono">/docs</code> 를 프록시하지 않아 SPA 화면이 대신 나옵니다.
            프론트 컨테이너가 옛 설정으로 도는 동안 그렇습니다 — 이미지를 다시 구우면 해결됩니다.
          </p>
          <p className="text-amber-300/80">
            그동안은 게이트웨이에 직접: <code className="font-mono">http://{window.location.hostname}:8000/docs</code>
          </p>
        </div>
      )}

      {reach === 'down' && (
        <div className="rounded-lg border border-red-600/50 bg-red-950/30 px-4 py-3 text-xs text-red-200">
          <span className="font-semibold">문서에 닿지 못했습니다</span> — 게이트웨이가 떠 있는지 확인하세요.
        </div>
      )}

      <div className="rounded-lg border border-neutral-700 bg-neutral-900 px-4 py-3 text-xs text-neutral-400 space-y-2">
        <p>
          <b className="text-neutral-300">내부 API</b> <code className="font-mono">/api/*</code> — 이 화면이 쓰는 경로.
          LAN 신뢰를 전제로 인증이 없습니다.
        </p>
        <p>
          <b className="text-neutral-300">외부 계약</b> <code className="font-mono">/api/ext/v1/*</code> — 외부 시스템용.
          <code className="font-mono">PIPER_API_TOKEN</code> Bearer 가 필요하고, 미설정이면 전체가 503 입니다.
        </p>
        <p className="text-amber-300/90">
          ⚠ <b>Swagger 에서 호출하면 실기가 움직입니다.</b> 추론·녹화·텔레옵을 시작하는 경로는
          heartbeat 의무를 함께 지는데 Swagger UI 는 heartbeat 를 보내지 않으므로,
          estopd 가 2.5초 뒤 그 프로세스를 정지시킵니다. 시작 계열은 화면이나 외부
          클라이언트로 부르고 <code className="font-mono">/docs</code> 는 조회·점검 위주로 쓰세요.
        </p>
      </div>
    </div>
  )
}
