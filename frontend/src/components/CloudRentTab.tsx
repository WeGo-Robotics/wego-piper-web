import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../services/api'

/**
 * RENT — 빌릴 기계를 고른다 (feature/vast-training.md §9-2).
 *
 * ## ⚠ 화면에 쓰는 가격은 `dph_total` 이 아니다
 *
 * Vast 가 표시하는 `dph_total` 에 섞인 디스크는 **검색 기본값(약 5GB)** 이고 우리
 * 템플릿은 40GB 를 쓴다. 서버가 요청한 디스크로 다시 계산한 값이 `hourly` 다 —
 * 표에는 그것만 쓴다. 호스트마다 `storage_cost` 가 크게 벌어져 일률적인 보정으로는
 * 못 맞추므로, 두 값을 나란히 보여 어디서 차이가 났는지 읽히게 한다.
 *
 * ## ⚠ 준비도는 목록을 막지 않는다
 *
 * 세팅하기 전에 가격부터 보고 싶은 게 사람이다. 막는 것은 [빌리기] 하나뿐이고,
 * 무엇이 모자란지와 설정으로 가는 길을 같이 보여 준다.
 *
 * ## ⚠ [빌리기]가 켜진 이유
 *
 * 버튼을 고쳐서가 아니라 **상한이 셋 다 생겼기 때문**이다: 학습 스크립트의
 * `timeout`(가장 안쪽) · 예산/시간 틱(바깥) · `finally` 로 보장된 파기. 그전까지
 * 비활성이던 것은 디자인이 아니라 사실이었다 — 끄는 코드가 없었다.
 */

type Offer = {
  id: number; machine_id: number; gpu_name: string; num_gpus: number; gpu_ram_gb: number
  hourly: number; dph_base: number; storage_hourly: number; disk_gb: number; dph_total: number
  inet_down_cost_per_gb: number; inet_up_cost_per_gb: number
  inet_down_mbps: number; inet_up_mbps: number
  cuda_max_good: number; reliability: number; dlperf: number; dlperf_per_dph: number
  cpu_cores: number; cpu_ram_gb: number; disk_space_gb: number; disk_bw: number
  geolocation: string; duration_days: number; verified: boolean; rentable: boolean
  compute_cap: number | null; support: 'ok' | 'too_old' | 'too_new' | 'unknown'
  warnings: string[]
}
type Template = {
  id: number; name: string; image: string; tag: string | null
  disk_gb: number; description: string; variant: string
  /**
   * 이 템플릿 이미지의 CUDA 빌드(`cu126`·`cu128`). ⚠ **GPU 호환 판정이 이걸 따라간다** —
   * 두 빌드는 담고 있는 커널이 다르고 한쪽이 다른 쪽의 상위집합이 아니다
   * (실측: cu128 은 Blackwell 을 얻는 대신 맥스웰·파스칼·**V100** 을 잃는다).
   */
  cuda: string
  /** ⚠ create 가 쓰는 값. 템플릿을 고칠 때마다 바뀌므로 화면에 박지 않는다(§12-7). */
  hash_id: string
}
type Check = { ok: boolean; detail: string | null }
type Readiness = {
  checks: Record<string, Check>
  ready: boolean
  credit: number | null
  templates: Template[]
}

/**
 * 빌릴 수 있는 GPU 한 기종.
 *
 * ⚠ **목록을 코드에 박지 않는다.** 예전엔 7개를 박아 뒀는데 사용자가 쓰려는 3060 이
 * 없었고, 대신 RTX 5090 이 들어 있었다 — Vast 에서 가장 흔한 GPU 지만 sm_120 이라
 * 우리 cu126 이미지로는 **애초에 못 돈다**. 박아 둔 목록은 이렇게 조용히 틀린다.
 */
type Gpu = {
  name: string
  compute_cap: number | null
  support: 'ok' | 'too_old' | 'too_new' | 'unknown'
  vram_gb: number
  /** ⚠ 확정 수량이 아니라 **이번에 본 수**다 — 새로고침마다 흔들린다 */
  offers_seen: number
  min_hourly: number
  /** 지금 필터로 실제 기계가 있나. false 면 골라도 표가 빈다 */
  available: boolean
  /** 없으면 왜 없는지 */
  reason: string | null
}

/**
 * 선택지 묶음. ⚠ 고를 수 없는 것도 **숨기지 않는다** — 사용자가 "싹 다 보여줘" 라고
 * 했고, 목록에서 빼 버리면 왜 안 보이는지 알 길이 없다. 대신 왜 못 고르는지를 적는다.
 */
const GROUP_AVAILABLE = '고를 수 있습니다'
const GROUP_UNAVAILABLE = '지금 조건으로는 기계가 없습니다'

const CHECK_LABEL: Record<string, string> = {
  cli: 'vastai CLI', api_key: 'API 키', ssh_key: 'SSH 키', template: '학습 템플릿',
}
type SortKey = 'value' | 'price' | 'cuda' | 'reliability' | 'cpu' | 'inet'

