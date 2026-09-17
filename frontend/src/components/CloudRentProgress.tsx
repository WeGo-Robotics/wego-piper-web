import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 임대 학습이 **지금 어디까지 왔나** (feature/vast-training.md §5·§6).
 *
 * ## ⚠ 이게 없으면 [빌리기]는 눌러 놓고 아무것도 못 본다
 *
 * 여태 `GET /api/cloud/rent` 를 보는 화면이 하나도 없었다. 학습 로그는 학습 페이지에,
 * 기계는 인스턴스 탭에 있었지만 **그 사이 단계**(만드는 중 · SSH 대기 · 회수 · 파기)는
 * 어디에도 안 나왔다. 6분씩 걸리는 구간이라 사람은 "먹통인가" 를 먼저 의심한다.
 *
 * ## ⚠ 파기됨 ≠ 끝남
 *
 * 기계를 끈 뒤에도 Hub 에서 가중치를 받는 구간이 남는다(`pulling`). 그 자리를 안
 * 보여 주면 화면이 "파기됨" 에서 멈춘 것처럼 보이고, 사람은 다 끝난 줄 알고 자리를
 * 뜬다 — 그리고 아직 안 온 가중치로 추론을 걸려다 그제서야 안다.
 */

type Retrieval = {
  state: 'idle' | 'checking' | 'rescuing' | 'waiting' | 'pulling' | 'done' | 'missing' | 'failed'
  repo_id: string; path: string; source: string; detail: string
}
type Job = {
  job_id: string; label: string; phase: string; instance_id: number | null; reason: string
  cost: { rate_usd_h: number; accrued_usd: number; budget_usd: number
    elapsed_h: number; max_hours: number }
}
type Status = { busy: boolean; job: Job | null; retrieval: Retrieval }

/** 단계 이름은 **백엔드의 `Phase` 값 그대로** 받는다 — 화면이 따로 지어내지 않는다. */
const PHASE: Record<string, string> = {
  searching: '기계 고르는 중', creating: '만드는 중', ssh_wait: '접속 기다리는 중',
  training: '학습 중', retrieving: '가중치 회수 중', destroying: '파기하는 중',
  destroyed: '파기됨', orphan: '파기 확인 실패',
}
const RETRIEVAL: Record<Retrieval['state'], string> = {
  idle: '', checking: 'Hub 확인 중', rescuing: '기계에서 직접 받는 중',
  waiting: '파기 뒤에 받습니다', pulling: 'Hub 에서 받는 중',
  done: '가중치 받음', missing: '받을 가중치가 없음', failed: '가중치 못 받음',
}

/** 아직 움직일 여지가 있나 — 멈춘 뒤에도 회수가 남아 있으면 계속 본다. */
const moving = (s: Status) =>
  s.busy || s.retrieval.state === 'pulling' || s.retrieval.state === 'checking'
  || s.retrieval.state === 'rescuing'

export default function CloudRentProgress() {
  const [st, setSt] = useState<Status | null>(null)

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout>

    const tick = async () => {
      try {
        const r = await api.get<Status>('/cloud/rent')
        if (!alive) return
        setSt(r)
        // ⚠ 도는 동안은 5초, 아니면 20초. 끝난 화면을 5초마다 새로 받을 이유가 없다 —
        //   요청이 쌓이면 다른 버튼이 조용히 안 먹는다(api.ts 의 단일비행과 같은 이유).
        timer = setTimeout(() => void tick(), moving(r) ? 5_000 : 20_000)
      } catch {
        if (alive) timer = setTimeout(() => void tick(), 20_000)
      }
    }
    void tick()
    return () => { alive = false; clearTimeout(timer) }
  }, [])

  if (!st?.job) return null
  const job = st.job
  // 번들과 백엔드가 한 배포에서 어긋날 수 있다 — 없는 자리를 읽고 화면이 통째로
  // 하얘지는 것보다, 회수 칸만 안 보이는 편이 낫다.
  const retrieval: Retrieval = st.retrieval ?? {
    state: 'idle', repo_id: '', path: '', source: '', detail: '' }
  const c = job.cost
  const done = !st.busy
  const bad = job.phase === 'orphan' || retrieval.state === 'failed'

  return (
    <section className={`rounded-lg border p-3 text-sm ${bad
      ? 'border-red-700/50 bg-red-950/30'
      : done ? 'border-neutral-700 bg-neutral-800/60' : 'border-blue-700/50 bg-blue-950/20'}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`font-medium ${bad ? 'text-red-300' : done ? 'text-neutral-300' : 'text-blue-200'}`}>
          {PHASE[job.phase] ?? job.phase}
        </span>
        {job.instance_id && <span className="text-neutral-500">기계 {job.instance_id}</span>}
        {/* ⚠ 누적 비용은 **우리 시계 × 요금**이다. 청구가 진실이고 이건 어림이다 */}
        <span className="text-neutral-400">
          약 ${c.accrued_usd.toFixed(3)} / ${c.budget_usd.toFixed(2)}
          <span className="text-neutral-600"> · {c.elapsed_h.toFixed(2)}h</span>
        </span>
        {retrieval.state !== 'idle' && (
          <span className={retrieval.state === 'failed' ? 'text-red-300'
            : retrieval.state === 'done' ? 'text-emerald-300' : 'text-neutral-300'}>
            {RETRIEVAL[retrieval.state]}
            {retrieval.repo_id && <span className="text-neutral-600"> · {retrieval.repo_id}</span>}
          </span>
        )}
      </div>
      {(job.reason || retrieval.detail) && (
        <p className="mt-1 text-xs text-neutral-400">
          {[job.reason, retrieval.detail].filter(Boolean).join(' · ')}
        </p>
      )}
    </section>
  )
}
