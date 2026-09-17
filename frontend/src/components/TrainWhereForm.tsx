import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * **어디서 돌릴 것인가** — 로컬이냐, 클라우드냐 (feature/vast-training.md §8 W4).
 *
 * ## ⚠ 학습 설정은 여기 없다
 *
 * 데이터셋·정책·스텝·배치는 **학습 페이지의 폼 그대로** 쓴다. 폼을 두 벌 만들면 반드시
 * 어긋나고, 실제로 그랬다 — RENT 탭이 자기 입력 둘만 들고 나머지는 서버 기본값으로
 * 돌렸다(§12-17). 여기가 정하는 것은 **기계와 상한**뿐이다.
 *
 * ## ⚠ 칸은 둘인데 "로컬" 이 늘 로컬은 아니다
 *
 * 러너는 서버 설정(`PIPER_TRAIN_SSH_HOST`)이 정하므로, 그게 채워진 기계에서는 [로컬]이
 * 사실 **사내 SSH 박스**로 간다. 선택지를 셋으로 늘리지는 않되(회차마다 러너를 고르는
 * 것은 서버가 아직 못 한다) **그 사실은 말한다** — 화면이 "로컬" 이라 적어 두고 실제로는
 * 다른 기계에서 도는 것이 제일 나쁘다.
 */

export type Offer = {
  id: number; gpu_name: string; num_gpus: number; hourly: number; disk_gb: number
  compute_cap: number | null; support: string; warnings: string[]
  geolocation: string; reliability: number; dlperf_per_dph: number
}
type Template = { id: number; name: string; hash_id: string; variant: string; cuda: string; description: string }
type Gpu = { name: string; support: string; offers_seen: number; cheapest_hourly: number | null }

export type RentPick = {
  offer_id: number; template_hash: string; disk_gb: number
  budget_usd: number; max_hours: number
}

