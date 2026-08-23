import { useCallback, useEffect, useRef, useState } from 'react'
import ManualControlPanel from './ManualControlPanel'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'
import { JOINTS } from '../config/joints'

/**
 * 웹 조그 — 추론을 안 띄우고 팔을 움직인다 (feature/manual-control.md §2).
 *
 * 슬라이더는 [ManualControlPanel](ManualControlPanel.tsx) 을 그대로 쓴다.
 * 목적지만 다르다: 추론 경로가 아니라 `/robots/jog/goal` 이다.
 *
 * ## 세션이 있는 이유
 *
 * 조그는 팔의 명령 경로(shm 세그먼트)를 **점유**한다. 열려 있는 동안 추론·녹화가
 * 막히므로, 슬라이더를 만지는 것만으로 열리면 안 된다 — 명시적으로 시작하고
 * 끝낸다. 5분 안 만지면 백엔드가 알아서 닫는다.
 */

type Props = {
  iface: string
  /** 이 팔에 명령을 보낼 수 있나. 마스터거나 역할을 모르면 못 보낸다. */
  commandable: boolean
  reason?: string
}

export default function JogPanel({ iface, commandable, reason }: Props) {
  const { notify } = useSystemMessage()
  const [running, setRunning] = useState(false)
  // 시작 자세. **슬라이더의 출발점**이라, 0 이면 첫 조작에 팔이 튄다.
  const [joints, setJoints] = useState<number[]>([])
  const [busy, setBusy] = useState(false)
  const runningRef = useRef(false)
  runningRef.current = running

  const fail = (e: unknown, fallback: string) =>
    notify({ level: 'error', source: '조그',
             text: e instanceof Error ? e.message : fallback })

  // 조그를 안 켠 동안에도 현재 자세를 읽어 슬라이더를 맞춰 둔다 —
  // 켜자마자 슬라이더가 엉뚱한 데 있으면 첫 조작이 큰 이동이 된다.
  useEffect(() => {
    if (!commandable) return
    let alive = true
    const read = () => {
      api.get<Record<string, number>>(`/robots/parking/joints/${iface}`)
        .then((d) => { if (alive) setJoints(JOINTS.map((j) => d[j.actionKey] ?? 0)) })
        .catch(() => {})
    }
    read()
    // 조그 중에는 안 읽는다 — 슬라이더가 팔을 따라가면 서로를 밀어 떨린다
    const id = setInterval(() => { if (!runningRef.current) read() }, 500)
    return () => { alive = false; clearInterval(id) }
  }, [iface, commandable])

  const start = async () => {
    setBusy(true)
    try {
      await api.post('/robots/jog/start', { iface })
      setRunning(true)
    } catch (e) { fail(e, '조그를 시작하지 못했습니다') }
    finally { setBusy(false) }
  }

  const stop = useCallback(async () => {
    setBusy(true)
    try {
      await api.post('/robots/jog/stop', {})
      setRunning(false)
    } catch (e) { fail(e, '조그를 멈추지 못했습니다') }
    finally { setBusy(false) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ⚠ 화면을 떠나면 **반드시 닫는다.** 열린 채로 두면 추론·녹화가 계속 막히고,
  //   사용자는 왜 막히는지 알 길이 없다.
  useEffect(() => () => { if (runningRef.current) api.post('/robots/jog/stop', {}).catch(() => {}) }, [])

  const send = (values: Record<string, number>) => {
    api.post('/robots/jog/goal', { iface, values }).catch((e) => {
      // 세션이 닫혔는데 계속 밀면 같은 오류가 쌓인다 — 한 번 알리고 멈춘다
      setRunning(false)
      fail(e, '목표를 보내지 못했습니다')
    })
  }

  if (!commandable) {
    return (
      <p className="rounded border border-neutral-700 bg-neutral-800 px-3 py-2 text-xs text-neutral-500">
        {reason ?? '이 팔은 조작할 수 없습니다'}
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-neutral-400">
          웹 조그
          {running && <span className="ml-2 text-amber-400">· 명령 경로 점유 중</span>}
        </span>
        <button onClick={running ? stop : start} disabled={busy}
          className={`px-2 py-1 text-xs rounded text-white disabled:opacity-50 ${
            running ? 'bg-red-600 hover:bg-red-500' : 'bg-amber-600 hover:bg-amber-500'}`}>
          {busy ? '…' : running ? '조그 끝내기' : '조그 시작'}
        </button>
      </div>

      <ManualControlPanel
        currentJoints={joints}
        disabled={!running}
        onSend={send}
        title={`${iface} 관절 조그`}
        disabledHint="[조그 시작] 을 누르면 움직일 수 있습니다"
      />

      {running && (
        <p className="text-[10px] text-neutral-500">
          슬라이더를 놓으면 그 자세까지 가서 섭니다. 5분 동안 안 만지면 자동으로
          끝납니다 — 열려 있는 동안 추론·녹화가 막히기 때문입니다.
        </p>
      )}
    </div>
  )
}
