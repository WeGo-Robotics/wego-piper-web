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
  /** **같은 쪽** 리더 팔. 없으면 릴레이를 못 연다. */
  leader?: string
  /** 이 팔의 좌/우. 미지정이면 텔레오퍼레이션을 못 쓴다 — 짝을 정할 수 없다. */
  side?: string | null
}

type Teleop = { running: boolean; iface: string | null; mode: string; started: number | null }

const SIDE_LABEL: Record<string, string> = { left: '왼쪽', right: '오른쪽' }

const MODE_NAMES: Record<string, string> = {
  leader: '리더로 조종', joint: '관절 조그', endpoint: '말단 조그',
}

export default function JogPanel({ iface, commandable, reason, leader, side }: Props) {
  const { notify } = useSystemMessage()
  const [running, setRunning] = useState(false)
  // 시작 자세. **슬라이더의 출발점**이라, 0 이면 첫 조작에 팔이 튄다.
  const [joints, setJoints] = useState<number[]>([])
  const [relaying, setRelaying] = useState(false)
  // 릴레이 모드. **기본은 관절 복제** — 그쪽만 안전 필터를 탄다.
  const [relayMode, setRelayMode] = useState<'joint' | 'pose'>('joint')
  // POSE 모드가 왜 안 보내고 있나 (짐벌락·작업공간 밖·바닥·과속). 서버가 알려준다.
  const [relayBlocked, setRelayBlocked] = useState('')
  // ⚠ **서버가 실제로 돌고 있는 모드.** 고른 것과 다를 수 있다 — 옛 백엔드가
  //   `mode` 를 조용히 버리고 관절 복제로 돌아 "6D 인데 관절이 따라 돈다"로
  //   보고됐다. 화면이 고른 값을 보여주면 그 거짓말을 그대로 반복한다.
  const [runningMode, setRunningMode] = useState('')
  // 붙일 수 있는 기구학 모델. 6D 모드에서 팔로워 모델을 고른다 —
  // 관절 구성이 다른 팔(SO-101 등)을 붙이려는 것이 이 모드의 이유다.
  const [arms, setArms] = useState<string[]>([])
  const [followerArm, setFollowerArm] = useState('piper')
  const relayingRef = useRef(false)
  relayingRef.current = relaying
  const [busy, setBusy] = useState(false)
  const runningRef = useRef(false)
  runningRef.current = running

  const fail = (e: unknown, fallback: string) =>
    notify({ level: 'error', source: '조그',
             text: e instanceof Error ? e.message : fallback })

  // ⚠ **서버가 누구를 잡고 있는지 읽는다.** 이걸 안 보면 버튼이 거짓말한다:
  //   릴레이를 켜 둔 채 새로고침하면 로컬 state 가 비어서 [조그 시작] 이 눌리는
  //   것처럼 보이고, 누르면 409 만 돌아온다 — "조그가 안 된다"로 보고된 게 이 경우다.
  const [heldBy, setHeldBy] = useState<Teleop | null>(null)
  useEffect(() => {
    let alive = true
    const read = () => {
      api.get<Teleop>('/robots/teleop/status')
        .then((t) => {
          if (!alive) return
          setHeldBy(t.running ? t : null)
          // 내 팔을 내가 잡고 있는 경우에만 로컬 state 를 켠다
          setRunning(t.running && t.iface === iface && t.mode === 'joint')
          setRelaying(t.running && t.iface === iface && t.mode === 'leader')
        })
        .catch(() => {})
    }
    read()
    const id = setInterval(read, 1500)
    return () => { alive = false; clearInterval(id) }
  }, [iface])

  useEffect(() => {
    api.get<{ arms: string[] }>('/robots/relay/arms')
      .then((r) => setArms(r.arms ?? []))
      .catch(() => {})
  }, [])

  // POSE 모드는 조건이 안 맞으면 **말없이 안 보낸다** — 이유를 화면에 내야
  // 사용자가 "릴레이가 고장났다"고 읽지 않는다.
  useEffect(() => {
    if (!relaying) { setRelayBlocked(''); return }
    let alive = true
    const read = () => {
      api.get<{ mode: string; blocked: string }>('/robots/relay/status')
        .then((r) => {
          if (!alive) return
          setRelayBlocked(r.blocked || '')
          setRunningMode(r.mode ?? '')
        })
        .catch(() => {})
    }
    read()
    const id = setInterval(read, 700)
    return () => { alive = false; clearInterval(id) }
  }, [relaying])

  // 조그를 안 켠 동안에도 현재 자세를 읽어 슬라이더를 맞춰 둔다 —
  // 켜자마자 슬라이더가 엉뚱한 데 있으면 첫 조작이 큰 이동이 된다.
  useEffect(() => {
    if (!commandable) return
    let alive = true
    const read = () => {
      api.get<Record<string, number>>(`/robots/parking/joints/${iface}`)
        // ⚠ **`actionKey` 가 아니라 `name` 이다.** `/parking/joints` 는 평문
        //   이름(`joint1`)을 주는데 `actionKey` 는 LeRobot 규약(`joint1.pos`)이라,
        //   찾으면 전부 `undefined` 가 되고 `?? 0` 이 **슬라이더를 0 으로 굳혔다.**
        //   조그를 켜면 그 0 이 첫 목표가 되어 팔이 엉뚱한 자세로 기어갔다.
        .then((d) => { if (alive) setJoints(JOINTS.map((j) => d[j.name] ?? 0)) })
        .catch(() => {})
    }
    read()
    // 조그 중에는 안 읽는다 — 슬라이더가 팔을 따라가면 서로를 밀어 떨린다
    const id = setInterval(() => { if (!runningRef.current) read() }, 500)
    return () => { alive = false; clearInterval(id) }
  }, [iface, commandable])

  // 확인창을 안 띄운다 — 사용자가 지웠다. 대신 버튼 위 `title` 이 경고를 남긴다.
  // 되돌릴 수 있는 조작이고(다시 조그하면 된다) 자주 누르는 버튼이다.
  const goHome = async () => {
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
      await api.post('/robots/relay/start', {
        leader, follower: iface, mode: relayMode,
        leader_arm: 'piper', follower_arm: followerArm,
      })
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

  const heldLabel = heldBy
    ? `${heldBy.iface} ${MODE_NAMES[heldBy.mode] ?? heldBy.mode} 중`
    : ''

  const send = (values: Record<string, number>) => {
    api.post('/robots/jog/goal', { iface, values }).catch((e) => {
      // 세션이 닫혔는데 계속 밀면 같은 오류가 쌓인다 — 한 번 알리고 멈춘다
      setRunning(false)
      fail(e, '목표를 보내지 못했습니다')
    })
  }

  // 다른 조작이 팔을 잡고 있으면 **무엇이** 잡고 있는지 보여준다.
  const blocked = heldBy && !(heldBy.iface === iface && (running || relaying))

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
      {/* ⚠ **텔레오퍼레이션은 같은 쪽 리더로만 한다.** 좌우가 뒤집히면 조작자의
          손 방향과 팔 방향이 어긋나고, 그건 사람이 실수하는 자리다.
          좌/우 미지정 팔은 짝을 정할 수 없으므로 수동 조작만 쓴다. */}
      {!side && (
        <p className="rounded border border-neutral-700 bg-neutral-800 px-3 py-2
                      text-xs text-neutral-500">
          좌/우가 지정되지 않아 <b>수동 조작만</b> 됩니다 — [좌/우?] 로 정하면
          같은 쪽 리더로 조종할 수 있습니다.
        </p>
      )}
      {side && !leader && (
        <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2
                      text-xs text-amber-300">
          {SIDE_LABEL[side] ?? side}에 리더 팔이 없습니다 — 그 쪽 팔 하나를
          [마스터] 로 설정하면 조종할 수 있습니다.
        </p>
      )}
      {side && leader && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-neutral-400">
              {leader} 로 조종
              {relaying && (
                <span className="ml-2 text-amber-400">
                  · {runningMode === 'pose' ? '6D 자세' : '관절 복제'}로 따라가는 중
                </span>
              )}
            </span>
            <button onClick={relaying ? stopRelay : startRelay} disabled={busy || running}
              title={running ? '조그를 먼저 끝내세요' : undefined}
              className={`px-2 py-1 text-xs rounded text-white disabled:opacity-50 ${
                relaying ? 'bg-red-600 hover:bg-red-500' : 'bg-purple-700 hover:bg-purple-600'}`}>
              {relaying ? '릴레이 끝내기' : '리더로 조종'}
            </button>
          </div>

          {/* 모드는 시작 전에만 고른다 — 도는 중에 바꾸면 팔이 관절 모드와
              MoveP 사이에서 한 번 튄다. */}
          <div className="flex items-center gap-1">
            {([['joint', '관절 복제'], ['pose', '6D 자세']] as const).map(([m, label]) => (
              <button key={m} onClick={() => setRelayMode(m)} disabled={relaying || busy}
                className={`px-2 py-0.5 text-[11px] rounded border transition-colors
                  disabled:opacity-40 ${relayMode === m
                    ? 'border-purple-500 bg-purple-500/20 text-purple-200'
                    : 'border-neutral-700 text-neutral-500 hover:text-neutral-300'}`}>
                {label}
              </button>
            ))}
            <span className="ml-1 text-[11px] text-neutral-600">
              {relayMode === 'joint'
                ? '리더 관절을 그대로 복제합니다'
                : 'FK → 6D 자세 → IK → 팔로워 관절 (다른 팔도 가능)'}
            </span>
          </div>

          {/* 6D 모드는 관절 구성이 다른 팔을 팔로워로 붙이기 위한 것이다.
              관절 목표로 끝나므로 안전 필터는 관절 복제와 똑같이 걸린다. */}
          {relayMode === 'pose' && (
            <div className="space-y-1 rounded border border-neutral-700 bg-neutral-900/60
                            px-2 py-1.5 text-[11px] leading-relaxed text-neutral-400">
              <p>
                리더 관절 → FK → <b>말단 6D 자세</b> → IK → 팔로워 관절.
                가운데 자세만 건너가므로 <b>관절 구성이 다른 팔</b>도 팔로워로 쓸 수 있습니다.
              </p>
              <p>
                관절 목표로 끝나므로 바닥 필터·관절 범위·변화율 제한이
                관절 복제와 <b>똑같이 걸립니다.</b>
              </p>
              {arms.length > 1 && (
                <div className="flex items-center gap-1 pt-0.5">
                  <span>팔로워 모델</span>
                  <select value={followerArm} onChange={(e) => setFollowerArm(e.target.value)}
                    disabled={relaying}
                    className="rounded border border-neutral-700 bg-neutral-900 px-1 py-0.5
                               text-[11px] text-neutral-200 disabled:opacity-40">
                    {arms.map((a) => <option key={a} value={a}>{a}</option>)}
                  </select>
                </div>
              )}
            </div>
          )}

          {/* 고른 것과 실제가 다르면 **그 사실이 제일 먼저 보여야 한다.** */}
          {relaying && runningMode && runningMode !== relayMode && (
            <p className="rounded border border-red-500/50 bg-red-500/10 px-2 py-1.5
                          text-[11px] text-red-300">
              고른 모드는 <b>{relayMode === 'pose' ? '6D 자세' : '관절 복제'}</b> 인데
              실제로는 <b>{runningMode === 'pose' ? '6D 자세' : '관절 복제'}</b> 로 돌고 있습니다.
              백엔드가 새 코드인지 확인하세요 ([설정]-[서비스]).
            </p>
          )}

          {relaying && relayBlocked && (
            <p className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1.5
                          text-[11px] text-amber-300">
              멈춤: {relayBlocked}
            </p>
          )}
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
        <button onClick={running ? stop : start} disabled={busy || relaying || !!blocked}
          title={blocked ? `${heldLabel} — 먼저 멈추세요`
                 : relaying ? '릴레이를 먼저 끝내세요' : undefined}
          className={`px-2 py-1 text-xs rounded text-white disabled:opacity-50 ${
            running ? 'bg-red-600 hover:bg-red-500' : 'bg-amber-600 hover:bg-amber-500'}`}>
          {busy ? '…' : running ? '조그 끝내기' : '조그 시작'}
        </button>
        </span>
      </div>

      {blocked && (
        <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          {heldLabel}입니다 — 조작은 한 번에 하나입니다. 먼저 그것을 멈추세요.
        </p>
      )}

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
