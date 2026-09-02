import { useCallback, useEffect, useRef, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { JOINTS } from '../config/joints'
import { api } from '../services/api'

/**
 * 하드웨어 영점 설정 (piper SDK `JointConfig(set_zero=0xAE)`, CAN 0x475).
 *
 * ## ⚠ 소프트웨어 영점과 다른 물건이다
 *
 *   소프트웨어  [파킹 보정] — 우리 파일(`~/.piper/parking/*.json`)에 자세를 적는다.
 *               `JOINT_CALIBRATION` 도 이쪽이다. 고쳐도 팔은 아무것도 모르고,
 *               언제든 되돌린다.
 *
 *   하드웨어    여기 — 모터 드라이버 **플래시**에 지금 위치를 0 으로 굽는다.
 *               전원을 꺼도 남고, **되돌리는 명령이 SDK 에 없다.**
 *               raw 엔코더 값의 의미 자체가 바뀐다.
 *
 * 그래서 이 창은 두 가지를 해야 한다: 지금 raw 가 얼마인지 **보여주고**,
 * 무엇이 어긋나게 되는지 **말한다.** 버튼만 있으면 되돌릴 수 없는 조작을
 * 실수로 누르게 된다.
 */

type Result = { ok: boolean; raw_before?: number | null; raw_after?: number | null; error?: string }

export default function ZeroCalibrationModal({ iface, onClose }:
  { iface: string; onClose: () => void }) {
  const { notify, confirm } = useSystemMessage()
  const [raw, setRaw] = useState<Record<string, number>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [done, setDone] = useState<Record<string, Result>>({})
  const pollRef = useRef<ReturnType<typeof setInterval>>(undefined)

  // ⚠ **정규화가 아니라 raw 를 보여준다.** 정규화 값은 우리 표를 거친 것이라
  //   영점을 옮기면 같이 흔들린다 — 무엇을 굽는지 보려면 팔이 말하는 숫자여야 한다.
  const read = useCallback(async () => {
    try {
      const d = await api.get<Record<string, number>>(`/robots/joints/raw/${iface}`)
      setRaw(d)
    } catch { /* 폴링이라 조용히 넘어간다 */ }
  }, [iface])

  useEffect(() => {
    read()
    pollRef.current = setInterval(read, 300)
    return () => clearInterval(pollRef.current)
  }, [read])

  // 0x150(0x02) 리셋 — 슬립으로 밀린 보고 위치를 실제에 재동기화한다 (piper_sdk
  // #120). 영점을 굽기 **전에** 눌러야 오진(슬립을 영점으로 굽기)을 피한다.
  // 백엔드가 리셋 전후 간극(=쌓인 슬립)을 재서 문구로 준다.
  const [resetting, setResetting] = useState(false)
  const doReset = async () => {
    setResetting(true)
    try {
      const r = await api.post<{ warnings: string[] }>('/robots/reset', { iface })
      for (const w of r.warnings) notify({ level: 'warn', source: '영점', text: w })
      if (r.warnings.length === 0)
        notify({ level: 'info', source: '영점',
          text: '리셋 완료 — 재동기화 간극 없음 (보고 위치가 실제와 일치하고 있었습니다)' })
      await read()
    } catch (e) {
      notify({ level: 'error', source: '영점',
        text: e instanceof Error ? e.message : '리셋 실패' })
    } finally {
      setResetting(false)
    }
  }

  // 300ms 폴링이 이미 돌지만, 명시적 버튼은 "지금 이 순간 값"이라는 확신을 준다 —
  // 폴링이 조용히 실패하고 있어도 여기서는 에러가 보인다.
  const [refreshing, setRefreshing] = useState(false)
  const doRefresh = async () => {
    setRefreshing(true)
    try {
      const d = await api.get<Record<string, number>>(`/robots/joints/raw/${iface}`)
      setRaw(d)
    } catch (e) {
      notify({ level: 'error', source: '영점',
        text: e instanceof Error ? e.message : '위치를 읽지 못했습니다' })
    } finally {
      setRefreshing(false)
    }
  }

  const setZero = async (joint: string, label: string) => {
    const now = raw[joint]
    const ok = await confirm(
      `${label} 의 지금 위치를 하드웨어 영점으로 굽습니다.\n\n` +
      `현재 raw ${now ?? '?'}\n\n` +
      '이 값이 모터 플래시에 쓰이고 전원을 꺼도 남습니다. 되돌리는 명령이 없습니다.\n' +
      '이 관절이 정말 원점 자세에 있는지 확인하세요.',
      { danger: true })
    if (!ok) return
    setBusy(joint)
    try {
      const r = await api.post<Result>('/robots/zero', { iface, joint })
      setDone((d) => ({ ...d, [joint]: r }))
      notify({ level: 'info', source: '영점',
        text: `${label} 영점 설정 — raw ${r.raw_before} → ${r.raw_after}` })
    } catch (e) {
      const msg = e instanceof Error ? e.message : '영점 설정 실패'
      setDone((d) => ({ ...d, [joint]: { ok: false, error: msg } }))
      notify({ level: 'error', source: '영점', text: `${label}: ${msg}` })
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
         onClick={onClose}>
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border
                      border-red-500/40 bg-neutral-900 p-5 space-y-4"
           onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold text-neutral-100">
              하드웨어 영점 — {iface}
            </h2>
            <p className="text-xs text-neutral-400">
              모터 플래시에 굽습니다. [파킹 보정]과 다른 물건입니다.
            </p>
          </div>
          <button onClick={onClose}
                  className="shrink-0 rounded px-2 py-1 text-sm text-neutral-400 hover:text-white">
            닫기
          </button>
        </div>

        {/* ⚠ 이 경고가 없으면 버튼 일곱 개짜리 평범한 창으로 보인다. */}
        <div className="space-y-1 rounded border border-red-500/40 bg-red-500/10 px-3 py-2
                        text-xs leading-relaxed text-red-300">
          <p><b>되돌릴 수 없습니다.</b> SDK 에 영점 해제 명령이 없습니다.
            되돌리려면 그 관절을 원래 자세로 되돌려 놓고 다시 구워야 하는데,
            그 자세를 기록해 두지 않았다면 찾을 수 없습니다.</p>
          <p>영점을 옮기면 <b>raw 값의 의미가 바뀝니다.</b> 정규화·순기구학·바닥
            필터의 높이, 저장된 파킹 자세, <b>이미 녹화한 데이터셋의 뜻</b>까지
            그 팔 기준으로 어긋납니다.</p>
        </div>

        <ol className="list-decimal space-y-0.5 pl-5 text-xs text-neutral-400">
          <li>[리셋]으로 보고 위치를 실제와 재동기화합니다 — 간극이 크게 나오면
            영점이 아니라 <b>슬립</b>이었던 것입니다 (굽지 마세요)</li>
          <li>토크를 끄고 관절을 손으로 원점 자세에 맞춥니다</li>
          <li>아래 raw 값이 그 자세에서 멈춘 것을 확인합니다</li>
          <li>그 관절의 [영점] 을 누릅니다</li>
        </ol>

        <div className="flex items-center gap-2">
          <button onClick={() => void doReset()} disabled={resetting || busy !== null}
            title="MotionCtrl_1(0x02,0,0) — 급정지 해제 + 에러 클리어 + 보고 위치를 출력축 실제값에 재동기화. 슬립(piper_sdk #120)이 쌓여 있었다면 간극이 경고로 뜹니다."
            className="rounded bg-blue-600 px-3 py-1.5 text-xs text-white
                       hover:bg-blue-500 disabled:opacity-40">
            {resetting ? '리셋 중…' : '리셋 (0x150 재동기화)'}
          </button>
          <button onClick={() => void doRefresh()} disabled={refreshing}
            title="raw 위치를 지금 다시 읽습니다 (평소에도 0.3초마다 자동 갱신됩니다)"
            className="rounded bg-neutral-700 px-3 py-1.5 text-xs text-neutral-200
                       hover:bg-neutral-600 disabled:opacity-40">
            {refreshing ? '읽는 중…' : '위치 새로고침'}
          </button>
        </div>

        <div className="space-y-1">
          {JOINTS.map((j) => {
            const r = done[j.name]
            return (
              <div key={j.name}
                   className="flex items-center gap-3 rounded border border-neutral-700
                              bg-neutral-800 px-3 py-2">
                <span className="w-20 text-sm text-neutral-200">{j.label}</span>
                <span className="w-24 text-right font-mono text-xs text-neutral-400">
                  {raw[j.name] ?? '—'}
                </span>
                <span className="flex-1 text-xs">
                  {r && (r.ok
                    ? <span className="text-green-400">✔ {r.raw_before} → {r.raw_after}</span>
                    : <span className="text-red-400">{r.error}</span>)}
                </span>
                <button
                  onClick={() => void setZero(j.name, j.label)}
                  disabled={busy !== null}
                  className="shrink-0 rounded bg-red-600/80 px-2.5 py-1 text-xs text-white
                             hover:bg-red-600 disabled:opacity-40">
                  {busy === j.name ? '…' : '영점'}
                </button>
              </div>
            )
          })}
        </div>

        {/* 전체 한 번에 굽는 버튼은 두지 않는다 — 되돌릴 수 없는 조작을 일곱 개
            묶어 한 번에 실행할 이유가 없고, 하나라도 자세가 틀리면 전부 다시 맞춰야 한다. */}
        <p className="text-xs text-neutral-600">
          한 번에 하나씩만 굽습니다. 일곱 개를 묶어 실행하면 하나가 틀렸을 때
          어느 것이 틀렸는지 알 수 없습니다.
        </p>
      </div>
    </div>
  )
}