const money = (n: number) => `$${n.toFixed(3)}`
/** 소수는 읽는 사람이 자릿수를 세지 않게 짧게 — 1024 는 1.0k */
const num = (n: number, d = 0) => n.toLocaleString(undefined, { maximumFractionDigits: d })

export default function CloudRentTab() {
  const [ready, setReady] = useState<Readiness | null>(null)
  const [offers, setOffers] = useState<Offer[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 필터 — 기본값은 템플릿의 extra_filters 그대로(§2 에서 실물로 확인된 것)
  // '' = 전체(제약 없음). 사용자가 "싹 다 보여줘" 라고 했다
  const [gpu, setGpu] = useState('RTX 4090')
  const [gpus, setGpus] = useState<Gpu[]>([])
  const [gpusDetail, setGpusDetail] = useState<string | null>(null)
  const [gpusLoading, setGpusLoading] = useState(false)
  const [diskGb, setDiskGb] = useState(40)
  const [maxPrice, setMaxPrice] = useState<string>('')
  const [minCpu, setMinCpu] = useState<string>('')
  const [sort, setSort] = useState<SortKey>('value')

  const [picked, setPicked] = useState<number | null>(null)
  const [templateId, setTemplateId] = useState<number | null>(null)
  const [budget, setBudget] = useState(10)
  // ⚠ 회수 경로다. 비면 서버가 400 으로 거절한다 — 학습은 멀쩡히 끝나고
  //   가중치만 사라지는 것을 막기 위해서다.
  const [dataset, setDataset] = useState('')
  const [repo, setRepo] = useState('')
  // ⚠ **화면에 없던 값들이다.** 여태 [빌리기]는 `steps=5000 · batch=8 · amp=bf16` 으로
  //   돌면서 그 숫자를 어디에도 보여 주지 않았다 — 예산 상한에는 "화면이 보여 준 숫자와
  //   실제로 걸리는 숫자가 다르면 그 화면은 거짓말이다" 라는 규칙을 적어 두고, 정작
  //   학습 설정에는 안 지키고 있었다.
  const [policyType, setPolicyType] = useState('act')
  const [steps, setSteps] = useState(5000)
  const [batchSize, setBatchSize] = useState(8)
  const [saveFreq, setSaveFreq] = useState(1000)
  const [amp, setAmp] = useState('bf16')
  const [confirming, setConfirming] = useState(false)

  const templates = ready?.templates ?? []
  const template = templates.find((t) => t.id === templateId) ?? templates[0] ?? null
  const offer = offers.find((o) => o.id === picked) ?? null

  const loadReadiness = useCallback(() => {
    api.get<Readiness>('/cloud/readiness', { timeoutMs: 60_000 })
      .then((r) => {
        setReady(r)
        // ⚠ **기본은 slim 이다 — 실측으로 바꿨다**(§12-13, 2026-09-17).
        //
        //   같은 4090 호스트(5.9Gbps)에서:
        //     full 14GB  → 8분 상한에 걸릴 때까지 pull 을 못 끝냈다 (실패)
        //     slim 1.3GB → pull 16초 · SSH 62초 · 스택 준비 4분 23초 → 완주
        //
        //   full 의 **가장 좋았던 기록**(6분 20초)과 비교해도 slim 이 빠르다. 예전
        //   주석은 "네트워크가 빠르면 full 이 낫다" 였는데, 정작 네트워크가 빠른
        //   호스트에서 14GB 가 안 끝났다 — 회선이 아니라 크기가 문제였다.
        //
        // ⚠ slim 이 되는 것은 **스택 준비 게이트가 생긴 뒤부터다**(`wait_for_stack`).
        //   그전에는 접속되자마자 학습을 걸어 `No module named 'lerobot'` 로 죽었다.
        setTemplateId((cur) => cur ?? r.templates.find((t) => t.variant === 'slim')?.id ?? r.templates[0]?.id ?? null)
      })
      .catch(() => setReady(null))
  }, [])

  const loadOffers = useCallback(async (refresh = false) => {
    setLoading(true)
    setError(null)
    const p = new URLSearchParams({ gpu, disk_gb: String(diskGb), limit: '50' })
    if (template?.cuda) p.set('cuda', template.cuda)
    if (maxPrice.trim()) p.set('max_price', maxPrice.trim())
    if (minCpu.trim()) p.set('min_cpu', minCpu.trim())
    if (refresh) p.set('refresh', 'true')
    try {
      // ⚠ 오래 걸린다(실측 2~4초, 느린 날은 그 이상). 기본 시간 제한으로는 모자랄 수 있다.
      const r = await api.get<{ offers: Offer[]; query: string }>(
        `/cloud/vast/offers?${p}`, { timeoutMs: 60_000 })
      setOffers(r.offers)
      setQuery(r.query)
      setPicked((cur) => (r.offers.some((o) => o.id === cur) ? cur : null))
    } catch (e) {
      setOffers([])
      setError(e instanceof Error ? e.message : '오퍼를 불러오지 못했습니다')
    } finally {
      setLoading(false)
    }
    // ⚠ **고른 템플릿의 CUDA 빌드를 같이 보낸다.** 안 보내면 서버가 기본값으로
    //   판정해서, cu128 템플릿을 골라도 표는 cu126 기준으로 말한다 — 조용히 틀린다.
  }, [gpu, diskGb, maxPrice, minCpu, template?.cuda])

  const loadGpus = useCallback(() => {
    // ⚠ **표와 같은 필터를 넘긴다.** 카탈로그가 자기 조건으로 만들어지면 "선택지엔
    //   있는데 표는 0행" 이 된다. 유일한 차이는 GPU 이름 항이 없다는 것뿐이다.
    // ⚠ 서버 실측 4.8초(질의 둘, 서버가 10분 캐시). **오퍼 목록과 따로 간다** —
    //   이게 늦는다고 표가 안 떠서는 안 된다.
    const p = new URLSearchParams({ disk_gb: String(diskGb) })
    if (template?.cuda) p.set('cuda', template.cuda)
    if (maxPrice.trim()) p.set('max_price', maxPrice.trim())
    if (minCpu.trim()) p.set('min_cpu', minCpu.trim())
    setGpusLoading(true)
    api.get<{ gpus: Gpu[]; detail: string | null }>(`/cloud/gpus?${p}`, { timeoutMs: 60_000 })
      .then((r) => { setGpus(r.gpus); setGpusDetail(r.detail) })
      .catch((e) => setGpusDetail(e instanceof Error ? e.message : '기종 목록을 불러오지 못했습니다'))
      .finally(() => setGpusLoading(false))
  }, [diskGb, maxPrice, minCpu, template?.cuda])

  useEffect(loadReadiness, [loadReadiness])
  useEffect(() => {
    // ⚠ 오퍼(400ms)보다 느긋하게 — 질의 둘이라 비싸다. 숫자 칸은 한 글자마다 바뀐다.
    const t = setTimeout(loadGpus, 900)
    return () => clearTimeout(t)
  }, [loadGpus])

  // ⚠ 자동 폴링은 없다 — 가격은 초 단위로 변하지 않고 호출은 비싸다(실측 2~4초).
  //   필터가 바뀔 때만 부르되 **디바운스한다**: 숫자 칸은 한 글자마다 바뀌므로
  //   "40" 을 치면 4 → 40 으로 두 번, 지우고 고치면 더 난다. 이 저장소는 이미
  //   요청이 쌓여 정지 버튼까지 안 나간 적이 있다(ERR_INSUFFICIENT_RESOURCES).
  useEffect(() => {
    const t = setTimeout(() => { void loadOffers() }, 400)
    return () => clearTimeout(t)
  }, [loadOffers])

  /**
   * 고를 수 있는 기종.
   *
   * ⚠ **합집합이다.** 카탈로그 조회가 실패하거나 아직 안 왔어도 선택기가 비면 안 된다 —
   * 지금 표에 보이는 기종과, 지금 고른 것을 반드시 포함시킨다. 그래야 목록이 죽어도
   * 화면은 산다(그래서 서버에 하드코딩 씨앗을 두지 않았다).
   */
  const gpuOptions = useMemo(() => {
    const by = new Map<string, Gpu>()
    for (const g of gpus) by.set(g.name, g)
    // 카탈로그가 늦거나 죽어도 선택기가 비면 안 된다 — 지금 표에 보이는 기종을 합친다
    for (const o of offers) {
      if (!by.has(o.gpu_name)) {
        by.set(o.gpu_name, { name: o.gpu_name, compute_cap: o.compute_cap, support: o.support,
                             vram_gb: o.gpu_ram_gb, offers_seen: 0, min_hourly: o.hourly,
                             available: true, reason: null })
      }
    }
    // 지금 고른 것은 무슨 일이 있어도 목록에 남는다 — 사라지면 선택이 튄다
    if (gpu && !by.has(gpu)) {
      by.set(gpu, { name: gpu, compute_cap: null, support: 'unknown', vram_gb: 0,
                    offers_seen: 0, min_hourly: 0, available: true, reason: null })
    }
    const all = [...by.values()]
    const cheap = (a: Gpu, b: Gpu) => (a.min_hourly || 9e9) - (b.min_hourly || 9e9)
    return [
      [GROUP_AVAILABLE, all.filter((g) => g.available).sort(cheap)] as const,
      [GROUP_UNAVAILABLE, all.filter((g) => !g.available).sort(cheap)] as const,
    ].filter(([, items]) => items.length > 0)
  }, [gpus, offers, gpu])

  const pickedGpu = useMemo(
    () => gpuOptions.flatMap(([, items]) => items).find((g) => g.name === gpu) ?? null,
    [gpuOptions, gpu])

  const sorted = useMemo(() => {
    const by: Record<SortKey, (o: Offer) => number> = {
      value: (o) => -o.dlperf_per_dph, price: (o) => o.hourly, cuda: (o) => -o.cuda_max_good,
      reliability: (o) => -o.reliability, cpu: (o) => -o.cpu_cores, inet: (o) => -o.inet_down_mbps,
    }
    return [...offers].sort((a, b) => by[sort](a) - by[sort](b))
  }, [offers, sort])

  /**
   * 결과가 0건인 **이유**.
   *
   * ⚠ 그냥 "없습니다" 로 두면 검색이 고장난 것처럼 읽힌다. 실측: RTX 5090 은 시장에
   * 46대 있는데 우리 필터의 `cuda_vers>=12.4` 가 **전부** 거른다 — Vast 의 `cuda_vers`
   * 는 드라이버 최대치가 아니라 "그 기계가 실제로 돌릴 수 있는 CUDA" 라서 sm_120
   * 기계는 12.8 부터만 잡힌다. 사유는 서버가 카탈로그에 실어 준다.
   */
  const emptyReason = useMemo(() => {
    if (pickedGpu?.reason) {
      return pickedGpu.support === 'too_new'
        ? `${pickedGpu.name} — ${pickedGpu.reason} 다른 기종을 고르거나 이미지를 다시 구워야 합니다.`
        : `${pickedGpu.name} — ${pickedGpu.reason}`
    }
    return '조건에 맞는 기계가 없습니다 — 필터를 넓혀 보세요.'
  }, [pickedGpu])

  const missing = Object.entries(ready?.checks ?? {}).filter(([, c]) => !c.ok)
  const credit = ready?.credit ?? null
  const maxHours = offer && credit ? credit / offer.hourly : null
  const budgetHours = offer ? budget / offer.hourly : null

  return (
    <div className="space-y-4 pb-40">
      {/* 준비도 — ⚠ 목록을 막지 않는다. 무엇이 모자란지만 말한다 */}
      {ready && !ready.ready && (
        <section className="rounded-lg border border-amber-600/40 bg-amber-950/30 p-3 text-sm">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="font-medium text-amber-200">빌리려면 먼저:</span>
            {missing.map(([k, c]) => (
              <span key={k} className="text-amber-100/80">
                <strong>{CHECK_LABEL[k] ?? k}</strong>
                {c.detail ? ` — ${c.detail}` : ''}
              </span>
            ))}
            <a href="/settings" className="ml-auto shrink-0 underline text-amber-200 hover:text-amber-100">
              설정 → 클라우드
            </a>
          </div>
          <p className="mt-1 text-xs text-amber-200/60">목록은 그대로 볼 수 있습니다 — 막히는 것은 [빌리기] 뿐입니다.</p>
        </section>
      )}

      {/* 필터 */}
      <section className="flex flex-wrap items-end gap-3 rounded-lg border border-neutral-700 bg-neutral-800 p-3">
        <label className="flex min-w-0 flex-col gap-1">
          <span className="text-xs text-neutral-400">
            GPU{gpus.length > 0 && <span className="text-neutral-600"> · {gpus.length}종</span>}
            {gpusLoading && <span className="text-neutral-600"> · 갱신 중…</span>}
          </span>
          {/* ⚠ 80종이다. 묶음으로 나누지 않으면 고를 수가 없고, 못 도는 것을 빼 버리면
              왜 안 보이는지 알 길이 없다 — 그래서 묶어서 **다** 보여준다. */}
          <select value={gpu} onChange={(e) => setGpu(e.target.value)}
            className="w-[22rem] min-w-0 max-w-full rounded border border-neutral-600 bg-neutral-900 px-2 py-1.5 text-sm">
            <option value="">전체 (제한 없음)</option>
            {gpuOptions.map(([group, items]) => (
              <optgroup key={group} label={`${group} — ${items.length}종`}>
                {/* ⚠ 라벨은 **이름이 맨 앞**이다. native select 는 접두 일치로 type-ahead 가
                    되므로 포커스한 채 "rtx 30" 을 치면 그 구간으로 점프한다 — 앞에 VRAM·
                    가격을 붙이는 순간 70종에서 찾을 방법이 사라진다. 순서를 바꾸지 마라. */}
                {items.map((g) => (
                  <option key={g.name} value={g.name}>
                    {g.name}
                    {g.vram_gb > 0 && ` · ${num(g.vram_gb)}GB`}
                    {/* ⚠ "20대" 가 아니라 "20대쯤" 이다 — 같은 질의를 두 번 돌려도
                        행이 갈린다(실측 234~238). 확정값처럼 적으면 새로고침마다 흔들린다 */}
                    {g.offers_seen > 0 && ` · ${g.offers_seen}대쯤`}
                    {g.min_hourly > 0 && ` · ${money(g.min_hourly)}~`}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <label className="flex w-28 flex-col gap-1">
          {/* ⚠ 필터이자 **가격 입력**이다 — 바꾸면 표 전체의 $/h 가 다시 계산된다 */}
          <span className="text-xs text-neutral-400">디스크 GB</span>
          <input type="number" min={5} max={2000} value={diskGb}
            onChange={(e) => setDiskGb(Math.max(5, Number(e.target.value) || 40))}
            className="w-full rounded border border-neutral-600 bg-neutral-900 px-2 py-1.5 text-sm" />
        </label>
        <label className="flex w-28 flex-col gap-1">
          <span className="text-xs text-neutral-400">최대 $/h</span>
          <input type="number" step="0.05" min={0} placeholder="제한 없음" value={maxPrice}
            onChange={(e) => setMaxPrice(e.target.value)}
            className="w-full rounded border border-neutral-600 bg-neutral-900 px-2 py-1.5 text-sm" />
        </label>
        <label className="flex w-28 flex-col gap-1">
          <span className="text-xs text-neutral-400">최소 CPU 코어</span>
          <input type="number" min={0} placeholder="제한 없음" value={minCpu}
            onChange={(e) => setMinCpu(e.target.value)}
            className="w-full rounded border border-neutral-600 bg-neutral-900 px-2 py-1.5 text-sm" />
        </label>
        <label className="flex min-w-0 flex-col gap-1">
          <span className="text-xs text-neutral-400">정렬</span>
          <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)}
            className="min-w-0 rounded border border-neutral-600 bg-neutral-900 px-2 py-1.5 text-sm">
            <option value="value">가성비</option>
            <option value="price">낮은 가격</option>
            <option value="cuda">CUDA</option>
            <option value="reliability">신뢰도</option>
            <option value="cpu">CPU</option>
            <option value="inet">네트워크</option>
          </select>
        </label>
        <button onClick={() => void loadOffers(true)} disabled={loading}
          className="ml-auto rounded bg-neutral-700 px-4 py-1.5 text-sm text-neutral-200 hover:bg-neutral-600 disabled:opacity-50">
          {loading ? '찾는 중…' : '새로고침'}
        </button>
      </section>

      {/* ⚠ **조용히 빈 표를 주지 않는다.** 실측: `cuda_vers>=12.4` 하나가 Blackwell
          전체를 걸러낸다(RTX 5090 은 시장에 46대인데 필터로는 0대). 이유를 안 적으면
          사용자는 화면이 고장난 줄 안다. 막지는 않는다 — cu128 로 다시 구우면 풀리는
          문제고, 그건 사용자가 정할 일이다. */}
      {pickedGpu && !pickedGpu.available && (
        <p className="rounded border border-amber-700/40 bg-amber-950/30 p-2 text-xs text-amber-200">
          ⚠ <strong>{pickedGpu.name}</strong> — {pickedGpu.reason}
          {pickedGpu.support === 'too_new'
            && ' (이미지를 cu128 로 다시 구우면 쓸 수 있습니다: TORCH_CUDA=cu128 ./deploy/build-train.sh)'}
        </p>
      )}

      {gpusDetail && (
        <p className="flex flex-wrap items-center gap-2 text-xs text-neutral-500">
          <span>기종 목록: {gpusDetail} — 지금 결과에 보이는 기종만 고를 수 있습니다.</span>
          {/* ⚠ 자동 재시도는 넣지 않는다. 이 저장소는 요청이 쌓여 정지 버튼까지
              안 나간 적이 있다 — 다시 시도는 사람이 누른다. */}
          <button onClick={loadGpus} disabled={gpusLoading}
            className="rounded border border-neutral-600 px-2 py-0.5 text-neutral-300 hover:border-neutral-400 disabled:opacity-50">
            다시 시도
          </button>
        </p>
      )}

      {query && (
        <p className="text-xs text-neutral-600">
          질의 <code className="text-neutral-500">{query}</code>
          {credit !== null && <> · 크레딧 <strong className="text-neutral-400">${credit.toFixed(2)}</strong></>}
        </p>
      )}

      {error && (
        <p className="rounded border border-red-700/50 bg-red-950/30 p-3 text-sm text-red-300">{error}</p>
      )}

      {/* 표 — ⚠ 열이 많다. 가로 스크롤은 이 상자 안에서만 생긴다 */}
      <div className="overflow-x-auto rounded border border-neutral-700">
        <table className="w-full text-xs">
          <thead className="bg-neutral-900">
            <tr className="text-left text-neutral-400">
              <th className="p-2 font-medium">GPU</th>
              <th className="p-2 font-medium">$/h</th>
              <th className="p-2 font-medium">가성비</th>
              <th className="p-2 font-medium">CUDA</th>
              <th className="p-2 font-medium">신뢰도</th>
              <th className="p-2 font-medium">네트워크</th>
              <th className="p-2 font-medium">CPU</th>
              <th className="p-2 font-medium">디스크</th>
              <th className="p-2 font-medium">위치</th>
              <th className="p-2 font-medium">잔여</th>
              <th className="p-2" />
            </tr>
          </thead>
          <tbody>
            {sorted.map((o) => (
              <tr key={o.id}
                onClick={() => setPicked(o.id)}
                className={`cursor-pointer border-t border-neutral-700 hover:bg-neutral-800/60 ${
                  picked === o.id ? 'bg-blue-950/40' : ''}`}>
                <td className="whitespace-nowrap p-2">
                  <div className="font-medium text-neutral-200">{o.num_gpus}× {o.gpu_name}</div>
                  <div className="text-neutral-500">{num(o.gpu_ram_gb, 0)}GB VRAM</div>
                </td>
                <td className="whitespace-nowrap p-2">
                  {/* ⚠ 굵은 값이 우리 가격. 아래에 출처를 쪼개 둔다 — Vast 표시가와 다른 이유가 여기 있다 */}
                  <div className="font-semibold text-neutral-100">{money(o.hourly)}</div>
                  <div className="text-neutral-500">
                    GPU {money(o.dph_base)} + 디스크 {money(o.storage_hourly)}
                  </div>
                </td>
                <td className="p-2 text-neutral-300">{num(o.dlperf_per_dph)}</td>
                <td className="p-2 text-neutral-300">{o.cuda_max_good}</td>
                <td className="p-2 text-neutral-300">{(o.reliability * 100).toFixed(1)}%</td>
                <td className="whitespace-nowrap p-2 text-neutral-400">
                  ↓{num(o.inet_down_mbps)}  ↑{num(o.inet_up_mbps)}
                </td>
                <td className="p-2 text-neutral-300">{num(o.cpu_cores, 1)}</td>
                <td className="whitespace-nowrap p-2 text-neutral-400">{num(o.disk_space_gb)}GB</td>
                <td className="whitespace-nowrap p-2 text-neutral-400">{o.geolocation}</td>
                <td className="whitespace-nowrap p-2 text-neutral-500">{num(o.duration_days)}일</td>
                <td className="p-2">
                  {o.warnings.length > 0 && (
                    <div className="flex flex-col gap-1">
                      {o.warnings.map((w) => (
                        <span key={w} className="whitespace-nowrap rounded bg-amber-900/50 px-1.5 py-0.5 text-[11px] text-amber-200">
                          ⚠ {w}
                        </span>
                      ))}
                    </div>
                  )}
                </td>
              </tr>
            ))}
            {!loading && sorted.length === 0 && !error && (
              <tr><td colSpan={11} className="p-6 text-center text-sm text-neutral-500">
                {emptyReason}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* 고른 것 — 하단 고정 요약 바 */}
      {offer && (
        <div className="fixed bottom-0 left-0 right-0 z-40 border-t border-neutral-700 bg-neutral-900/95 px-4 py-3 backdrop-blur">
          <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-x-4 gap-y-2 text-sm">
            <div className="min-w-0">
              <div className="font-medium text-neutral-100">{offer.num_gpus}× {offer.gpu_name}</div>
              <div className="text-xs text-neutral-500">{offer.geolocation} · id {offer.id}</div>
            </div>

            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">템플릿</span>
              <select value={template?.id ?? ''} onChange={(e) => setTemplateId(Number(e.target.value))}
                className="min-w-0 rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm">
                {/* ⚠ 설명을 버리지 않는다. 백엔드가 "스택 포함, 부팅 즉시 학습" /
                    "부팅 때 bootstrap.sh 가 스택 설치" 를 이미 주고 있는데 화면은
                    `full`·`slim` 네 글자만 보여 주고 있었다 — 그 둘의 차이가 임대
                    시간과 요금을 가르는데 고르는 사람은 알 길이 없었다. */}
                {templates.map((t) => (
                  <option key={t.id} value={t.id} title={t.description}>
                    {t.variant || t.name}{t.description ? ` — ${t.description}` : ''}
                  </option>
                ))}
              </select>
            </label>

            <div className="min-w-0">
              <div className="font-semibold text-neutral-100">{money(offer.hourly)}/h</div>
              {/* ⚠ 전송비는 시간이 아니라 GB 로 붙는다 — 시간당에 섞지 않는다 */}
              <div className="text-xs text-neutral-500">
                + 전송 ${offer.inet_down_cost_per_gb.toFixed(4)}/GB 올림 · ${offer.inet_up_cost_per_gb.toFixed(4)}/GB 내림
              </div>
            </div>

            {/* ⚠ **회수 경로다.** 저장소가 비면 서버가 거절한다 — 없으면 학습은
                멀쩡히 끝나고 가중치만 사라진다. 그리고 푸시는 종료 시점 한 번뿐이라
                그 한 번이 유일한 기회다(§12-4). */}
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">데이터셋</span>
              <input value={dataset} onChange={(e) => setDataset(e.target.value)}
                placeholder="wego-hansu/sim_data2"
                className="w-52 rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm" />
            </label>
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">가중치 저장소</span>
              <input value={repo} onChange={(e) => setRepo(e.target.value)}
                placeholder="wego-hansu/my-act"
                className="w-52 rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm" />
            </label>

            {/* ⚠ 학습 설정 — **보여 주고 고칠 수 있게.** 전에는 서버 기본값이
                조용히 들어갔다. 학습 페이지의 폼을 통째로 옮기지는 않는다(폼이 둘이
                되면 반드시 어긋난다) — 비용과 결과를 가르는 것만 낸다. */}
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">정책</span>
              <select value={policyType} onChange={(e) => setPolicyType(e.target.value)}
                className="rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm">
                {['act', 'smolvla', 'diffusion', 'pi0'].map((t) => <option key={t}>{t}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">스텝</span>
              <input type="number" min={1} step={100} value={steps}
                onChange={(e) => setSteps(Math.max(1, Number(e.target.value) || 1))}
                className="w-24 rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm" />
            </label>
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">배치</span>
              <input type="number" min={1} value={batchSize}
                onChange={(e) => setBatchSize(Math.max(1, Number(e.target.value) || 1))}
                className="w-16 rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm" />
            </label>
            {/* ⚠ 체크포인트 주기는 **회수 보험과 직결된다** — 중간에 죽었을 때
                `scp` 로 가져올 것이 있으려면 이 주기 안에 한 번은 저장돼 있어야 한다(§5). */}
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">저장 주기</span>
              <input type="number" min={1} step={100} value={saveFreq}
                onChange={(e) => setSaveFreq(Math.max(1, Number(e.target.value) || 1))}
                className="w-20 rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm" />
            </label>
            {/* ⚠ 고른 기계가 cc 8.0 미만이면 bf16 은 **에뮬레이션으로 느려진다**(§12-15).
                경고만 띄우고 바꿀 방법을 안 주면 그 경고는 반쪽이다. */}
            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">AMP</span>
              <select value={amp} onChange={(e) => setAmp(e.target.value)}
                className="rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm">
                {['bf16', 'fp16', 'off'].map((t) => <option key={t}>{t}</option>)}
              </select>
            </label>

            <label className="flex items-center gap-2">
              <span className="text-xs text-neutral-400">예산 상한 $</span>
              <input type="number" min={1} value={budget}
                onChange={(e) => setBudget(Math.max(1, Number(e.target.value) || 10))}
                className="w-20 rounded border border-neutral-600 bg-neutral-800 px-2 py-1 text-sm" />
            </label>

            <div className="text-xs text-neutral-400">
              {credit !== null && <>크레딧 ${credit.toFixed(2)} → 최대 <strong>{maxHours?.toFixed(0)}시간</strong> · </>}
              예산 ${budget} → <strong>{budgetHours?.toFixed(1)}시간</strong>
            </div>

            <div className="ml-auto flex gap-2">
              <button onClick={() => setPicked(null)}
                className="rounded border border-neutral-600 px-3 py-1.5 text-sm text-neutral-300 hover:border-neutral-400">
                해제
              </button>
              <button onClick={() => setConfirming(true)}
                disabled={!ready?.ready || !dataset.trim() || !repo.trim()}
                title={!ready?.ready ? '설정 → 클라우드 에서 준비를 마쳐 주세요'
                  : (!dataset.trim() || !repo.trim())
                    ? '데이터셋과 가중치 저장소를 채워 주세요 — 없으면 결과를 가져올 수 없습니다'
                    : ''}
                className="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-40">
                빌리기
              </button>
            </div>
          </div>
        </div>
      )}

      {confirming && offer && (
        <RentConfirm offer={offer} template={template} budget={budget} credit={credit}
          dataset={dataset} repo={repo}
          train={{ policyType, steps, batchSize, saveFreq, amp }}
          onStarted={() => { setConfirming(false); setPicked(null) }}
          onClose={() => setConfirming(false)} />
      )}
    </div>
  )
}

/**
 * 확인 — **논블로킹 React 모달**.
 *
 * ⚠ `window.confirm` 을 쓰면 안 된다. 브라우저 모달이 heartbeat 를 막아 로컬 추론이
 * E-stop 으로 죽는다 (실제 사고 전례, §6).
 */
type TrainOpts = {
  policyType: string; steps: number; batchSize: number; saveFreq: number; amp: string
}

function RentConfirm({ offer, template, budget, credit, dataset, repo, train, onClose, onStarted }: {
  offer: Offer; template: Template | null; budget: number; credit: number | null
  dataset: string; repo: string; train: TrainOpts
  onClose: () => void; onStarted: () => void
}) {
  const hours = budget / offer.hourly
  const [sending, setSending] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [command, setCommand] = useState('')

  // ⚠ **무엇이 돌지 그대로 보여 준다.** 서버가 [빌리기]와 **같은 함수**로 만든 명령이라
  //   화면과 실제가 갈릴 수 없다. 숫자를 여기서 베껴 적으면 언젠가 어긋난다.
  const body = {
    offer_id: offer.id,
    template_hash: template?.hash_id ?? '',
    disk_gb: offer.disk_gb,
    budget_usd: budget,
    max_hours: Math.max(0.1, budget / offer.hourly),
    dataset_repo_id: dataset,
    policy_repo_id: repo,
    policy_type: train.policyType,
    steps: train.steps,
    batch_size: train.batchSize,
    save_freq: train.saveFreq,
    amp: train.amp,
  }

  useEffect(() => {
    api.post<{ command: string }>('/cloud/rent/preview', body)
      .then((r) => setCommand(r.command))
      .catch(() => setCommand(''))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataset, repo, train.policyType, train.steps, train.batchSize, train.saveFreq])

  const rent = async () => {
    setSending(true)
    setErr(null)
    try {
      // ⚠ **미리보기와 같은 본문을 보낸다.** 다른 것을 보내면 위에 보여 준 명령이
      //   그 순간 거짓말이 된다.
      await api.post('/cloud/rent', body, { timeoutMs: 120_000 })
      onStarted()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '빌리지 못했습니다')
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="max-h-[90vh] w-full max-w-lg space-y-4 overflow-y-auto rounded-lg border border-neutral-600 bg-neutral-900 p-5"
        onClick={(e) => e.stopPropagation()}>
        <h2 className="text-lg font-semibold text-neutral-100">이 기계를 빌립니다</h2>

        <dl className="space-y-1.5 text-sm">
          {[
            ['기계', `${offer.num_gpus}× ${offer.gpu_name} · ${offer.geolocation} · id ${offer.id}`],
            ['템플릿', template ? `${template.variant || template.name} — ${template.tag ?? ''}` : '없음'],
            ['디스크', `${offer.disk_gb}GB`],
            ['시간당', `${money(offer.hourly)} (GPU ${money(offer.dph_base)} + 디스크 ${money(offer.storage_hourly)})`],
            ['전송비', `올림 $${offer.inet_down_cost_per_gb.toFixed(4)}/GB · 내림 $${offer.inet_up_cost_per_gb.toFixed(4)}/GB`],
            ['학습', `${train.policyType} · ${train.steps.toLocaleString()}스텝 · 배치 ${train.batchSize} · `
                     + `저장 ${train.saveFreq.toLocaleString()}스텝마다 · AMP ${train.amp}`],
            ['데이터셋', dataset || '(비었습니다)'],
            ['가중치 저장소', repo || '(비었습니다)'],
            ['예산 상한', `$${budget} → 약 ${hours.toFixed(1)}시간`],
            ['크레딧', credit !== null ? `$${credit.toFixed(2)}` : '모름'],
          ].map(([k, v]) => (
            <div key={k} className="flex gap-3">
              <dt className="w-20 shrink-0 text-neutral-500">{k}</dt>
              <dd className="min-w-0 flex-1 text-neutral-200">{v}</dd>
            </div>
          ))}
        </dl>

        {command && (
          <div>
            <p className="mb-1 text-xs text-neutral-500">실제로 도는 명령</p>
            {/* ⚠ 가로 스크롤을 자기 안에서 먹는다 — 창 전체가 밀리면 버튼이 사라진다 */}
            <pre className="overflow-x-auto rounded border border-neutral-700 bg-neutral-950 p-2
                            text-[11px] leading-relaxed text-neutral-300">{command}</pre>
          </div>
        )}

        {offer.warnings.length > 0 && (
          <ul className="space-y-1 rounded border border-amber-700/40 bg-amber-950/30 p-2 text-xs text-amber-200">
            {offer.warnings.map((w) => <li key={w}>⚠ {w}</li>)}
          </ul>
        )}

        {/* ⚠ 이 버튼이 켜진 것은 버튼을 고쳐서가 아니라 **상한이 셋 다 생겼기**
            때문이다: 학습 스크립트의 timeout(가장 안쪽) · 예산/시간 틱(바깥) ·
            finally 로 보장된 파기. 그전까지 비활성이던 것은 디자인이 아니라 사실이었다. */}
        <p className="rounded border border-neutral-700 bg-neutral-800/60 p-2 text-xs text-neutral-400">
          끝나면 <strong className="text-neutral-300">자동으로 파기</strong>됩니다 —
          완주든 실패든 예산 초과든 같은 길입니다. 파기를 확인하지 못하면 인스턴스 탭에
          빨간 배너로 남습니다. 학습 자체에도 상한이 걸려, 이 게이트웨이가 죽어도
          기계가 영원히 도는 일은 없습니다.
        </p>

        {err && (
          <p className="rounded border border-red-700/50 bg-red-950/30 p-2 text-xs text-red-300">{err}</p>
        )}

        <div className="flex justify-end gap-2">
          <button onClick={onClose} disabled={sending}
            className="rounded border border-neutral-600 px-4 py-1.5 text-sm text-neutral-300 hover:border-neutral-400 disabled:opacity-50">
            닫기
          </button>
          <button onClick={() => void rent()} disabled={sending || !template}
            title={template ? '' : '학습 템플릿이 필요합니다'}
            className="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-40">
            {sending ? '빌리는 중…' : '빌리기'}
          </button>
        </div>
      </div>
    </div>
  )
}