export default function TrainWhereForm({ where, onWhere, onPick, runner, disabledReason }: {
  where: 'here' | 'rent'
  onWhere: (w: 'here' | 'rent') => void
  onPick: (p: RentPick | null) => void
  /** 서버가 실제로 쓰는 러너(`/training/status`). `ssh` 면 [로컬]이 사내 박스로 간다. */
  runner: string
  disabledReason?: string
}) {
  const [templates, setTemplates] = useState<Template[]>([])
  const [templateId, setTemplateId] = useState<number | null>(null)
  const [gpus, setGpus] = useState<Gpu[]>([])
  const [gpu, setGpu] = useState('RTX 4090')
  const [offers, setOffers] = useState<Offer[]>([])
  const [offerId, setOfferId] = useState<number | null>(null)
  const [budget, setBudget] = useState(10)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const template = templates.find((t) => t.id === templateId) ?? templates[0] ?? null
  const offer = offers.find((o) => o.id === offerId) ?? offers[0] ?? null

  useEffect(() => {
    if (where !== 'rent') return
    api.get<{ templates: Template[] }>('/cloud/readiness', { timeoutMs: 60_000 })
      // ⚠ 기본은 slim — 실측으로 full(14GB)은 같은 호스트에서 pull 도 못 끝냈다(§12-13)
      .then((r) => { setTemplates(r.templates); setTemplateId((c) => c ?? r.templates.find((t) => t.variant === 'slim')?.id ?? r.templates[0]?.id ?? null) })
      .catch(() => {})
  }, [where])

  useEffect(() => {
    if (where !== 'rent') return
    const p = new URLSearchParams({ disk_gb: '40' })
    if (template?.cuda) p.set('cuda', template.cuda)
    api.get<{ gpus: Gpu[] }>(`/cloud/gpus?${p}`, { timeoutMs: 60_000 })
      .then((r) => setGpus(r.gpus)).catch(() => {})
  }, [where, template?.cuda])

  const load = useCallback(() => {
    if (where !== 'rent') return
    const p = new URLSearchParams({ gpu, disk_gb: '40', limit: '8' })
    if (template?.cuda) p.set('cuda', template.cuda)
    setLoading(true); setErr('')
    api.get<{ offers: Offer[] }>(`/cloud/vast/offers?${p}`, { timeoutMs: 90_000 })
      .then((r) => { setOffers(r.offers); setOfferId((c) => r.offers.some((o) => o.id === c) ? c : r.offers[0]?.id ?? null) })
      .catch((e) => setErr(e instanceof Error ? e.message : '오퍼를 불러오지 못했습니다'))
      .finally(() => setLoading(false))
  }, [where, gpu, template?.cuda])

  useEffect(() => { const t = setTimeout(load, 300); return () => clearTimeout(t) }, [load])

  // ⚠ 고른 것을 **위로 올린다.** 여기서 직접 시작하지 않는다 — 시작 버튼은 하나여야 하고,
  //   그 버튼이 학습 설정을 들고 있다.
  useEffect(() => {
    if (where !== 'rent' || !offer || !template) { onPick(null); return }
    onPick({
      offer_id: offer.id, template_hash: template.hash_id, disk_gb: offer.disk_gb,
      budget_usd: budget,
      // ⚠ 상한 둘을 **항상 같이 보낸다** — 예산만 있으면 느린 기계에서 시간이 무한이다
      max_hours: Math.max(0.1, budget / offer.hourly),
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [where, offer?.id, template?.hash_id, budget])

  const money = (n: number) => `$${n.toFixed(n < 1 ? 4 : 2)}`

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-xs text-neutral-400">실행 위치</span>
        <div className="flex overflow-hidden rounded-lg border border-neutral-700 text-sm">
          {([['here', '로컬'], ['rent', '클라우드']] as const).map(([k, label]) => (
            <button key={k} onClick={() => onWhere(k)}
              disabled={k === 'rent' && !!disabledReason}
              title={k === 'rent' ? disabledReason ?? '' : ''}
              className={`px-3 py-1.5 ${where === k
                ? 'bg-neutral-700 text-white' : 'bg-neutral-900 text-neutral-400 hover:text-white'}
                disabled:cursor-not-allowed disabled:opacity-40`}>
              {label}
            </button>
          ))}
        </div>
        {where === 'rent' && (
          <span className="text-xs text-neutral-500">
            끝나면 자동으로 파기됩니다 · 가중치는 저장소에서 자동으로 받아옵니다
          </span>
        )}
        {/* ⚠ 이 기계에 사내 SSH 박스가 설정돼 있으면 [로컬]은 거기로 간다 — 라벨만
            믿게 두지 않는다. 설정이 없으면(대부분) 이 줄은 안 뜬다. */}
        {where === 'here' && runner === 'ssh' && (
          <span className="text-xs text-amber-300/80">
            ⚠ 이 게이트웨이는 사내 서버(SSH)로 보냅니다 — 이 기계의 GPU 가 아닙니다
          </span>
        )}
      </div>

      {where === 'rent' && (
        <div className="space-y-3 rounded-lg border border-blue-800/40 bg-blue-950/10 p-3">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">템플릿</span>
              <select value={template?.id ?? ''} onChange={(e) => setTemplateId(Number(e.target.value))}
                className="rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm">
                {templates.map((t) => (
                  <option key={t.id} value={t.id} title={t.description}>
                    {t.variant || t.name}{t.cuda ? ` · ${t.cuda}` : ''}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">GPU</span>
              <select value={gpu} onChange={(e) => setGpu(e.target.value)}
                className="max-w-[16rem] rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm">
                {/* ⚠ 못 도는 기종을 **숨기지 않는다** — 없는 셈 치면 "왜 안 보이지" 가 된다 */}
                {gpus.map((g) => (
                  <option key={g.name} value={g.name} disabled={g.support !== 'ok'}>
                    {g.name}{g.cheapest_hourly ? ` · ${money(g.cheapest_hourly)}/h` : ''}
                    {g.support === 'too_new' ? ' (이 이미지에 커널 없음)'
                      : g.support === 'too_old' ? ' (너무 구형)' : ''}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">예산 상한 $</span>
              <input type="number" min={1} value={budget}
                onChange={(e) => setBudget(Math.max(1, Number(e.target.value) || 10))}
                className="w-20 rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm" />
            </label>
            {offer && (
              <span className="text-xs text-neutral-400">
                {money(offer.hourly)}/h → 약 {(budget / offer.hourly).toFixed(1)}시간
              </span>
            )}
          </div>

          {err && <p className="text-xs text-red-300">{err}</p>}
          {loading && <p className="text-xs text-neutral-500">기계를 찾는 중…</p>}

          <div className="space-y-1">
            {offers.map((o) => (
              <label key={o.id}
                className={`flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 rounded border px-2 py-1.5 text-sm
                  ${o.id === offer?.id ? 'border-blue-600 bg-blue-950/30' : 'border-neutral-700 hover:border-neutral-500'}`}>
                <input type="radio" name="offer" checked={o.id === offer?.id}
                  onChange={() => setOfferId(o.id)} className="accent-blue-500" />
                <span className="font-medium text-neutral-200">{o.num_gpus}× {o.gpu_name}</span>
                <span className="text-neutral-300">{money(o.hourly)}/h</span>
                <span className="text-xs text-neutral-500">{o.geolocation} · 신뢰 {(o.reliability * 100).toFixed(1)}%</span>
                {/* ⚠ 경고를 **고르는 자리에** 띄운다. 확인 창에만 있으면 이미 마음을 정한 뒤다 */}
                {o.warnings.map((w) => (
                  <span key={w} className="w-full text-xs text-amber-300">⚠ {w}</span>
                ))}
              </label>
            ))}
            {!loading && offers.length === 0 && (
              <p className="text-xs text-neutral-500">조건에 맞는 기계가 없습니다 — GPU 나 템플릿을 바꿔 보세요.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
