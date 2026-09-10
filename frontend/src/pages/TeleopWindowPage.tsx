import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../services/api'

/**
 * 조종 창 — 별도 브라우저 창 (feature/web-leader.md §5).
 *
 * 앱 셸 없이 카메라 + HUD 만. 이 창의 모든 키·버튼·휠·이동이 조종이다. 브라우저는
 * "지금 눌린 것"과 마우스 델타를 30Hz 로 보내고, 자세는 게이트웨이가 적분한다 —
 * 입력이 100ms 끊기면 게이트웨이가 리더를 세운다. 창을 닫거나(pagehide) 포커스를
 * 잃으면(blur) 즉시 정지 상태를 보낸다. E-stop heartbeat 도 이 창이 보낸다.
 *
 * HUD 는 DOM 이다 — 수집 프레임은 카메라 데몬의 shm 원본이라 오버레이는 데이터에 안 들어간다.
 */

type Status = {
  running: boolean; relaying?: boolean; publishing?: boolean; speed_scale?: number; homing?: boolean; follower?: string; mode?: 'joint' | 'ee'; selected?: string; pose_lock?: boolean
  pose?: Record<string, number>; ee?: { x: number; y: number; z: number; roll: number; pitch: number; yaw: number } | null
  blocked?: string; input_age?: number | null
  relay?: { running: boolean; holding: boolean; sent: number; stale: boolean }
}
type Cam = { id: string; name?: string; label?: string; connected?: boolean }

const KEY_OF: Record<string, string> = {
  KeyQ: 'q', KeyA: 'a', KeyW: 'w', KeyS: 's', KeyE: 'e', KeyD: 'd', KeyR: 'r', KeyF: 'f', KeyG: 'g',   // KeyT 는 팔 리셋(가로챔)
  KeyY: 'y', KeyH: 'h', KeyI: 'i', KeyK: 'k', KeyJ: 'j', KeyL: 'l', KeyU: 'u', KeyO: 'o',
  BracketLeft: '[', BracketRight: ']',
}
const SELECT_OF: Record<string, string> = {
  Digit1: 'joint1', Digit2: 'joint2', Digit3: 'joint3', Digit4: 'joint4', Digit5: 'joint5', Digit6: 'joint6', Digit7: 'gripper',
}
const JOINT_HELP = 'Q/A W/S E/D R/F T/G Y/H 관절 1~6 · [ ] 그리퍼 · 1~7 선택 후 휠 · 우드래그 j1/j2 (Shift: j4/j3) · 가운데 드래그 j6/j5 · Shift 빠르게 · Ctrl 미세'
const EE_HELP = '마우스 이동 = 앞뒤·좌우 · 휠 = 위아래 · Q/A 롤 · W/S 피치 · E/D 요 (마우스와 동시) · 왼쪽 닫기 · 오른쪽 열기 · 가운데 자세고정 · Ctrl 미세'

