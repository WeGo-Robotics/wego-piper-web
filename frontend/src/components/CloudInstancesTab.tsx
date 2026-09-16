import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * 인스턴스 — **돈이 나가고 있는 것들** (feature/vast-training.md §6).
 *
 * ## 왜 띄우는 버튼보다 이게 먼저인가
 *
 * ⚠ 지금 원격 학습에는 자동 가드가 거의 없다. 예산 상한도 고아 스캐너도 아직
 * 배선 전이고, **사람이 유일한 가드다.** 그런데 그 사람이 볼 창구가 없었다 —
 * 무엇이 돌고 있는지 알려면 터미널에서 `vastai show instances` 를 쳐야 했다.
 * 끄는 쪽을 먼저 만든 것은 그래서다.
 *
 * ## ⚠ 고아는 숨기지 않는다
 *
 * 우리 라벨이 붙었는데 레지스트리가 모르는 것이 고아다 — 게이트웨이가 죽었다
 * 살아났거나 레코드를 잃으면 생긴다. **맨 위에 빨갛게** 둔다: 아무도 안 보는
 * 동안 요금이 나가는 것이 정확히 이것이다.
 */

type SSH = { host: string; port: number; user: string; key_path: string }
type Instance = {
  id: number
  label: string
  status: string
  gpu_name: string
  rate_usd_h: number
  ssh: SSH | null
  image: string
  message: string
  orphan: boolean
}

const money = (n: number) => `$${n.toFixed(4)}`

export default function CloudInstancesTab() {
  const [rows, setRows] = useState<Instance[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [confirming, setConfirming] = useState<Instance | null>(null)

  const load = useCallback(() => {
    setError(null)
    api.get<{ instances: Instance[]; orphans: number }>('/cloud/instances', { timeoutMs: 60_000 })
      .then((r) => setRows(r.instances))
      .catch((e) => { setRows([]); setError(e instanceof Error ? e.message : '불러오지 못했습니다') })
  }, [])

  useEffect(load, [load])

  const destroy = async (inst: Instance) => {
    setBusy(inst.id)
    setError(null)
    try {
      await api.delete(`/cloud/instances/${inst.id}`, { timeoutMs: 120_000 })
      setConfirming(null)
      load()
    } catch (e) {
      // ⚠ 실패를 조용히 넘기지 않는다. 확인 안 된 파기는 **과금이 계속된다**는 뜻이다.
      setError(e instanceof Error ? e.message : '파기하지 못했습니다')
    } finally {
      setBusy(null)
    }
  }

  const orphans = (rows ?? []).filter((r) => r.orphan)
  const total = (rows ?? []).reduce((s, r) => s + r.rate_usd_h, 0)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm text-neutral-400">
          {rows === null ? '불러오는 중…'
            : rows.length === 0 ? '도는 인스턴스가 없습니다'
            : <>인스턴스 <strong className="text-neutral-200">{rows.length}</strong>개 ·
               합계 <strong className="text-neutral-200">{money(total)}/h</strong></>}
        </span>
        <button onClick={load}
          className="ml-auto rounded bg-neutral-700 px-3 py-1.5 text-sm text-neutral-200 hover:bg-neutral-600">
          새로고침
        </button>
      </div>

      {error && (
        <p className="rounded border border-red-700/50 bg-red-950/30 p-3 text-sm text-red-300">{error}</p>
      )}

      {orphans.length > 0 && (
        <p className="rounded border border-red-600/60 bg-red-950/40 p-3 text-sm text-red-200">
          ⚠ <strong>고아 {orphans.length}개</strong> — 우리 라벨이 붙었는데 이 게이트웨이가
          모르는 인스턴스입니다. 아무도 안 보는 동안 요금이 나가고 있을 수 있습니다.
          {' '}자동으로 끄지 않습니다: <strong>다른 기계의 학습일 수 있어서</strong>입니다.
          확인하고 직접 파기해 주세요.
        </p>
      )}

      {rows !== null && rows.length > 0 && (
        <div className="overflow-x-auto rounded border border-neutral-700">
          <table className="w-full text-xs">
            <thead className="bg-neutral-900">
              <tr className="text-left text-neutral-400">
                <th className="p-2 font-medium">상태</th>
                <th className="p-2 font-medium">GPU</th>
                <th className="p-2 font-medium">$/h</th>
                <th className="p-2 font-medium">라벨</th>
                <th className="p-2 font-medium">SSH</th>
                <th className="p-2 font-medium">진행</th>
                <th className="p-2" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className={`border-t border-neutral-700 ${r.orphan ? 'bg-red-950/30' : ''}`}>
                  <td className="whitespace-nowrap p-2">
                    <div className={r.status === 'running' ? 'text-green-300' : 'text-amber-300'}>{r.status}</div>
                    <div className="text-neutral-600">id {r.id}</div>
                  </td>
                  <td className="whitespace-nowrap p-2 text-neutral-200">{r.gpu_name}</td>
                  <td className="whitespace-nowrap p-2 font-semibold text-neutral-100">{money(r.rate_usd_h)}</td>
                  <td className="whitespace-nowrap p-2 text-neutral-400">
                    {r.label || '—'}
                    {r.orphan && <span className="ml-1 rounded bg-red-900/60 px-1 text-[11px] text-red-200">고아</span>}
                  </td>
                  <td className="whitespace-nowrap p-2 text-neutral-500">
                    {r.ssh ? `${r.ssh.host}:${r.ssh.port}` : '—'}
                  </td>
                  <td className="p-2 text-neutral-500">{r.message || '—'}</td>
                  <td className="whitespace-nowrap p-2">
                    <button onClick={() => setConfirming(r)} disabled={busy === r.id}
                      className="rounded border border-red-700/60 px-3 py-1 text-red-300 hover:border-red-500 disabled:opacity-50">
                      {busy === r.id ? '파기 중…' : '파기'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ⚠ 논블로킹 React 모달. `window.confirm` 은 heartbeat 를 막아 로컬 추론을
          E-stop 시킨다 (실제 사고 전례, §6). */}
      {confirming && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setConfirming(null)}>
          <div className="w-full max-w-md space-y-4 rounded-lg border border-red-500/40 bg-neutral-900 p-5"
            onClick={(e) => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-neutral-100">이 인스턴스를 파기합니다</h2>
            <dl className="space-y-1 text-sm">
              {[['기계', `${confirming.gpu_name} · id ${confirming.id}`],
                ['요금', `${money(confirming.rate_usd_h)}/h`],
                ['라벨', confirming.label || '—']].map(([k, v]) => (
                <div key={k} className="flex gap-3">
                  <dt className="w-14 shrink-0 text-neutral-500">{k}</dt>
                  <dd className="min-w-0 flex-1 text-neutral-200">{v}</dd>
                </div>
              ))}
            </dl>
            <p className="rounded border border-neutral-700 bg-neutral-800/60 p-2 text-xs text-neutral-400">
              되돌릴 수 없고 기계의 데이터는 사라집니다. 회수하지 않은 체크포인트가 있으면
              <strong className="text-neutral-300"> 지금 잃습니다</strong> — 푸시는 학습이
              끝날 때 한 번만 일어납니다.
            </p>
            <div className="flex justify-end gap-2">
              <button onClick={() => setConfirming(null)}
                className="rounded border border-neutral-600 px-4 py-1.5 text-sm text-neutral-300 hover:border-neutral-400">
                취소
              </button>
              <button onClick={() => void destroy(confirming)} disabled={busy !== null}
                className="rounded bg-red-700 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-50">
                파기
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
