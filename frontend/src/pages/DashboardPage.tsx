import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../services/api'
import { useActivity } from '../hooks/useActivity'
import { useDeviceSummary } from '../hooks/useDeviceSummary'
import { ageText, TREND_WINDOW_S, useSystemStatus } from '../hooks/useSystemStatus'
import type { TrendSample } from '../hooks/useSystemStatus'

/**
 * 대시보드 — **"지금 괜찮은가, 아니면 어디가 문제인가"** 하나에만 답한다.
 *
 * ## 왜 메뉴 카드를 걷어냈나
 *
 * 예전 대시보드는 사이드바와 **똑같은 목적지**를 카드로 반복했다. 이동 수단이
 * 둘일 이유가 없고, 그 자리에 상태를 놓으면 클릭 없이 답이 보인다.
 *
 * ## 무엇을 넣을지 어떻게 골랐나
 *
 * 항목을 지어내지 않고 **이 시스템이 실제로 고장난 방식**에서 뽑았다:
 * 유닛이 옛 코드로 도는 것(하루에 두 번 겪었다), USB 컨트롤러가 죽어 팔·카메라가
 * 통째로 사라지는 것, 배타 모드 때문에 시작이 막히는 것, GPU 경합, 디스크.
 */

function Dot({ ok, warn }: { ok: boolean; warn?: boolean }) {
  const c = warn ? 'bg-amber-400' : ok ? 'bg-green-400' : 'bg-neutral-600'
  return <span className={`inline-block w-2 h-2 rounded-full ${c}`} />
}

function Card({ title, to, children }: {
  title: string; to?: string; children: React.ReactNode
}) {
  return (
    <section className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-neutral-300">{title}</h2>
        {to && <Link to={to} className="text-xs text-neutral-500 hover:text-blue-400">자세히 →</Link>}
      </div>
      {children}
    </section>
  )
}

type Series = {
  label: string
  /** 선 색 (svg stroke). 범례 막대에도 그대로 쓴다. */
  color: string
  pick: (s: TrendSample) => number | null
}

/**
 * 최근 15분 추이 — 2D 라인 차트 (x=시간, y=값).
 *
 * "지금 몇 %"(막대)는 순간이고, 문제는 **추세**에서 보인다 — GPU 메모리 선이
 * 슬금슬금 오르면 곧 OOM 이고, fps 가 흘러내리면 카메라·USB 문제의 전조다.
 *
 * 견본은 **서버가 쌓는다**(`useSystemStatus` 참고) — 페이지를 연 순간 이미
 * 15분치가 있다. x 축 오른끝은 마지막 견본의 t 다. 클라이언트 시계를 쓰면
 * 서버와 시각이 어긋난 만큼 그래프가 통째로 밀린다.
 *
 * ⚠ 인라인 SVG 로 그린다. Plotly 는 청크가 4.6MB 라 첫 화면인 대시보드에
 *   실을 물건이 아니다.
 */
function TrendChart({ samples, series, yMax = 100, caption }: {
  samples: TrendSample[]; series: Series[]; yMax?: number; caption: string
}) {
  const W = 400
  const H = 80
  // 점 하나로는 선이 안 된다 — 견본이 쌓이면 나타난다
  if (samples.length < 2) return null
  const t1 = samples[samples.length - 1].t
  const t0 = t1 - TREND_WINDOW_S
  const x = (t: number) => ((t - t0) / TREND_WINDOW_S) * W
  const y = (v: number) => H - (Math.min(yMax, Math.max(0, v)) / yMax) * H
  const path = (pick: Series['pick']) => {
    let d = ''
    let prevT: number | null = null
    for (const s of samples) {
      if (s.t < t0) continue
      const v = pick(s)
      if (v == null) { prevT = null; continue }
      // 견본(4초)이 세 번 넘게 빈 구간은 잇지 않는다 — 게이트웨이가 내려갔거나
      // 값이 없던(추론 정지 등) 시간이고, 이어 그리면 그동안 평온했다는 거짓말이 된다
      const move = prevT === null || s.t - prevT > 12
      d += `${move ? 'M' : 'L'}${x(s.t).toFixed(1)},${y(v).toFixed(1)}`
      prevT = s.t
    }
    return d
  }
  return (
    <div className="mt-2">
      <div className="flex items-center justify-between text-[10px] text-neutral-500">
        <span>{caption}</span>
        <span className="flex gap-2.5">
          {series.map((sr) => (
            <span key={sr.label} className="flex items-center gap-1">
              <i className="inline-block h-0.5 w-3" style={{ background: sr.color }}
                 aria-hidden /> {sr.label}
            </span>
          ))}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           className="mt-1 h-20 w-full rounded border border-neutral-700/60 bg-neutral-900/60"
           role="img" aria-label={`${series.map((sr) => sr.label).join('·')} 최근 15분 추이`}>
        <line x1={0} y1={H / 2} x2={W} y2={H / 2} stroke="#404040"
              strokeDasharray="3 5" strokeWidth={1} vectorEffect="non-scaling-stroke" />
        {series.map((sr) => (
          <path key={sr.label} d={path(sr.pick)} fill="none" stroke={sr.color}
                strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
        ))}
      </svg>
    </div>
  )
}

