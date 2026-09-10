import { useCallback, useEffect, useRef, useState } from 'react'
import { useSystemMessage } from './SystemMessages'
import { api } from '../services/api'

/**
 * 버전 카드 — "지금 도는 것이 무엇인가", 그리고 [업데이트] (feature/version-update.md).
 *
 * 정본은 코드 밖이다(이미지 매니페스트·git). 바깥 소프트웨어는 출처가 셋 —
 * 컨테이너(게이트웨이가 잰다)·호스트(unitd 가 말한다)·데몬(자기 보고). **온 것만
 * 그린다.** 죽은 데몬·없는 unitd 는 비워 둔다 — 지어내지 않는다.
 *
 * 업데이트는 셋으로 나뉜다: [확인](레지스트리/원격 태그, 배지만) → [받기](이미지를
 * 받고 꺼내기만 — 실행하지 않는다) → [적용](apply.sh). 실행은 호스트의 piper-unitd 가
 * 일시 유닛으로 — 게이트웨이는 절차의 마지막에 자기 자신이 갈아치워진다. 그래서
 * [적용] 뒤 화면은 **끊길 것을 알고** /health 를 두드리다 돌아오면 새로고침한다.
 *
 * sudo 가 필요한 전제는 자동화하지 않는다 — apply.sh 가 찍은 명령을 복사 가능한
 * 블록으로 보여 주고 "실행한 뒤 다시 [적용]". **이번 릴리스가 실제로 건드린** wheel
 * 만 게이트웨이 버전과 견준다(매니페스트의 `wheels`) — 다르면 노란 줄: 업데이트 뒤
 * 그 데몬이 재시작을 못 받았다는 뜻이다.
 *
 * ⚠ **게이트웨이 버전 == 모든 wheel 버전은 틀린 전제다.** `release.sh` 는 바뀐
 * 패키지만 굽는다(feature/version-update.md 자체가 "판정은 diff로"). v0.4.8~
 * v0.4.10처럼 bus/shm/robot/cam/rs 를 안 건드린 패치에서는 그 wheel 들이 예전
 * 버전 그대로인 게 **정상**이다 — 매번 다 굽지 않는 게 이 프로젝트의 설계다.
 * .120 실기에서 이걸 "모든 게 최신"으로 잘못 가정해 매 패치마다 오탐이 났다.
 */

type VersionInfo = {
  gateway: { version: string; source: string | null; built_at?: string | null; prev?: string | null
    registry?: string | null; wheels?: string | null }
  container: Record<string, string>
  host: Record<string, string>
  deploy: { work?: string; current?: string | null; applied_at?: number | null
    versions?: { version: string; built_at?: string }[] }
  daemons: Record<string, Record<string, string>>
}
type Check = { current: string; latest: string | null; available: boolean; mode: 'image' | 'source'
  checked_at: number; error: string | null }
type UpdateStatus = { active: boolean; finished: boolean; ok?: boolean; result?: string; exit?: string
  log: string; need_sudo: string[]; version?: string; stage?: 'pull' | 'apply'; mode?: string
  started?: number; unitd: boolean }

const CONTAINER_ORDER = ['lerobot', 'torch', 'cuda', 'torchvision', 'transformers', 'piper-sdk',
  'pyrealsense2', 'opencv-python-headless', 'ultralytics', 'anthropic', 'numpy']
const HOST_ORDER = ['nvidia-driver', 'docker', 'redis', 'ollama', 'python']
const LABEL: Record<string, string> = {
  'nvidia-driver': 'NVIDIA', cuda: 'CUDA', 'piper-sdk': 'piper_sdk',
  'opencv-python-headless': 'opencv', 'feetech-servo-sdk': 'feetech',
}

function fmtDate(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString('ko-KR', { hour12: false })
}

/** `piper-robot 0.4.5` 처럼 태그가 도장 찍힌 wheel 만, **이번 릴리스가 실제로
 * 건드린 패키지**만 비교한다(매니페스트 `wheels`, 공백으로 나뉜 짧은 이름 —
 * `bus` → `piper-bus`). `wheels` 가 없거나 비었으면 이번 릴리스는 wheel 을
 * 하나도 안 구웠다는 뜻이라 아무것도 비교하지 않는다 — 안 그러면 wheel 을
 * 안 건드린 패치마다 모든 데몬이 "낡았다"로 나온다. `0.1.0` 은 도장 전 빌드. */
