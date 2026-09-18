import { useCallback, useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * **어디서 돌릴 것인가** — 로컬이냐, 클라우드냐 (feature/vast-training.md §8 W4).
 *
 * ## ⚠ 여기서는 **빌리지 않는다**
 *
 * 기계를 만드는 것은 클라우드 페이지의 일이고, 여기서는 **이미 있는 것 중에 고르기만**
 * 한다. 빌리기가 두 곳에 있으면 "끄는 책임" 도 두 곳으로 갈라진다 — 한쪽은 끝나면
 * 끄고 한쪽은 안 끄는 식이 되면 아무도 규칙을 못 외운다.
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

type Instance = {
  id: number; label: string; status: string; gpu_name: string
  rate_usd_h: number; orphan: boolean
}

export type RentPick = { instance_id: number; fetch_checkpoints: boolean }

/** 사람이 일부러 빌린 기계의 라벨 — 클라우드 페이지가 이 접두어로 만든다. */
const BOX = 'piper-box-'

export default function TrainWhereForm({ where, onWhere, onPick, runner, disabledReason }: {
  where: 'here' | 'rent'
  onWhere: (w: 'here' | 'rent') => void
  onPick: (p: RentPick | null) => void
  /** 서버가 실제로 쓰는 러너(`/training/status`). `ssh` 면 [로컬]이 사내 박스로 간다. */
  runner: string
  disabledReason?: string
}) {
  const [rows, setRows] = useState<Instance[]>([])
  const [picked, setPicked] = useState<number | null>(null)
  // ⚠ 기본은 **끈 상태**다. 최종본만 오는 것이 싸고, 대부분은 그것이면 된다.
  const [ckpts, setCkpts] = useState(false)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  // ⚠ **학습을 걸 수 있는 것만** 고르게 한다. 한 묶음으로 도는 기계(`piper-<job>`)는
  //   자기 학습이 끝나면 스스로 파기되므로 여기에 끼면 안 된다.
  const usable = rows.filter((i) => i.label.startsWith(BOX) && i.status === 'running')
  const instance = usable.find((i) => i.id === picked) ?? usable[0] ?? null

  const load = useCallback(() => {
    if (where !== 'rent') return
    setLoading(true); setErr('')
    api.get<{ instances: Instance[] }>('/cloud/instances', { timeoutMs: 90_000 })
      .then((r) => setRows(r.instances))
      .catch((e) => setErr(e instanceof Error ? e.message : '인스턴스를 불러오지 못했습니다'))
      .finally(() => setLoading(false))
  }, [where])

  useEffect(load, [load])

  useEffect(() => {
    onPick(where === 'rent' && instance
      ? { instance_id: instance.id, fetch_checkpoints: ckpts } : null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [where, instance?.id, ckpts])

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
        {/* ⚠ 이 기계에 사내 SSH 박스가 설정돼 있으면 [로컬]은 거기로 간다 — 라벨만
            믿게 두지 않는다. 설정이 없으면(대부분) 이 줄은 안 뜬다. */}
        {where === 'here' && runner === 'ssh' && (
          <span className="text-xs text-amber-300/80">
            ⚠ 이 게이트웨이는 사내 서버(SSH)로 보냅니다 — 이 기계의 GPU 가 아닙니다
          </span>
        )}
      </div>

      {where === 'rent' && (
        <div className="space-y-2 rounded-lg border border-blue-800/40 bg-blue-950/10 p-3">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-xs text-neutral-400">
              빌려 둔 기계 {usable.length > 0 && <span className="text-neutral-600">· {usable.length}대</span>}
            </span>
            <button onClick={load} className="text-xs text-blue-400 hover:underline">새로고침</button>
            <a href="/cloud" className="ml-auto text-xs text-blue-400 hover:underline">
              클라우드 GPU 에서 빌리기 →
            </a>
          </div>

          {err && <p className="text-xs text-red-300">{err}</p>}
          {loading && <p className="text-xs text-neutral-500">불러오는 중…</p>}

          {!loading && usable.length === 0 && (
            /* ⚠ 여기서 빌리게 하지 않는다 — 빌리는 곳은 한 군데여야 한다. 대신 어디로
               가야 하는지 말한다. */
            <p className="text-xs text-neutral-400">
              빌려 둔 기계가 없습니다 — <a href="/cloud" className="text-blue-400 hover:underline">
              클라우드 GPU</a> 에서 먼저 빌리세요.
            </p>
          )}

          {usable.map((i) => (
            <label key={i.id}
              className={`flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 rounded border px-2 py-1.5 text-sm
                ${i.id === instance?.id ? 'border-blue-600 bg-blue-950/30' : 'border-neutral-700 hover:border-neutral-500'}`}>
              <input type="radio" name="instance" checked={i.id === instance?.id}
                onChange={() => setPicked(i.id)} className="accent-blue-500" />
              <span className="font-medium text-neutral-200">{i.gpu_name}</span>
              <span className="text-neutral-300">{money(i.rate_usd_h)}/h</span>
              <span className="text-xs text-neutral-500">{i.label} · id {i.id}</span>
            </label>
          ))}

          {/* ⚠ **Hub 로는 중간 체크포인트를 못 받는다.** `push_to_hub` 가 학습이 끝날 때
              한 번만 올려서, `save_freq` 를 아무리 잘게 줘도 Hub 에는 최종본뿐이다.
              중간 것은 기계 안에만 있고 파기하면 같이 사라진다 — 20K 학습에 5000마다
              저장했는데 하나만 돌아오는 것이 그 때문이다. */}
          {instance && (
            <label className="flex cursor-pointer items-start gap-2 text-xs text-neutral-300">
              <input type="checkbox" checked={ckpts} onChange={(e) => setCkpts(e.target.checked)}
                className="mt-0.5 accent-blue-500" />
              <span>
                중간 체크포인트도 받기
                <span className="text-neutral-500">
                  {' '}— 저장 주기마다 찍힌 것을 기계에서 직접 끌어옵니다. Hub 에는 최종본만
                  올라가므로, 안 받으면 기계를 파기할 때 같이 사라집니다.
                  한 벌 약 200MB · 전송비 $0.017/GB.
                </span>
              </span>
            </label>
          )}

          {instance && (
            /* ⚠ **끝나도 안 끈다**는 것을 여기서 말한다. 한 묶음([빌리기])과 반대라서,
               모르고 있으면 빈 기계가 밤새 돈다. */
            <p className="text-xs text-neutral-500">
              학습이 끝나도 이 기계는 <strong className="text-neutral-400">꺼지지 않습니다</strong> —
              {' '}{money(instance.rate_usd_h)}/h 가 계속 나갑니다. 다 쓰면 인스턴스 탭에서 파기하세요.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
