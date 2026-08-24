import { useCallback, useEffect, useRef, useState } from 'react'
import EndPosePanel from './EndPosePanel'
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
  /** 짝이 될 리더 팔. 있으면 "리더로 조종" 도 제공한다. */
  leader?: string
}

export default function JogPanel({ iface, commandable, reason, leader }: Props) {
  const { notify, confirm } = useSystemMessage()
  const [running, setRunning] = useState(false)
  // 시작 자세. **슬라이더의 출발점**이라, 0 이면 첫 조작에 팔이 튄다.
  const [joints, setJoints] = useState<number[]>([])
  const [relaying, setRelaying] = useState(false)
  const relayingRef = useRef(false)
  relayingRef.current = relaying
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

  const goHome = async () => {
    const yes = await confirm(
      `${iface} 를 파킹(원점) 자세로 보냅니다.\n\n` +
      '팔이 지금 자세에서 파킹까지 **한 번에** 움직입니다 — 경로 위가 비어 있는지 확인하세요.')
    if (!yes) return
    setBusy(true)
    try {
      await api.post('/robots/parking/go', { iface })
      notify({ level: 'info', source: '로봇', text: `${iface} 파킹 자세로 이동` })
    } catch (e) { fail(e, '파킹으로 보내지 못했습니다') }
    finally { setBusy(false) }
  }

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
  useEffect(() => () => {
    if (runningRef.current) api.post('/robots/jog/stop', {}).catch(() => {})
    if (relayingRef.current) api.post('/robots/relay/stop', {}).catch(() => {})
  }, [])

  const startRelay = async () => {
    if (!leader) return
    setBusy(true)
    try {
      await api.post('/robots/relay/start', { leader, follower: iface })
      setRelaying(true)
    } catch (e) { fail(e, '릴레이를 시작하지 못했습니다') }
    finally { setBusy(false) }
  }

  const stopRelay = async () => {
    setBusy(true)
    try {
      await api.post('/robots/relay/stop', {})
      setRelaying(false)
    } catch (e) { fail(e, '릴레이를 멈추지 못했습니다') }
    finally { setBusy(false) }
  }

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
      {/* 리더 릴레이 — 같은 명령 경로를 쓰므로 조그와 **동시에 못 켠다**.
          백엔드의 teleop 세션이 그걸 지키고, 여기서는 서로 가려 둔다. */}
      {leader && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-neutral-400">
            {leader} 로 조종
            {relaying && <span className="ml-2 text-amber-400">· 따라가는 중</span>}
          </span>
          <button onClick={relaying ? stopRelay : startRelay} disabled={busy || running}
            title={running ? '조그를 먼저 끝내세요' : undefined}
            className={`px-2 py-1 text-xs rounded text-white disabled:opacity-50 ${
              relaying ? 'bg-red-600 hover:bg-red-500' : 'bg-purple-700 hover:bg-purple-600'}`}>
            {relaying ? '릴레이 끝내기' : '리더로 조종'}
          </button>
        </div>
      )}

      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-neutral-400">
          웹 조그
          {running && <span className="ml-2 text-amber-400">· 명령 경로 점유 중</span>}
        </span>
        <span className="flex gap-1">
        {/* 원점(파킹)으로 — 조그로 되돌리려면 관절 여섯을 손으로 맞춰야 한다 */}
        <button onClick={goHome} disabled={busy || running || relaying}
          title={running || relaying ? '조작을 먼저 끝내세요' : '파킹(원점) 자세로 이동'}
          className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white disabled:opacity-50">
          원점으로
        </button>
        <button onClick={running ? stop : start} disabled={busy || relaying}
          title={relaying ? '릴레이를 먼저 끝내세요' : undefined}
          className={`px-2 py-1 text-xs rounded text-white disabled:opacity-50 ${
            running ? 'bg-red-600 hover:bg-red-500' : 'bg-amber-600 hover:bg-amber-500'}`}>
          {busy ? '…' : running ? '조그 끝내기' : '조그 시작'}
        </button>
        </span>
      </div>

      <ManualControlPanel
        currentJoints={joints}
        disabled={!running}
        onSend={send}
        title={`${iface} 관절 조그`}
        disabledHint="[조그 시작] 을 누르면 움직일 수 있습니다"
      />

      {/* 말단 조그 — 조그 세션과 **다른 경로**다(shm 이 아니라 RPC).
          그래서 세션을 안 열어도 되지만, 릴레이 중에는 팔이 리더를 따라가므로 숨긴다. */}
      <EndPosePanel iface={iface} enabled={!relaying} />

      {running && (
        <p className="text-[10px] text-neutral-500">
          슬라이더를 놓으면 그 자세까지 가서 섭니다. 5분 동안 안 만지면 자동으로
          끝납니다 — 열려 있는 동안 추론·녹화가 막히기 때문입니다.
        </p>
      )}
    </div>
  )
}