function wheelMismatch(gw: string, wheels: string | null | undefined,
                        daemons: Record<string, Record<string, string>>): string[] {
  const tag = gw.replace(/^v/, '').replace(/-.*$/, '')
  const touched = new Set((wheels ?? '').split(/\s+/).filter(Boolean).map((p) => `piper-${p}`))
  if (touched.size === 0) return []
  const out: string[] = []
  for (const [d, vs] of Object.entries(daemons)) {
    for (const [pkg, v] of Object.entries(vs)) {
      if (touched.has(pkg) && v !== '0.1.0' && v !== tag) out.push(`${d} ${pkg} ${v}`)
    }
  }
  return out
}

export default function VersionCard() {
  const { notify, confirm } = useSystemMessage()
  const [info, setInfo] = useState<VersionInfo | null>(null)
  const [err, setErr] = useState('')
  const [check, setCheck] = useState<Check | null>(null)
  const [checking, setChecking] = useState(false)
  const [status, setStatus] = useState<UpdateStatus | null>(null)
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [waitingBack, setWaitingBack] = useState(false)
  const [rollbackTo, setRollbackTo] = useState('')
  const pollRef = useRef<number | null>(null)

  const loadInfo = useCallback(() => {
    api.get<VersionInfo>('/system/version').then(setInfo)
      .catch((e) => setErr(e instanceof Error ? e.message : '버전을 읽지 못했습니다'))
  }, [])
  const loadStatus = useCallback(() =>
    api.get<UpdateStatus>('/system/update/status').then(setStatus).catch(() => {}), [])

  useEffect(() => { loadInfo(); loadStatus() }, [loadInfo, loadStatus])

  // 받기·적용이 도는 동안 2초마다 상태(저널 꼬리)를 본다
  useEffect(() => {
    if (!status?.active) { if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null }; return }
    if (!pollRef.current) pollRef.current = window.setInterval(loadStatus, 2000)
    return () => { if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null } }
  }, [status?.active, loadStatus])

  // 받기가 끝나면 그 버전의 "무엇이 달라졌나"를 읽는다
  useEffect(() => {
    if (status?.finished && status.ok && status.stage === 'pull' && status.version) {
      api.get<{ notes: string }>(`/system/update/notes?version=${encodeURIComponent(status.version)}`)
        .then((r) => setNotes(r.notes)).catch(() => setNotes(''))
    }
  }, [status?.finished, status?.ok, status?.stage, status?.version])

  const doCheck = async (force = true) => {
    setChecking(true)
    try { setCheck(await api.get<Check>(`/system/update/check?force=${force ? 1 : 0}`)) }
    catch (e) { notify({ level: 'error', source: '업데이트', text: e instanceof Error ? e.message : '확인 실패' }) }
    finally { setChecking(false) }
  }

  const pull = async (version: string) => {
    setBusy(true); setNotes('')
    try {
      await api.post('/system/update/pull', { version })
      notify({ level: 'info', source: '업데이트', text: `${version} 받는 중 — 아무것도 실행하지 않습니다` })
      setTimeout(loadStatus, 800)
    } catch (e) { notify({ level: 'error', source: '업데이트', text: e instanceof Error ? e.message : '받기 실패' }) }
    finally { setBusy(false) }
  }

  const waitForGateway = () => {
    setWaitingBack(true)
    const started = Date.now()
    const tick = async () => {
      try {
        const r = await fetch('/health', { cache: 'no-store' })
        if (r.ok && Date.now() - started > 8000) { window.location.reload(); return }
      } catch { /* 아직 안 돌아왔다 */ }
      if (Date.now() - started < 10 * 60 * 1000) window.setTimeout(tick, 2000)
    }
    window.setTimeout(tick, 4000)
  }

  const apply = async (version: string, rollback = false) => {
    // ⚠ window.confirm 금지 — heartbeat 를 막는다. 논블로킹 모달.
    const yes = await confirm(
      (rollback ? `${version} 으로 되돌립니다.\n\n` : `${version} 을 적용합니다.\n\n`) +
      '데몬과 게이트웨이가 갈아치워집니다 — 몇 초에서 몇 분 동안 화면이 끊깁니다.\n' +
      '돌아오면 자동으로 새로고침합니다. 데이터(데이터셋·설정)는 건드리지 않습니다.')
    if (!yes) return
    setBusy(true)
    try {
      await api.post('/system/update/apply', { version })
      notify({ level: 'warn', source: '업데이트', text: `${version} 적용 중 — 곧 연결이 끊깁니다` })
      setTimeout(loadStatus, 800)
      waitForGateway()
    } catch (e) { notify({ level: 'error', source: '업데이트', text: e instanceof Error ? e.message : '적용 실패' }) }
    finally { setBusy(false) }
  }

  if (err) return <p className="text-xs text-red-400">{err}</p>
  if (!info) return <p className="text-xs text-neutral-500">버전을 읽는 중…</p>

  const gw = info.gateway
  const mismatch = wheelMismatch(gw.version, gw.wheels, info.daemons)
  const pick = (src: Record<string, string>, order: string[]) =>
    order.filter((k) => src[k]).map((k) => [k, src[k]] as const)
  const daemonRows = Object.entries(info.daemons)
    .map(([d, vs]) => [d, Object.entries(vs).filter(([k]) => !k.startsWith('piper-bus') && !k.startsWith('piper-shm'))] as const)
    .filter(([, vs]) => vs.length > 0)
  const current = gw.version.split('-')[0]
  const pulled = !!(status?.finished && status.ok && status.stage === 'pull' && status.version)
  const pulledVersion = pulled ? status!.version! : null
  const others = (info.deploy?.versions ?? []).map((v) => v.version).filter((v) => v !== current)
  const canControl = status?.unitd !== false

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">
            piper-web <span className="font-mono text-emerald-300">{gw.version}</span>
            {check?.available && check.latest && (
              <span className="ml-2 rounded bg-blue-600/30 px-1.5 py-0.5 text-xs text-blue-200">
                새 버전 {check.latest}
              </span>
            )}
          </h2>
          <p className="mt-0.5 text-[11px] text-neutral-500">
            {gw.source === 'git' ? '소스에서 실행 (git describe)'
              : gw.source === 'env' || gw.source === 'manifest' ? '배포 이미지'
              : '버전을 알 수 없습니다'}
            {gw.built_at && ` · ${fmtDate(gw.built_at)} 빌드`}
            {gw.prev && ` · 직전 ${gw.prev}`}
            {info.deploy?.current && ` · 적용본 ${info.deploy.current}`}
            {check && !check.available && !check.error && ' · 최신입니다'}
            {check?.error && <span className="text-amber-400"> · 확인 실패: {check.error}</span>}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <button onClick={() => doCheck(true)} disabled={checking || busy}
            className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-50">
            {checking ? '확인 중…' : '새 버전 확인'}
          </button>
          {check?.available && check.latest && check.mode === 'image' && !pulled && (
            <button onClick={() => pull(check.latest!)} disabled={busy || !!status?.active || !canControl}
              title={!canControl ? '서비스 관리 데몬(piper-unitd)이 없습니다' : '이미지를 받고 꺼내기만 — 실행하지 않습니다'}
              className="px-2 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
              {check.latest} 받기
            </button>
          )}
          {((pulled && pulledVersion) || (check?.available && check.mode === 'source' && check.latest)) && (
            <button onClick={() => apply(pulledVersion ?? check!.latest!)} disabled={busy || !!status?.active || !canControl}
              className="px-2 py-1 text-xs rounded bg-emerald-700 hover:bg-emerald-600 text-white disabled:opacity-50">
              {pulledVersion ?? check!.latest} 적용
            </button>
          )}
        </div>
      </div>

      {mismatch.length > 0 && (
        <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          ⚠ 게이트웨이는 {gw.version} 인데 데몬 wheel 이 다릅니다: {mismatch.join(', ')} —
          업데이트 뒤 데몬이 재시작을 못 받았습니다. 아래 서비스에서 재시작하세요.
        </p>
      )}

      {waitingBack && (
        <p className="rounded border border-blue-500/40 bg-blue-500/10 px-3 py-2 text-xs text-blue-200">
          적용 중 — 게이트웨이가 갈아치워지는 동안 연결이 끊깁니다. 돌아오면 자동으로 새로고침합니다.
        </p>
      )}

      {status && (status.active || status.finished) && (
        <div className="space-y-1.5 rounded border border-neutral-700 bg-neutral-900/60 p-3">
          <p className="text-xs text-neutral-300">
            {status.stage === 'pull' ? '받기' : '적용'} {status.version}
            {status.active ? <span className="ml-2 text-blue-300">진행 중…</span>
              : status.ok ? <span className="ml-2 text-emerald-400">완료</span>
              : <span className="ml-2 text-red-400">실패 (종료 코드 {status.exit ?? '?'})</span>}
          </p>
          {status.need_sudo.length > 0 && (
            <div className="rounded border border-red-500/40 bg-red-500/10 p-2 text-xs text-red-200">
              <p className="mb-1">전제가 빠져 있어 멈췄습니다. 호스트에서 아래를 실행한 뒤 다시 누르세요
                (그룹은 다시 로그인해야 반영됩니다):</p>
              <pre className="whitespace-pre-wrap font-mono text-[11px] text-red-100">{status.need_sudo.join('\n')}</pre>
              <button onClick={() => navigator.clipboard?.writeText(status.need_sudo.join('\n'))}
                className="mt-1 px-2 py-0.5 text-[11px] rounded bg-neutral-700 hover:bg-neutral-600">복사</button>
            </div>
          )}
          {notes && (
            <details open className="text-xs">
              <summary className="cursor-pointer text-neutral-400">{status.version} 에서 무엇이 달라졌나</summary>
              <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap font-sans text-[11px] text-neutral-300">{notes}</pre>
            </details>
          )}
          {status.log && (
            <details className="text-xs">
              <summary className="cursor-pointer text-neutral-500">로그</summary>
              <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-neutral-400">{status.log}</pre>
            </details>
          )}
        </div>
      )}

      {/* ⚠ **값이 카드 밖으로 넘쳐 가로 스크롤바가 생겼다.** 항목 span 들이
          `whitespace-nowrap` 인데 JSX 가 그 사이에 공백 없이 붙여서, "0.5.0" 과
          "torch" 경계에 **줄바꿈 기회가 아예 없었다** — 한 줄 전체가 쪼갤 수 없는
          덩어리였다. 거기에 `1fr` 트랙은 최소값이 auto(=min-content)라 그 덩어리
          폭만큼 늘었다. 재현: 700px 창에서 scrollWidth 1224.

          그래서 두 겹으로 막는다: 값 트랙은 `minmax(0,1fr)` 로 내용에 안 밀리고,
          값 줄은 flex-wrap 이라 **항목 경계에서** 접힌다(텍스트 줄바꿈 기회와 무관).
          "lerobot 0.5.0" 같은 항목 하나만 nowrap 으로 붙들어 둔다.
          라벨 열의 nowrap 은 그대로다 — 한 글자씩 세로로 쌓이던 것을 막는 자리다. */}
      <div className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-[auto_minmax(0,1fr)]">
        <span className="whitespace-nowrap text-neutral-500">컨테이너</span>
        <div className="flex min-w-0 flex-wrap gap-x-3 gap-y-0.5 text-neutral-300">
          {pick(info.container, CONTAINER_ORDER).map(([k, v]) => (
            <span key={k} className="whitespace-nowrap">{LABEL[k] ?? k} <b className="font-mono font-normal text-neutral-200">{v}</b></span>
          ))}
          {pick(info.container, CONTAINER_ORDER).length === 0 && <span className="text-neutral-600">—</span>}
        </div>
        <span className="whitespace-nowrap text-neutral-500">호스트</span>
        <div className="flex min-w-0 flex-wrap gap-x-3 gap-y-0.5 text-neutral-300">
          {pick(info.host, HOST_ORDER).map(([k, v]) => (
            <span key={k} className="whitespace-nowrap">{LABEL[k] ?? k} <b className="font-mono font-normal text-neutral-200">{v}</b></span>
          ))}
          {pick(info.host, HOST_ORDER).length === 0 && (
            <span className="text-neutral-600">— (서비스 관리 데몬 piper-unitd 가 말합니다)</span>
          )}
        </div>
        <span className="whitespace-nowrap text-neutral-500">데몬</span>
        <div className="flex min-w-0 flex-wrap gap-x-3 gap-y-0.5 text-neutral-300">
          {daemonRows.map(([d, vs]) => (
            // ⚠ 데몬 하나를 통째로 nowrap 으로 묶으면 그 묶음이 칸보다 길 때 또 넘친다
            //   (robotd 는 패키지가 넷이다). 묶음도 접히게 하고 이름·패키지 하나씩만 붙든다.
            <span key={d} className="inline-flex min-w-0 flex-wrap items-baseline gap-x-1">
              <span className="whitespace-nowrap">{d}</span>
              {vs.map(([k, v]) => (
                <span key={k} className="whitespace-nowrap">{LABEL[k] ?? k.replace(/^piper-/, 'piper_')} <b className="font-mono font-normal text-neutral-200">{v}</b></span>
              ))}
            </span>
          ))}
          {daemonRows.length === 0 && <span className="text-neutral-600">— (데몬 자기 보고에서 옵니다)</span>}
        </div>
      </div>

      {others.length > 0 && canControl && (
        <div className="flex items-center gap-2 text-xs text-neutral-400">
          <span>받아 둔 다른 버전:</span>
          <select value={rollbackTo} onChange={(e) => setRollbackTo(e.target.value)}
            className="rounded bg-neutral-700 border border-neutral-600 px-2 py-1 text-xs">
            <option value="">선택</option>
            {others.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
          <button onClick={() => apply(rollbackTo, true)} disabled={!rollbackTo || busy || !!status?.active}
            className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-50">
            이 버전으로
          </button>
        </div>
      )}
    </div>
  )
}
