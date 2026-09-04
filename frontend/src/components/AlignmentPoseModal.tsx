import { useCallback, useEffect, useRef, useState } from 'react'
import ManualControlPanel from './ManualControlPanel'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'
import { JOINTS } from '../config/joints'

/**
 * 정렬 자세 만들기 — **조그로 실제 팔을 움직여 자세를 정한다**
 * (feature/alignment-check.md).
 *
 * ⚠ **자세는 손으로 적는 것이 아니다.** 숫자를 적게 하면 오타 하나가 팔을
 * 엉뚱한 곳으로 보낸다. 여기서는 팔을 실제로 움직여 눈으로 보고 저장한다.
 *
 * ⚠ **태그가 보이는지 여기서 보여준다.** 안 보이는 자세로 검사를 만들면
 * 실행할 때가 되어서야 "태그가 안 보입니다" 를 만나는데, 그때는 그 자세가 왜
 * 그렇게 정해졌는지도 잊은 뒤다. 카메라 영상과 검출된 태그 ID 를 같이 둔다.
 *
 * ⚠ 열려 있는 동안 **조그 세션이 명령 경로를 점유**한다 — 추론·녹화가 막힌다.
 * 그래서 닫을 때 반드시 놓는다(성공하든 실패하든).
 */

const ARM = JOINTS.filter((j) => j.name !== 'gripper')

type Props = {
  iface: string
  cameraId: string
  tagMm: number
  family: string
  onClose: () => void
  onSaved: (name: string) => void
}

