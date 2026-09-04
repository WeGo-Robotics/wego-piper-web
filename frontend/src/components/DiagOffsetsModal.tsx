import { useEffect, useState } from 'react'
import { api } from '../services/api'
import { JOINTS } from '../config/joints'

/**
 * 검사 중심 오프셋 — **팔마다 사람이 정한다.**
 *
 * ⚠ **코드에 기본값을 둘 수 없다.** 걸리는 것은 팔에 붙은 기구물이지 관절이
 * 아니다. 실기에서 마스터 암에만 달린 기구물 때문에 joint5 가 아래에서 걸렸는데,
 * 한때 이걸 상수로 박아서 그 기구물이 없는 팔까지 중심을 옮길 뻔했다. 무엇이
 * 달려 있는지는 코드가 알 수 없고, 사람만 안다.
 *
 * 부호는 **관절 좌표 그대로**다. "위" 를 기구학으로 찾아 주지 않는다 — 직접
 * 맞추는 값이라 적은 대로 나와야 다음에 얼마를 고칠지 알 수 있다.
 */

const ARM = JOINTS.filter((j) => j.name !== 'gripper')

type Props = { iface: string; onClose: () => void; onSaved?: () => void }

export default function DiagOffsetsModal({ iface, onClose, onSaved }: Props) {
  const [vals, setVals] = useState<Record<string, string>>({})
  const [limit, setLimit] = useState(90)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    let live = true
    void (async () => {
      try {
        const d = await api.get<{ offsets: Record<string, number>; limit_deg: number }>(
          `/robots/diag/offsets/${iface}`)
        if (!live) return
        setLimit(d.limit_deg ?? 90)
        setVals(Object.fromEntries(ARM.map((j) =>
          [j.name, d.offsets?.[j.name] != null ? String(d.offsets[j.name]) : ''])))
      } catch { /* 못 읽으면 빈 값 — 안 옮기는 쪽이 안전하다 */ }
    })()
    return () => { live = false }
  }, [iface])

  const save = async () => {
    setBusy(true); setErr('')
    const offsets: Record<string, number> = {}
    for (const j of ARM) {
      const raw = (vals[j.name] ?? '').trim()
      if (!raw) continue
      const n = Number(raw)
      // ⚠ 숫자가 아닌 값을 0 으로 읽으면 **사람이 적은 오프셋이 조용히 사라진다**
      if (!Number.isFinite(n)) { setErr(`${j.label}: 숫자가 아닙니다 — "${raw}"`); setBusy(false); return }
      if (Math.abs(n) > limit) { setErr(`${j.label}: ±${limit}° 를 넘습니다`); setBusy(false); return }
      offsets[j.name] = n
    }
    try {
      await api.post('/robots/diag/offsets', { iface, offsets })
      onSaved?.(); onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '저장하지 못했습니다')
    } finally { setBusy(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}>
      <div className="w-full max-w-md rounded-lg border border-neutral-700 bg-neutral-900 p-4"
        onClick={(e) => e.stopPropagation()}>
        <h3 className="text-sm font-semibold text-neutral-100">검사 중심 오프셋 — {iface}</h3>
        <p className="mt-2 text-xs text-neutral-400">
          검사는 <b>지금 자세를 중심으로</b> 흔듭니다. 팔에 붙은 기구물에 걸리는
          관절은 여기서 중심을 옮기세요. 부호는 관절 좌표 그대로입니다 —
          <b> +30 이면 +30°</b>. 비워 두면 안 옮깁니다.
        </p>
        <p className="mt-1 text-xs text-amber-400/90">
          팔마다 따로 저장됩니다. 기구물은 그 팔에만 붙어 있으니까요.
        </p>

        <div className="mt-3 space-y-1.5">
          {ARM.map((j) => (
            <div key={j.name} className="flex items-center gap-2 text-xs">
              <span className="w-16 text-neutral-400">{j.label}</span>
              <input type="number" step={1} placeholder="0"
                value={vals[j.name] ?? ''}
                onChange={(e) => setVals((v) => ({ ...v, [j.name]: e.target.value }))}
                className="w-24 rounded border border-neutral-700 bg-neutral-950 px-2 py-1
                           text-right text-neutral-100 focus:border-blue-500 focus:outline-none" />
              <span className="text-neutral-500">도</span>
            </div>
          ))}
        </div>

        {err && <p className="mt-2 rounded border border-red-500/40 bg-red-500/10 px-2 py-1
                              text-xs text-red-300">{err}</p>}

        <div className="mt-4 flex justify-end gap-2">
          <button onClick={onClose}
            className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300 hover:bg-neutral-600">
            취소
          </button>
          <button onClick={() => void save()} disabled={busy}
            className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-500
                       disabled:opacity-50">
            {busy ? '…' : '저장'}
          </button>
        </div>
      </div>
    </div>
  )
}