export default function TeleopWindowPage() {
  const params = new URLSearchParams(window.location.search)
  const follower = params.get('follower') ?? ''
  const [st, setSt] = useState<Status>({ running: false })
  const [cams, setCams] = useState<Cam[]>([])
  const [big, setBig] = useState<string>('')
  const [locked, setLocked] = useState(false)
  const [mode, setMode] = useState<'joint' | 'ee'>('joint')
  const [help, setHelp] = useState(false)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)   // 카메라 준비 전엔 화면을 덮어 오조작 막는다
  const [videoFps, setVideoFps] = useState<number | null>(null)   // 큰 스트림 실측 fps (null=아직/멈춤)
  const [snap, setSnap] = useState(0)             // 작은 카메라 스냅샷 폴링 틱
  const [bigTick, setBigTick] = useState(0)       // 큰 화면 스냅샷 폴링 틱 (로드 완료마다 자기속도로 전진)
  const [inputLive, setInputLive] = useState(false)  // 릴레이 전송이 늘고 있나 (입력이 실제로 팔에 가나)
  const frameAt = useRef<number[]>([])            // 최근 프레임 시각들
  const lastSent = useRef<{ n: number; at: number }>({ n: -1, at: 0 })
  const keys = useRef<Set<string>>(new Set())
  const mouse = useRef({ dx: 0, dy: 0, wheel: 0, buttons: new Set<number>() })
  const mods = useRef({ shift: false, ctrl: false })
  const pending = useRef<{ click?: string; toggle_lock?: boolean; select?: string; mode?: string }>({})
  const pressAt = useRef<Record<number, number>>({})
  const arena = useRef<HTMLDivElement | null>(null)
  const running = useRef(false)

  // 카메라: 연결된 것 전부 — 시뮬 팔이면 sim: 카메라를 붙인다
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        let want: Cam[]
        if (follower.startsWith('sim_')) {
          // ⚠ 시뮬은 `/cameras/scan`(실기 카메라 프로브 ~5초)을 건너뛴다 — sim 카메라는
          //   top·front·wrist 로 정해져 있으니 바로 연결한다(연결은 멱등·빠름). 창이 5초
          //   빈 화면으로 있던 원인(사용자 보고 2026).
          const ids = ['sim:top', 'sim:front', 'sim:wrist']
          const ok = await Promise.all(ids.map((id) =>
            api.post('/cameras/connect', { id }).then(() => true).catch(() => false)))
          want = ids.filter((_, i) => ok[i]).map((id) => ({ id, connected: true }))
        } else {
          const list = await api.get<Cam[] | { cameras: Cam[] }>('/cameras/scan')
          const arr = Array.isArray(list) ? list : (list.cameras ?? [])
          want = arr.filter((c) => c.connected)
          for (const c of want) if (!c.connected) { try { await api.post('/cameras/connect', { id: c.id }) } catch { /* 이미 연결 */ } }
        }
        if (cancelled) return
        setCams(want); if (want[0]) setBig(want[0].id)
        // 첫 프레임이 스트림에 실릴 틈을 주고 화면을 연다
        window.setTimeout(() => { if (!cancelled) setLoading(false) }, want.length ? 700 : 0)
        if (!want.length) setErr('연결된 카메라가 없습니다')
      } catch (e) { if (!cancelled) { setErr(e instanceof Error ? e.message : '카메라 목록 실패'); setLoading(false) } }
    })()
    return () => { cancelled = true }
  }, [follower])

  // 상태 5Hz
  useEffect(() => {
    const iv = window.setInterval(() => api.get<Status>('/leader/web/status').then((s) => {
      setSt(s); running.current = !!s.running; if (s.mode) setMode(s.mode)
      const sent = s.relay?.sent ?? 0; const now = performance.now()
      if (sent !== lastSent.current.n) { lastSent.current = { n: sent, at: now } }
      setInputLive(!!s.running && now - lastSent.current.at < 700)   // 최근 전송이 늘었나
    }).catch(() => {}), 200)
    return () => window.clearInterval(iv)
  }, [])

  // ⚠ **모든 카메라가 스냅샷 폴링(짧은 요청)** — 영구 MJPEG 스트림이 아니다. 스트림은
  //   브라우저 HTTP/1.1 연결(6개)을 영구 점유해 30Hz 입력 POST 를 굶겼고(사용자 보고 2026:
  //   중간중간 멈춤; 서버는 3스트림 다 풀 fps 였다), MJPEG 의 onLoad 는 Chrome 에서 프레임마다
  //   안 와 "영상 멈춤" 오판까지 냈다. 시뮬은 스트림 없이도 shm 에 계속 렌더한다(실측 8/8 갱신).
  useEffect(() => {
    const iv = window.setInterval(() => setSnap((s) => s + 1), 250)   // 작은 카메라 4fps
    return () => window.clearInterval(iv)
  }, [])
  // 큰 화면: 로드가 끝날 때마다 다음 프레임을 건다(자기속도) — 브라우저가 느리면 저절로
  // 느려질 뿐, 프레임을 취소해 놓쳐 "멈춤"으로 오판하지 않는다. 첫 프레임만 킥.
  useEffect(() => { if (big && !loading) setBigTick((n) => n + 1) }, [big, loading])
  // 영상 살아있나: onLoad(스냅샷은 프레임마다 확실히 온다)로 fps·정지 판정
  useEffect(() => {
    const iv = window.setInterval(() => {
      const now = performance.now()
      frameAt.current = frameAt.current.filter((tS) => now - tS < 2000)
      const recent = frameAt.current.filter((tS) => now - tS < 1000).length
      setVideoFps(frameAt.current.length && now - frameAt.current[frameAt.current.length - 1] < 1500 ? recent : null)
    }, 500)
    return () => window.clearInterval(iv)
  }, [])

  // E-stop heartbeat — 이 창도 보낸다 (텔레옵은 E-stop 대상)
  useEffect(() => {
    const iv = window.setInterval(() => api.post('/estop/heartbeat', { via: 'http', hidden: document.hidden, source: 'teleop' }).catch(() => {}), 500)
    return () => window.clearInterval(iv)
  }, [])

  const sendState = useCallback(async (zero = false) => {
    if (!running.current) return
    const m = mouse.current
    const body = zero
      ? { keys: [], mouse: { dx: 0, dy: 0, wheel: 0, buttons: [] }, shift: false, ctrl: false }
      : { keys: Array.from(keys.current), mouse: { dx: m.dx, dy: m.dy, wheel: m.wheel, buttons: Array.from(m.buttons) },
          shift: mods.current.shift, ctrl: mods.current.ctrl, ...pending.current }
    m.dx = 0; m.dy = 0; m.wheel = 0; pending.current = {}
    try { await api.post('/leader/web/input', body) } catch { /* 세션 없음 — 상태 폴링이 보여 준다 */ }
  }, [])

  // 30Hz 전송
  useEffect(() => {
    const iv = window.setInterval(() => { sendState(false) }, 33)
    return () => window.clearInterval(iv)
  }, [sendState])

  const start = async () => {
    setErr('')
    try { await api.post('/leader/web/start', { follower, mode }); running.current = true }
    catch (e) { setErr(e instanceof Error ? e.message : '시작 실패'); return false }
    return true
  }
  const stop = useCallback(async () => {
    running.current = false
    try { await api.post('/leader/web/stop', {}) } catch { /* 이미 끝남 */ }
  }, [])

  const [resetting, setResetting] = useState(false)
  const resetWorld = useCallback(async (armOnly = false) => {
    // 리셋 — 큐브까지(환경 리셋) 또는 팔만(T). 조종 중이면 리더도 파킹으로(백엔드가 처리).
    setResetting(true)
    try { await api.post('/leader/web/reset', { follower, arm_only: armOnly }) }
    catch (e) { setErr(e instanceof Error ? e.message : '리셋 실패') }
    finally { setResetting(false) }
  }, [follower])

  // 포인터 락 — 창을 클릭해야 걸린다. 그 클릭은 그리퍼가 아니다.
  const onArenaClick = async () => {
    if (loading) return                    // 준비 전 클릭 무시
    if (document.pointerLockElement === arena.current) return
    if (!running.current) { if (!(await start())) return }
    arena.current?.requestPointerLock()
  }
  useEffect(() => {
    const onChange = () => { const on = document.pointerLockElement === arena.current; setLocked(on); if (!on) { keys.current.clear(); mouse.current.buttons.clear(); sendState(true) } }
    document.addEventListener('pointerlockchange', onChange)
    return () => document.removeEventListener('pointerlockchange', onChange)
  }, [sendState])

  // 키
  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.code === 'Tab') { e.preventDefault(); const next = mode === 'joint' ? 'ee' : 'joint'; setMode(next); pending.current.mode = next; return }
      if (e.code === 'Space') { e.preventDefault(); keys.current.clear(); mouse.current.buttons.clear(); sendState(true); return }
      if (e.code === 'Slash' && e.shiftKey) { setHelp((h) => !h); return }
      if (e.code === 'KeyX') { pending.current.toggle_lock = true; return }
      if (e.code === 'KeyR' && !e.repeat) { e.preventDefault(); resetWorld(false); return }
      if (e.code === 'KeyT' && !e.repeat) { e.preventDefault(); resetWorld(true); return }
      if (SELECT_OF[e.code]) { pending.current.select = SELECT_OF[e.code]; return }
      const k = KEY_OF[e.code]; if (k) { e.preventDefault(); keys.current.add(k) }
      mods.current = { shift: e.shiftKey, ctrl: e.ctrlKey }
    }
    const up = (e: KeyboardEvent) => { const k = KEY_OF[e.code]; if (k) keys.current.delete(k); mods.current = { shift: e.shiftKey, ctrl: e.ctrlKey } }
    window.addEventListener('keydown', down); window.addEventListener('keyup', up)
    return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up) }
  }, [mode, sendState, resetWorld])

  // 마우스 — 락이 걸린 동안만
  useEffect(() => {
    const move = (e: MouseEvent) => { if (document.pointerLockElement !== arena.current) return; mouse.current.dx += e.movementX; mouse.current.dy += e.movementY; mods.current = { shift: e.shiftKey, ctrl: e.ctrlKey } }
    const wheel = (e: WheelEvent) => { if (document.pointerLockElement !== arena.current) return; e.preventDefault(); mouse.current.wheel += Math.sign(e.deltaY); mods.current = { shift: e.shiftKey, ctrl: e.ctrlKey } }
    const down = (e: MouseEvent) => { if (document.pointerLockElement !== arena.current) return; e.preventDefault(); if (e.button === 1) { pending.current.toggle_lock = true; return } mouse.current.buttons.add(e.button); pressAt.current[e.button] = performance.now() }
    const up = (e: MouseEvent) => { if (document.pointerLockElement !== arena.current) return; mouse.current.buttons.delete(e.button); const held = performance.now() - (pressAt.current[e.button] ?? 0); if (held < 150 && (e.button === 0 || e.button === 2)) pending.current.click = e.button === 0 ? 'close' : 'open' }
    const ctx = (e: Event) => e.preventDefault()
    window.addEventListener('mousemove', move); window.addEventListener('wheel', wheel, { passive: false })
    window.addEventListener('mousedown', down); window.addEventListener('mouseup', up); window.addEventListener('contextmenu', ctx)
    return () => { window.removeEventListener('mousemove', move); window.removeEventListener('wheel', wheel); window.removeEventListener('mousedown', down); window.removeEventListener('mouseup', up); window.removeEventListener('contextmenu', ctx) }
  }, [])

  // 포커스 잃음·숨김 → 정지 상태, 창 닫힘 → 끝
  useEffect(() => {
    const zero = () => { keys.current.clear(); mouse.current.buttons.clear(); sendState(true) }
    const bye = () => { navigator.sendBeacon('/api/leader/web/stop', new Blob(['{}'], { type: 'application/json' })) }
    window.addEventListener('blur', zero); document.addEventListener('visibilitychange', zero); window.addEventListener('pagehide', bye)
    return () => { window.removeEventListener('blur', zero); document.removeEventListener('visibilitychange', zero); window.removeEventListener('pagehide', bye) }
  }, [sendState])

  const others = cams.filter((c) => c.id !== big)
  const p = st.pose ?? {}
  const real = !follower.startsWith('sim_')
  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-100 flex flex-col select-none">
      <div className="flex items-center justify-between px-3 py-1.5 text-xs bg-neutral-900 border-b border-neutral-800">
        <div className="flex items-center gap-3">
          <span className="font-semibold">조종 — {follower || '(팔로워 없음)'}</span>
          {real && <span className="rounded bg-red-700/60 px-1.5 py-0.5 text-red-100">실기</span>}
          <span className={`px-1.5 py-0.5 rounded ${mode === 'joint' ? 'bg-blue-600/40' : 'bg-neutral-700'}`}>관절</span>
          <span className={`px-1.5 py-0.5 rounded ${mode === 'ee' ? 'bg-blue-600/40' : 'bg-neutral-700'}`}>EE</span>
          <span className="text-neutral-500">Tab 전환 · Ctrl 미세{mode === 'ee' ? ' · Shift 회전' : ' · Shift 빠르게'}</span>
          {st.pose_lock && <span className="text-amber-300">자세 고정</span>}
        </div>
        <div className="flex items-center gap-2">
          <span className={st.running ? (locked ? 'text-emerald-400' : 'text-amber-300') : 'text-neutral-500'}>
            {st.running
              ? (st.relaying === false ? '● 수집 중 — 녹화가 팔을 움직임, 여기선 입력만' : locked ? '● 조종 중 — Esc 끝' : '○ 클릭하면 이어서')
              : '클릭해서 시작'}
          </span>
          {/* 멈춘 게 영상인지 입력인지 구분: 영상=스트림 fps, 입력=팔에 전송 중인가 */}
          <span className={videoFps ? 'text-neutral-500' : 'text-red-400'} title="큰 화면 스트림 상태">
            영상 {videoFps ? `${videoFps}fps` : '멈춤'}
          </span>
          {locked && (
            <span className={inputLive ? 'text-neutral-500' : 'text-amber-400'} title="입력이 실제로 팔에 전달되는가">
              입력 {inputLive ? '전달 중' : '대기'}
            </span>
          )}
          {st.speed_scale !== undefined && st.speed_scale < 1 && <span className="text-red-300">속도 ×{st.speed_scale}</span>}
          {st.homing && <span className="text-cyan-300">원점 복귀 중… (조작하면 인계)</span>}
          <button onClick={() => resetWorld(true)} disabled={resetting} title="팔만 파킹으로 (T) — 큐브·조명 유지"
            className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-neutral-600 disabled:opacity-50">팔 초기화</button>
          <button onClick={() => resetWorld(false)} disabled={resetting} title="큐브를 시작 위치로, 팔을 파킹으로 (R)"
            className="px-2 py-0.5 rounded bg-amber-700 hover:bg-amber-600 disabled:opacity-50">{resetting ? '리셋 중…' : '환경 리셋'}</button>
          {st.running && <button onClick={stop} className="px-2 py-0.5 rounded bg-red-700 hover:bg-red-600">끝내기</button>}
          <button onClick={() => setHelp((h) => !h)} className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-neutral-600">?</button>
        </div>
      </div>
      <div ref={arena} onClick={onArenaClick} className={`relative flex-1 grid grid-cols-[3fr_1fr] gap-1 p-1 ${loading ? 'cursor-wait' : 'cursor-crosshair'}`}>
        {loading && (
          <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-neutral-950/85"
               onClickCapture={(e) => e.stopPropagation()}>
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-neutral-600 border-t-blue-400" />
            <p className="text-sm text-neutral-300">조종 화면 준비 중…</p>
            <p className="text-[11px] text-neutral-500">카메라를 연결하고 있습니다 — 잠시만요</p>
          </div>
        )}
        <div className="relative bg-black rounded overflow-hidden">
          {big ? <img src={`/api/cameras/${encodeURIComponent(big)}/preview?t=${bigTick}`} alt={big}
                      onLoad={() => { frameAt.current.push(performance.now()); window.setTimeout(() => setBigTick((n) => n + 1), 66) }}
                      onError={() => window.setTimeout(() => setBigTick((n) => n + 1), 200)}
                      className="w-full h-full object-contain" draggable={false} />
            : <p className="p-4 text-sm text-neutral-500">카메라가 없습니다</p>}
          <span className="absolute left-2 top-2 text-[11px] text-neutral-300 bg-black/50 px-1 rounded">{big}</span>
          {!loading && (
            <span className={`absolute right-2 top-2 text-[11px] px-1.5 rounded ${videoFps ? 'bg-black/50 text-neutral-300' : 'bg-red-700/80 text-red-100'}`}>
              {videoFps ? `${videoFps}fps` : '영상 멈춤'}
            </span>
          )}
        </div>
        <div className="flex flex-col gap-1">
          {others.map((c) => (
            <div key={c.id} onClick={(e) => { e.stopPropagation(); setBig(c.id) }} className="relative bg-black rounded overflow-hidden cursor-pointer">
              {/* 스냅샷 폴링(스트림 아님) — 연결을 물지 않는다. 틱마다 새 요청. */}
              <img src={`/api/cameras/${encodeURIComponent(c.id)}/preview?t=${snap}`} alt={c.id} className="w-full object-contain" draggable={false} />
              <span className="absolute left-1 top-1 text-[10px] text-neutral-300 bg-black/50 px-1 rounded">{c.id}</span>
            </div>
          ))}
        </div>
        {help && (
          <div className="absolute inset-x-8 top-8 rounded border border-neutral-600 bg-neutral-900/95 p-4 text-xs text-neutral-200 space-y-2" onClick={(e) => e.stopPropagation()}>
            <p><b>관절 모드:</b> {JOINT_HELP}</p>
            <p><b>EE 모드:</b> {EE_HELP}</p>
            <p><b>공통:</b> Tab 모드 전환 · Space 정지 · Esc 락 해제 · X 자세 고정 · R 환경 리셋 · T 팔 초기화 · ? 이 도움말</p>
          </div>
        )}
      </div>
      <div className="px-3 py-1.5 text-[11px] font-mono bg-neutral-900 border-t border-neutral-800 flex flex-wrap items-center gap-x-4 gap-y-1">
        {['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6', 'gripper'].map((j, i) => (
          <span key={j} className={st.selected === j ? 'text-blue-300' : 'text-neutral-300'}>
            {i < 6 ? `J${i + 1}` : 'G'} {p[j] !== undefined ? p[j].toFixed(0) : '—'}
          </span>
        ))}
        {st.ee && <span className="text-neutral-400">x {st.ee.x.toFixed(3)} y {st.ee.y.toFixed(3)} z {st.ee.z.toFixed(3)} · r {st.ee.roll} p {st.ee.pitch} y {st.ee.yaw}</span>}
        {st.relay && <span className={st.relay.stale ? 'text-amber-300' : 'text-neutral-500'}>릴레이 {st.relay.running ? `전송 ${st.relay.sent}` : '없음'}{st.relay.stale ? ' · 리더 낡음' : ''}</span>}
        {st.blocked && <span className="text-amber-300">⚠ {st.blocked}</span>}
        {err && <span className="text-red-400">{err}</span>}
      </div>
    </div>
  )
}