export default function AlignmentPoseModal({
  iface, cameraId, tagMm, family, onClose, onSaved,
}: Props) {
  const { notify } = useSystemMessage()
  const [name, setName] = useState('')
  const [joints, setJoints] = useState<number[]>([])
  const [tags, setTags] = useState<number[]>([])
  const [tagErr, setTagErr] = useState('')
  const [ready, setReady] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const live = useRef(true)

  // ── 조그 세션: 열 때 잡고 닫을 때 반드시 놓는다 ──
  useEffect(() => {
    live.current = true
    void (async () => {
      try {
        await api.post('/robots/jog/start', { iface })
        if (live.current) setReady(true)
      } catch (e) {
        if (live.current) setErr(e instanceof Error ? e.message : '조그를 열지 못했습니다')
      }
    })()
    return () => {
      live.current = false
      // ⚠ 실패해도 삼킨다 — 닫는 길이 막히면 세션이 영영 남는다
      void api.post('/robots/jog/stop', {}).catch(() => {})
    }
  }, [iface])

  // ── 팔이 실제로 어디 있나 ──
  useEffect(() => {
    const tick = () => {
      api.get<Record<string, number>>(`/robots/parking/joints/${iface}`)
        // ⚠ `actionKey` 가 아니라 `name` 이다 — 이 엔드포인트는 평문 관절 이름이다
        .then((d) => live.current && setJoints(JOINTS.map((j) => d[j.name] ?? 0)))
        .catch(() => {})
    }
    tick()
    const h = setInterval(tick, 500)
    return () => clearInterval(h)
  }, [iface])

  // ── 지금 이 카메라에 무엇이 보이나 ──
  useEffect(() => {
    if (!cameraId) return
    const tick = () => {
      api.get<{ tags: number[]; error?: string }>(
        `/alignment/tags/${encodeURIComponent(cameraId)}?tag_mm=${tagMm}&family=${family}`)
        .then((d) => {
          if (!live.current) return
          setTags(d.tags ?? []); setTagErr(d.error ?? '')
        })
        .catch(() => {})
    }
    tick()
    const h = setInterval(tick, 1000)
    return () => clearInterval(h)
  }, [cameraId, tagMm, family])

  const send = useCallback((values: Record<string, number>) => {
    // ⚠ `actionKey`(`joint1.pos`) 로 오므로 관절 이름으로 바꾼다
    const out: Record<string, number> = {}
    JOINTS.forEach((j) => { out[j.name] = values[j.actionKey] ?? 0 })
    void api.post('/robots/jog/goal', { iface, values: out }).catch((e) => {
      setErr(e instanceof Error ? e.message : '목표를 보내지 못했습니다')
    })
  }, [iface])

  const save = async () => {
    const n = name.trim()
    if (!n) { setErr('이름을 적으세요'); return }
    setBusy(true); setErr('')
    try {
      await api.post('/alignment/poses', { name: n, iface })
      notify({ level: 'info', source: '정렬', text: `자세 '${n}' 저장` })
      onSaved(n)
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '저장하지 못했습니다')
    } finally { setBusy(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="max-h-full w-full max-w-4xl overflow-y-auto rounded-lg border
                      border-neutral-700 bg-neutral-900 p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-neutral-100">
            정렬 자세 만들기 — {iface}
          </h3>
          <span className="text-xs text-amber-400">조그가 명령 경로를 잡고 있습니다</span>
        </div>
        <p className="mt-1 text-xs text-neutral-400">
          팔을 움직여 <b>태그가 잘 보이는 자세</b>로 맞춘 뒤 저장하세요. 저장되는
          것은 지금 관절값입니다(그리퍼 제외).
        </p>

        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <div className="space-y-2">
            <ManualControlPanel
              currentJoints={joints}
              disabled={!ready}
              disabledHint={err || '조그를 여는 중…'}
              onSend={send}
              title="조그" />
            <div className="rounded border border-neutral-700 bg-neutral-950 p-2">
              <p className="mb-1 text-[11px] text-neutral-500">지금 관절값 (정규화)</p>
              <div className="grid grid-cols-3 gap-x-3 gap-y-0.5 text-xs text-neutral-300">
                {ARM.map((j, i) => (
                  <div key={j.name} className="flex justify-between">
                    <span className="text-neutral-500">{j.label.replace('Joint ', 'J')}</span>
                    <span className="tabular-nums">{(joints[i] ?? 0).toFixed(1)}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="space-y-2">
            <div className="overflow-hidden rounded border border-neutral-700 bg-black">
              {cameraId ? (
                <img alt={`${cameraId} 영상`} className="w-full"
                  src={`/api/cameras/${encodeURIComponent(cameraId)}/stream`} />
              ) : (
                <p className="p-6 text-center text-xs text-neutral-500">카메라를 고르세요</p>
              )}
            </div>
            <div className="rounded border border-neutral-700 bg-neutral-950 p-2 text-xs">
              <p className="mb-1 text-[11px] text-neutral-500">
                보이는 태그 ({family}, {tagMm}mm)
              </p>
              {tagErr ? (
                <p className="text-amber-400">{tagErr}</p>
              ) : tags.length ? (
                <div className="flex flex-wrap gap-1">
                  {tags.map((t) => (
                    <span key={t} className="rounded bg-green-600/20 px-2 py-0.5
                                             font-mono text-green-300">{t}</span>
                  ))}
                </div>
              ) : (
                <p className="text-neutral-500">
                  안 보입니다 — 이 자세로는 검사를 못 합니다
                </p>
              )}
            </div>
          </div>
        </div>

        {err && <p className="mt-2 rounded border border-red-500/40 bg-red-500/10 px-2 py-1
                              text-xs text-red-300">{err}</p>}

        <div className="mt-4 flex items-center justify-end gap-2">
          <input value={name} onChange={(e) => setName(e.target.value)}
            placeholder="자세 이름" 
            className="w-48 rounded border border-neutral-700 bg-neutral-950 px-2 py-1
                       text-xs text-neutral-100 focus:border-blue-500 focus:outline-none" />
          <button onClick={onClose}
            className="rounded bg-neutral-700 px-3 py-1 text-xs text-neutral-300
                       hover:bg-neutral-600">
            취소
          </button>
          <button onClick={() => void save()} disabled={busy || !name.trim() || !ready}
            className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-500
                       disabled:opacity-50">
            {busy ? '…' : '이 자세로 저장'}
          </button>
        </div>
      </div>
    </div>
  )
}
