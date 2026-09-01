import { Link } from 'react-router-dom'
import { useActivity } from '../hooks/useActivity'
import { useDeviceSummary } from '../hooks/useDeviceSummary'
import { ageText, useSystemStatus } from '../hooks/useSystemStatus'

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

function Bar({ pct, warn }: { pct: number; warn?: boolean }) {
  return (
    <div className="h-1.5 w-full rounded bg-neutral-700">
      <div className={`h-full rounded ${warn ? 'bg-amber-400' : 'bg-blue-500'}`}
           style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
    </div>
  )
}

export default function DashboardPage() {
  const { units, gateway, gpus, disks, estop } = useSystemStatus()
  const devices = useDeviceSummary()
  const { running, labelOf } = useActivity()

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
                </div>
              ))}
              {gpus.length === 0 && (
                <p className="text-sm text-neutral-500">GPU 없음 — 학습·추론은 못 돈다</p>
              )}
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
