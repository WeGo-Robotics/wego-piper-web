import { useCallback, useEffect, useRef, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 말단(엔드포인트) 조그 — **관절은 팔의 온보드 IK 가 정한다**
 * (feature/teleoperation.md §3-C).
 *
 * ⚠ 이 모드는 관절 안전 필터를 **타지 않는다.** 그래서 화면도 절대 좌표를 안 받는다 —
 * 버튼을 누른 만큼만(±mm/±도) 간다. 상자 밖이면 백엔드가 거절하고 이유를 말한다.
 */

type Key = { label: string; hint: string; on: () => void }

/** 십자 패드 하나. 빈 자리는 그리지 않는다 — 눌리지 않는 칸이 있으면
 *  어디가 살아 있는지 매번 확인하게 된다. */
function Pad({ title, unit, up, down, left, right, centre }: {
  title: string; unit: string
  up?: Key; down?: Key; left?: Key; right?: Key; centre?: Key
}) {
  const key = (k: Key | undefined, cls: string) => (
    k ? (
      <button onClick={k.on} title={k.hint}
        className={`${cls} flex h-11 w-11 flex-col items-center justify-center rounded-lg
                    border border-neutral-600 bg-neutral-700 text-sm font-medium
                    text-neutral-100 transition-colors hover:bg-blue-600 active:bg-blue-500`}>
        <span>{k.label}</span>
        {k.hint && <span className="text-[9px] font-normal text-neutral-400">{k.hint}</span>}
      </button>
    ) : <span className={cls} />
  )
  return (
    <div className="flex flex-col items-center gap-1">
      <span className="text-[10px] text-neutral-500">{title} <span className="text-neutral-600">{unit}</span></span>
      <div className="grid grid-cols-3 grid-rows-3 gap-1">
        <span />{key(up, '')}<span />
        {key(left, '')}{key(centre, '')}{key(right, '')}
        <span />{key(down, '')}<span />
      </div>
    </div>
  )
}

type Pose = Record<string, number>
type Box = { x: number[]; y: number[]; z: number[] }

export default function EndPosePanel({ iface, enabled }: { iface: string; enabled: boolean }) {
  const { notify } = useSystemMessage()
  const [pose, setPose] = useState<Pose | null>(null)
  const [box, setBox] = useState<Box | null>(null)
  const trackRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // 화면을 떠나면 폴링을 멈춘다 — 안 그러면 모달을 닫아도 계속 읽는다
  useEffect(() => () => { if (trackRef.current) clearInterval(trackRef.current) }, [])
  const [stepMm, setStepMm] = useState(5)
  const [stepDeg, setStepDeg] = useState(2)

  const read = useCallback(() => {
    api.get<{ pose: Pose | null; box: Box }>(`/robots/end-pose/${iface}`)
      .then((r) => { setPose(r.pose); setBox(r.box) })
      .catch(() => {})
  }, [iface])

  useEffect(() => { if (enabled) read() }, [enabled, read])

  /**
   * 한 걸음 보낸다. **응답을 기다리며 잠기지 않는다.**
   *
   * ⚠ 예전에는 백엔드가 2초를 기다려 도달을 확인하고 답했다 — 버튼 한 번에 UI 가
   *   2초 잠겼고, 조그는 연타하는 물건이라 못 쓸 정도였다. 이제 명령을 보내고
   *   바로 풀리며, **팔이 가는 동안 자세만 따라 읽어** 움직임이 보이게 한다.
   *   도달 확인은 백엔드가 다음 명령 때 한다(막아야 할 순간이 거기다).
   */
  const jog = (axis: string, delta: number) => {
    api.post('/robots/end-pose/jog', { iface, axis, delta })
      .catch((e) => {
        // 상자 밖·도달 실패 모두 여기로 온다. 백엔드가 문장을 만든다 —
        // 화면이 조립하면 "왜 안 갔는지"가 두 곳에서 갈린다.
        notify({ level: 'warn', source: '말단 조그',
                 text: e instanceof Error ? e.message : '움직이지 못했습니다' })
      })
    // 가는 동안 자세를 따라 읽는다. 안 하면 눌러도 화면이 가만히 있어
    // "안 먹었나" 싶어 또 누르게 된다.
    track()
  }

  /** 잠깐 동안 자세를 자주 읽는다. 겹쳐 부르면 앞의 것을 대신한다. */
  const track = () => {
    if (trackRef.current) clearInterval(trackRef.current)
    let left = 12                       // 0.25초 × 12 ≈ 3초
    trackRef.current = setInterval(() => {
      read()
      if (--left <= 0 && trackRef.current) {
        clearInterval(trackRef.current)
        trackRef.current = null
      }
    }, 250)
  }

  if (!enabled) return null

  const mm = (v?: number) => (v === undefined ? '—' : (v / 1000).toFixed(1))
  const deg = (v?: number) => (v === undefined ? '—' : (v / 1000).toFixed(1))

  return (
    <div className="space-y-2 rounded border border-neutral-700 p-2">
      <div className="flex items-center justify-between">
        <span className="text-xs text-neutral-400">말단 조그 <span className="text-neutral-600">(온보드 IK)</span></span>
        <button onClick={read} className="text-[10px] text-neutral-500 hover:text-neutral-300">새로고침</button>
      </div>

      {pose ? (
        <p className="text-[11px] text-neutral-400 tabular-nums">
          X {mm(pose.x)} · Y {mm(pose.y)} · Z {mm(pose.z)} mm
          <span className="ml-2 text-neutral-500">
            RX {deg(pose.rx)} · RY {deg(pose.ry)} · RZ {deg(pose.rz)}°
          </span>
        </p>
      ) : (
        <p className="text-[11px] text-neutral-500">말단 자세를 읽지 못했습니다</p>
      )}

      <div className="flex items-center gap-2 text-[11px] text-neutral-400">
        <span>한 걸음</span>
        <input type="number" value={stepMm} min={1} max={20}
          onChange={(e) => setStepMm(Number(e.target.value))}
          className="w-12 rounded border border-neutral-700 bg-neutral-900 px-1 py-0.5 text-right" />
        <span>mm</span>
        <input type="number" value={stepDeg} min={1} max={10}
          onChange={(e) => setStepDeg(Number(e.target.value))}
          className="w-12 rounded border border-neutral-700 bg-neutral-900 px-1 py-0.5 text-right" />
        <span>도</span>
      </div>

      {/* ⚠ **방향을 자리로 읽게 한다.** 예전에는 축 라벨 옆에 작은 −/+ 두 개가
          있었는데, 어느 쪽이 앞인지 매번 라벨을 읽어야 했고 버튼이 손가락보다
          작았다. 십자 배치는 누르기 전에 방향이 보인다. */}
      <div className="flex flex-wrap items-start justify-center gap-4 py-1">
        <Pad title="이동" unit={`${stepMm}mm`}
             up={{ label: '↑', hint: 'X 앞', on: () => jog('x', stepMm) }}
             down={{ label: '↓', hint: 'X 뒤', on: () => jog('x', -stepMm) }}
             left={{ label: '←', hint: 'Y 좌', on: () => jog('y', stepMm) }}
             right={{ label: '→', hint: 'Y 우', on: () => jog('y', -stepMm) }} />
        <Pad title="높이" unit={`${stepMm}mm`}
             up={{ label: '▲', hint: 'Z 위', on: () => jog('z', stepMm) }}
             down={{ label: '▼', hint: 'Z 아래', on: () => jog('z', -stepMm) }} />
        {/* ⚠ RX 를 십자 **가운데**에 두었더니 한 방향밖에 안 됐다. 가운데는
            자리가 하나뿐인데 축은 양방향이 필요하다 — 롤은 따로 뺀다. */}
        <Pad title="손목" unit={`${stepDeg}°`}
             up={{ label: 'RY+', hint: '숙임', on: () => jog('ry', stepDeg) }}
             down={{ label: 'RY−', hint: '젖힘', on: () => jog('ry', -stepDeg) }}
             left={{ label: 'RZ+', hint: '좌', on: () => jog('rz', stepDeg) }}
             right={{ label: 'RZ−', hint: '우', on: () => jog('rz', -stepDeg) }} />
        <Pad title="롤 (RX)" unit={`${stepDeg}°`}
             left={{ label: '↺', hint: 'RX−', on: () => jog('rx', -stepDeg) }}
             right={{ label: '↻', hint: 'RX+', on: () => jog('rx', stepDeg) }} />
      </div>

      {box && (
        <p className="text-[10px] text-neutral-600">
          작업 공간 X {box.x[0]}~{box.x[1]} · Y {box.y[0]}~{box.y[1]} · Z {box.z[0]}~{box.z[1]} mm
          <span className="block">밖으로 나가는 명령은 <b>보내지 않고 거절</b>합니다.</span>
        </p>
      )}
    </div>
  )
}