function Bar({ pct, warn }: { pct: number; warn?: boolean }) {
  return (
    <div className="h-1.5 w-full rounded bg-neutral-700">
      <div className={`h-full rounded ${warn ? 'bg-amber-400' : 'bg-blue-500'}`}
           style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
    </div>
  )
}

/**
 * HF 로그인은 **업로드 준비 상태**다 (feature/hf-account.md §3). 미로그인이어도
 * 녹화·학습은 시작되는데 **결과를 못 보낸다** — 학습 몇 시간 뒤 업로드 단계에서
 * 아는 게 최악이라, 시작 전에 보이는 자리(대시보드)에 둔다. 계정 관리는 설정에 있다.
 */
type HfStatus = {
  logged_in: boolean; username: string; fullname?: string
  avatar_url?: string; token_name?: string; token_role?: string
}

export default function DashboardPage() {
  const { units, gateway, gpus, disks, estop, cpu, samples } = useSystemStatus()

  // 추론 fps 차트 — 창 안에 값이 하나라도 있어야 그린다. 방금 멈춘 추론의
  // 꼬리도 보인다 — "왜 멈췄나"를 볼 때 정확히 필요한 구간이다.
  const hasFps = samples.some((s) => s.fps != null)
  const fpsMax = Math.max(10,
    Math.ceil(Math.max(...samples.map((s) => s.fps ?? 0)) / 10) * 10)
  const devices = useDeviceSummary()
  const { running, labelOf } = useActivity()

  const [hf, setHf] = useState<HfStatus | null>(null)
  useEffect(() => {
    // 실패하면 null 로 둔다 — 게이트웨이가 안 닿는 것과 "미로그인"은 다른 말이다
    api.get<HfStatus>('/hub/whoami').then(setHf).catch(() => {})
  }, [])
  // "read" 토큰도 whoami 는 성공한다 — 로그인돼 보이는데 업로드에서만 실패하는
  // 상태라 따로 갈라 둔다. fine-grained 는 role 이 달라 "read" 일 때만 경고한다.
  const hfReadOnly = !!hf?.logged_in && hf.token_role === 'read'

  // ── 맨 위 한 줄이 이 화면의 요점이다 ──
  // 평소엔 여기만 보고 지나가고, 문제가 있을 때만 아래를 본다.
  const problems: string[] = []
  const staleNames = [
    ...units.filter((u) => u.stale).map((u) => u.name),
    ...(gateway?.stale ? ['게이트웨이'] : []),
  ]
  if (staleNames.length) problems.push(`옛 코드로 돎: ${staleNames.join(', ')} — 재시작 필요`)
  const down = units.filter((u) => !u.active).map((u) => u.name)
  if (down.length) problems.push(`멈춤: ${down.join(', ')}`)
  if (devices.alerts > 0) problems.push(`장치 경보 ${devices.alerts}건`)
  // ⚠ 디스크는 **남은 용량**으로 본다. 사용률 90% 라도 1TB 가 남았으면 문제가 아니다.
  for (const d of disks) if (d.free_gb < 20) problems.push(`디스크 ${d.free_gb}GB 남음`)
  if (hf && !hf.logged_in) problems.push('HF 미로그인 — 녹화·학습은 되지만 Hub 업로드 불가')
  if (hfReadOnly) problems.push('HF 토큰이 읽기 전용 — 학습 끝 업로드에서 실패')

  return (
    <div className="space-y-4">
      <div className={`rounded-lg border px-4 py-3 ${
        problems.length
          ? 'border-amber-600/50 bg-amber-950/30'
          : 'border-green-700/40 bg-green-950/20'}`}>
        <div className="flex items-start gap-3">
          <Dot ok={!problems.length} warn={!!problems.length} />
          {problems.length === 0 ? (
            <p className="text-sm text-neutral-300">정상 — 문제 없음</p>
          ) : (
            <ul className="space-y-0.5 text-sm text-amber-200">
              {problems.map((p) => <li key={p}>{p}</li>)}
            </ul>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* ── 왼쪽: 지금 무슨 일이 일어나는가 ── */}
        <div className="space-y-4">
          <Card title="지금 돌고 있는 것">
            {running.length === 0 ? (
              <p className="text-sm text-neutral-500">없음 — 유휴</p>
            ) : (
              <ul className="space-y-1">
                {running.map((a) => (
                  <li key={a} className="flex items-center gap-2 text-sm">
                    <Dot ok /> <span className="text-neutral-200">{labelOf(a)}</span>
                  </li>
                ))}
              </ul>
            )}
            {hasFps && (
              <TrendChart samples={samples} yMax={fpsMax}
                          caption={`최근 15분 · 0–${fpsMax}fps`}
                          series={[{ label: '추론 fps', color: '#34d399',
                                     pick: (s) => s.fps }]} />
            )}
          </Card>

          <Card title="서비스" to="/settings">
            <ul className="space-y-1.5 text-sm">
              {gateway && (
                <li className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-2">
                    <Dot ok warn={gateway.stale} />
                    <span className="text-neutral-200">게이트웨이</span>
                  </span>
                  <span className={gateway.stale ? 'text-amber-400' : 'text-neutral-500'}>
                    {gateway.stale ? '낡음' : `${ageText(gateway.age_s)} 전`}
                  </span>
                </li>
              )}
              {units.map((u) => (
                <li key={u.name} className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-2">
                    <Dot ok={u.active} warn={u.stale} />
                    <span className="text-neutral-200">{u.name}</span>
                  </span>
                  <span className={u.stale ? 'text-amber-400' : 'text-neutral-500'}>
                    {!u.active ? '멈춤' : u.stale ? '낡음' : `${ageText(u.age_s)} 전`}
                  </span>
                </li>
              ))}
              {units.length === 0 && <li className="text-neutral-500">불러오는 중…</li>}
            </ul>
          </Card>

          <Card title="HuggingFace" to="/settings">
            {!hf ? (
              <p className="text-sm text-neutral-500">불러오는 중…</p>
            ) : !hf.logged_in ? (
              <p className="flex items-center gap-2 text-sm">
                <Dot ok={false} />
                <span className="text-neutral-400">미로그인 — Hub 업로드 불가</span>
              </p>
            ) : (
              <div className="flex items-center gap-3">
                {hf.avatar_url && (
                  <img src={hf.avatar_url} alt="" className="h-8 w-8 rounded-full" />
                )}
                <div className="min-w-0 flex-1 text-sm">
                  <p className="truncate text-neutral-200">{hf.fullname || hf.username}</p>
                  <p className="truncate text-xs text-neutral-400">
                    {hf.username}
                    {/* ⚠ 토큰은 **이름만** 적는다. 값을 되돌려 보내는 화면은
                        그 자체가 자격증명 유출 경로다 (feature/hf-account.md §1) */}
                    {hf.token_name && ` · 토큰 ${hf.token_name}`}
                    {hf.token_role === 'write' && ' (쓰기)'}
                    {hfReadOnly && (
                      <span className="text-amber-400"> (읽기 전용 — 업로드 불가)</span>
                    )}
                  </p>
                </div>
                <Dot ok warn={hfReadOnly} />
              </div>
            )}
          </Card>
        </div>

        {/* ── 오른쪽: 무엇을 쓸 수 있는가 ── */}
        <div className="space-y-4">
          <Card title="하드웨어" to="/robots">
            <ul className="space-y-1.5 text-sm">
              <li className="flex items-center justify-between">
                <span className="flex items-center gap-2">
                  <Dot ok={devices.robots.ok > 0} warn={devices.robots.warn > 0} />
                  <span className="text-neutral-200">팔</span>
                </span>
                <span className="text-neutral-400">
                  {devices.robots.ok}개{devices.robots.warn > 0 && ` · 경보 ${devices.robots.warn}`}
                </span>
              </li>
              <li className="flex items-center justify-between">
                <span className="flex items-center gap-2">
                  <Dot ok={devices.cameras.ok > 0} warn={devices.cameras.warn > 0} />
                  <span className="text-neutral-200">카메라</span>
                </span>
                <span className="text-neutral-400">
                  {devices.cameras.ok}개{devices.cameras.warn > 0 && ` · 경보 ${devices.cameras.warn}`}
                </span>
              </li>
              <li className="flex items-center justify-between">
                <span className="flex items-center gap-2">
                  {/* ⚠ E-stop 은 **버스가 있어야** 의미가 있다. 버스가 없으면
                      "정상" 이 아니라 "모름" 이다 — 안전 표시가 거짓말하면 안 된다. */}
                  <Dot ok={!!estop?.bus_available} warn={!!estop && !estop.bus_available} />
                  <span className="text-neutral-200">E-stop</span>
                </span>
                <span className="text-neutral-400">
                  {!estop ? '…' : !estop.bus_available ? '버스 없음'
                    : estop.armed ? '작동됨' : '정상'}
                </span>
              </li>
            </ul>
          </Card>

          <Card title="자원">
            <div className="space-y-3">
              {gpus.map((g) => (
                <div key={g.name}>
                  <div className="mb-1 flex items-baseline justify-between text-sm">
                    <span className="text-neutral-200">{g.name.replace(/^NVIDIA\s+/, '')}</span>
                    <span className="text-neutral-400">
                      {g.util_pct ?? '?'}%
                      {g.mem_total_mb != null && ` · ${Math.round((g.mem_used_mb ?? 0) / 1024)}/${Math.round(g.mem_total_mb / 1024)}GB`}
                      {g.temp_c != null && ` · ${g.temp_c}°C`}
                    </span>
                  </div>
                  <Bar pct={g.util_pct ?? 0} />
                  <TrendChart samples={samples} caption="최근 15분 · 0–100%"
                              series={[
                                { label: '사용률', color: '#3b82f6',
                                  pick: (s) => s.gpus.find((x) => x.name === g.name)?.util_pct ?? null },
                                { label: '메모리', color: '#a78bfa',
                                  pick: (s) => s.gpus.find((x) => x.name === g.name)?.mem_pct ?? null },
                              ]} />
                </div>
              ))}
              {gpus.length === 0 && (
                <p className="text-sm text-neutral-500">GPU 없음 — 학습·추론은 못 돈다</p>
              )}
              <div>
                <div className="mb-1 flex items-baseline justify-between text-sm">
                  <span className="text-neutral-200">CPU</span>
                  <span className="text-neutral-400">{cpu != null ? `${cpu}%` : '?'}</span>
                </div>
                <Bar pct={cpu ?? 0} />
                <TrendChart samples={samples} caption="최근 15분 · 0–100%"
                            series={[{ label: '사용률', color: '#3b82f6',
                                       pick: (s) => s.cpu_pct }]} />
              </div>
              {disks.map((d) => (
                <div key={d.path}>
                  <div className="mb-1 flex items-baseline justify-between text-sm">
                    <span className="truncate text-neutral-200" title={d.path}>디스크</span>
                    <span className={d.free_gb < 20 ? 'text-amber-400' : 'text-neutral-400'}>
                      {d.free_gb}GB 남음 / {d.total_gb}GB
                    </span>
                  </div>
                  <Bar pct={d.used_pct ?? 0} warn={d.free_gb < 20} />
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
