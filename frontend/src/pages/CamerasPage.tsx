import { useEffect, useState } from 'react'
import PresetBar from '../components/PresetBar'
import { useSystemMessage } from '../components/SystemMessages'
import ParamSlider from '../components/ParamSlider'
import { api } from '../services/api'

/**
 * USB 2.0 경고 배지.
 *
 * 왜 문구를 프론트에서 안 만드나: 임계값(USB3 = 5000Mbps)과 판정을 백엔드가
 * 갖고 있다. 화면이 그걸 다시 적으면 한쪽만 고쳐져 어긋난다 —
 * `camera_manager._usb_warning` 이 정본이다.
 */
function UsbWarning({ reason }: { reason: string }) {
  return (
    <span
      title={reason}
      className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-500"
    >
      USB 2.0
    </span>
  )
}

/** 프로파일 적용 결과 한 줄. 데몬이 세어 준 것을 그대로 보여준다.
 *
 *  `잠김`은 실패가 아니다 — 자동 노출이 켜져 있어 그 값이 지금 안 쓰이는 상태다.
 *  실패로 칠하면 사용자가 고칠 수 없는 빨간 배지를 계속 보게 된다. */
function ApplyBadge({ r }: { r: CamApplyResult }) {
  const bad = r.failed > 0
  return (
    <span
      title={(r.details ?? []).map((d) => `${d.name}: ${d.want} → ${d.got ?? '—'} (${d.status})`).join('\n')}
      className={`rounded px-1.5 py-0.5 text-[10px] ${bad
        ? 'bg-red-500/15 text-red-400' : 'bg-neutral-700/60 text-neutral-300'}`}
    >
      {r.display_name}: 적용 {r.applied}
      {r.locked ? ` / 잠김 ${r.locked}` : ''}
      {r.failed ? ` / 실패 ${r.failed}` : ''}
      {r.truncated ? ' / 시간초과' : ''}
    </span>
  )
}

function Spinner({ className = '' }: { className?: string }) {
  return (
    <svg className={`animate-spin h-4 w-4 ${className}`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

type CamInfo = {
  id: string; name: string; usb_port?: string; cam_type: string
  /** 백엔드가 판정해서 준다 — 화면이 임계값을 따로 적으면 한쪽만 고쳐져 어긋난다. */
  usb_warning?: string | null
  /** 사람이 붙인 별칭("탑뷰"). LeRobot 카메라 키와는 별개 — 화면 표시용이다. */
  label?: string
  /** `label || name`. 각 화면이 따로 계산하지 않도록 백엔드가 준다. */
  display_name?: string
  connected: boolean; ready: boolean; has_preview: boolean
  /** 마지막 스캔에서 데몬이 이 장치를 봤는가. `connected` 와 다른 사실이다 —
   *  `present && !connected` 는 꽂혀 있는데 안 연 것이고, `!present` 는 아예 없는 것.
   *  예전엔 둘 다 `connected: false` 라 뽑아둔 카메라가 목록에 그대로 남았다. */
  present?: boolean
  /** 프레임이 **지금도** 오고 있는가. `has_preview` 와 다르다 — 세그먼트에 마지막
   *  프레임이 남아 있으면 그건 true 지만 스트림은 죽었을 수 있다. 그래서 뽑힌
   *  카메라가 화면에서 정상처럼 보였다. */
  streaming?: boolean
  /** RealSense 스트림 종류. depth 일 때만 깊이 인코딩 설정이 뜬다. */
  stream_type?: string
  /** rsd 가 소유하는 깊이 인코딩 파라미터 — 데이터셋 해석의 근거다. */
  depth_encoding?: { near_mm: number; far_mm: number; mode: string } | null
  /** raw 한 단위가 몇 미터인가. D435=0.001, **D405=0.0001**. */
  depth_units_m?: number | null
  config: { width: number | null; height: number | null; fps: number | null; color_mode: string; rotation: number; fourcc: string | null }
}

type CamControl = {
  cid: number; name: string; label: string; type: number
  min: number; max: number; step: number; default: number; value: number
  inactive?: boolean; readonly?: boolean
}

/** 카메라 한 대의 프로파일 적용 결과. **집계는 데몬이 한다** —
 *  `locked`(자동 모드가 잠금)와 `failed`(진짜 실패)의 구분이 거기 있다. */
type CamApplyResult = {
  cam_id: string; display_name: string
  applied: number; locked: number; failed: number; skipped: number
  truncated?: boolean
  details?: { name: string; want: number; got: number | null; status: string }[]
}

type ProfileReport = {
  profile: string
  cameras: CamApplyResult[]
  unmatched?: string[]
  error?: string
}

/** rsd `DepthEncoding` 의 기본값과 같아야 한다 (rs/piper_rs/depth.py). */
const DEPTH_DEFAULT = { near_mm: 150, far_mm: 1200 }

/** 슬라이더 눈금. 1mm 단위로 끌게 하면 손이 떨려도 값이 바뀐다. */
const DEPTH_STEP = 10

/**
 * 슬라이더가 덮는 범위. D405 는 70~500mm 짜리 근접 카메라고 D435 는 0.3~3m 라
 * 한 값으로 둘 다 잘 담을 수 없다 — 흔히 쓰는 구간을 덮고, 그 밖의 값은
 * **숫자 입력으로** 넣게 둔다. 지금 값이 밖에 있으면 눈금을 늘려 손잡이가
 * 끝에 붙어 보이지 않게 한다.
 */
const DEPTH_SLIDER_MAX = 2000

export default function CamerasPage() {
  // ⚠ `window.alert` 를 쓰지 않는다 — 이벤트 루프를 막아 E-stop heartbeat 가
  //   끊기고, 2초 타임아웃에 추론이 강제 종료된다 (confirm 으로 실제로 겪었다).
  const { notify, confirm: askConfirm } = useSystemMessage()
  const notifyError = (text: string) =>
    notify({ level: 'error', text, source: '카메라' })
  const [loading, setLoading] = useState(true)
  const [cams, setCams] = useState<CamInfo[]>([])
  const [scanning, setScanning] = useState(false)
  const [connectingId, setConnectingId] = useState<string | null>(null)
  // 미등록 카드에서 입력 중인 별칭 (등록 버튼을 누를 때 함께 보낸다)
  const [labelDraft, setLabelDraft] = useState<Record<string, string>>({})
  // 설정은 **모달로 띄운다.** 카드 안에서 펼치면 그리드 행 높이가 늘어나
  // 같은 줄의 다른 카드까지 함께 커진다.
  const [settingsCam, setSettingsCam] = useState<string | null>(null)
  // 서버가 아는 값. 슬라이더가 보여주는 값(`depthDraft`)과 따로 둔다 — 드래그
  // 중에는 아직 안 보냈고, 거부되면 이 값으로 되돌아가야 한다.
  const [depthEnc, setDepthEnc] = useState<{ near_mm: number; far_mm: number } | null>(null)
  const [depthDraft, setDepthDraft] = useState<{ near_mm: number; far_mm: number }>(DEPTH_DEFAULT)
  // 프로파일 — 노출·화이트밸런스 같은 컨트롤 값을 이름 붙여 저장한다.
  // 적용 자체는 **데몬이 카메라를 열 때** 하므로 여기는 저장·수동적용·결과 표시만 한다.
  const [profileReport, setProfileReport] = useState<ProfileReport | null>(null)

  /** 지금 장치에 들어 있는 값을 읽어 저장한다. 화면 상태가 아니라 **장치**가 출처다 —
   *  그래서 공통 프리셋 저장 API 를 그대로 못 쓰고 전용 엔드포인트를 탄다. */
  const captureProfile = async (name: string) => {
    const r = await api.post<{ values: { cameras: unknown[] } }>(
      '/cameras/profiles/capture', { name })
    const n = r.values?.cameras?.length ?? 0
    return `"${name}" 저장 — 카메라 ${n}대의 현재 값 (이제부터 연결할 때 자동 적용)`
  }

  /** 수동 적용. 연결 시 자동 적용과 **같은 데몬 함수**를 탄다 —
   *  경로가 갈리면 "수동으로는 되는데 자동으로는 안 된다"가 생긴다. */
  const applyProfile = async (name: string) => {
    const r = await api.post<ProfileReport>('/cameras/profiles/apply', { name })
    await api.post('/cameras/profiles/active', { name })
    setProfileReport(r)
    if (r.error) return r.error
    const sum = r.cameras.reduce((a, c) => ({
      applied: a.applied + c.applied, locked: a.locked + c.locked, failed: a.failed + c.failed,
    }), { applied: 0, locked: 0, failed: 0 })
    const miss = r.unmatched?.length ? ` / 못 찾음 ${r.unmatched.length}대` : ''
    return `"${name}" 적용 — ${sum.applied} 적용 / ${sum.locked} 잠김 / ${sum.failed} 실패${miss}`
  }

  /** 깊이 범위 변경. **한쪽만 바꿔도 다른 쪽 값을 함께 보낸다** —
   *  백엔드는 구간을 한 벌로 검증하므로(far > near) 절반만 보내면 거부된다.
   *
   *  ⚠ 거부되면 **서버가 아는 값**으로 되돌린다. 드래그하던 값으로 되돌리면
   *  화면은 바뀐 척하는데 장치는 안 바뀐 상태가 된다 — 깊이 범위는 녹화한
   *  데이터의 해석이 걸린 값이라 그 거짓말이 특히 비싸다. */
  const setDepthRange = async (id: string, near: number | null, far: number | null) => {
    const server = depthEnc ?? DEPTH_DEFAULT
    // ⚠ 빈 자리는 **화면에 보이는 값**(초안)으로 채운다. 서버 값으로 채우면
    //   한쪽을 끌어놓고 다른 쪽을 만졌을 때 먼저 끈 값이 조용히 사라진다.
    const body = { near_mm: near ?? depthDraft.near_mm, far_mm: far ?? depthDraft.far_mm }
    if (body.near_mm === server.near_mm && body.far_mm === server.far_mm) return
    try {
      const r = await api.post<{ encoding: { near_mm: number; far_mm: number } }>(
        `/cameras/${encodeURIComponent(id)}/depth-encoding`, body)
      setDepthEnc(r.encoding)
      setDepthDraft(r.encoding)
      bumpPreview([id])
    } catch (e) {
      // 거부 사유(far <= near, 녹화 중)는 백엔드가 문장으로 준다
      notifyError(e instanceof Error ? e.message : '깊이 범위를 바꾸지 못했습니다')
      setDepthDraft(server)
    }
  }
  // 프리뷰 캐시버스팅 타임스탬프 — 카메라별로 따로 관리해야 한 카메라 동작이
  // 다른 타일까지 새로고침시키지 않는다.
  const [previewTs, setPreviewTs] = useState<Record<string, number>>({})
  const [liveIds, setLiveIds] = useState<Set<string>>(new Set())
  const [controlsCam, setControlsCam] = useState<string | null>(null)
  const [controls, setControls] = useState<CamControl[]>([])
  // 디바이스 작업(초기화/업데이트)이 진행 중인 카메라 — 같은 디바이스에 동시 요청을
  // 막는다 (RealSense 등에서 컨트롤 질의와 스트림 probe 가 충돌하면 멈춤).
  const [busyId, setBusyId] = useState<string | null>(null)

  // 지정한 카메라들의 프리뷰만 새로고침 (캐시버스팅 타임스탬프 갱신)
  const bumpPreview = (ids: string[]) => {
    const now = Date.now()
    setPreviewTs((prev) => {
      const next = { ...prev }
      for (const id of ids) next[id] = now
      return next
    })
  }

  useEffect(() => {
    api.get<{ cameras: CamInfo[] }>('/cameras/current')
      .then((r) => {
        if (r.cameras) {
          setCams(r.cameras)
          bumpPreview(r.cameras.filter((c) => c.connected).map((c) => c.id))
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // ⚠ **목록을 주기적으로 다시 받는다.** 예전에는 마운트 때 한 번뿐이라, 다른 탭에서
  //   경보를 보고 카메라 페이지로 와도 뽑힌 카메라가 **멀쩡한 것처럼** 남아 있었다.
  //   백엔드가 감시 주기(2초)마다 `present` 를 내리므로 여기서 그걸 가져오면 된다.
  useEffect(() => {
    const id = setInterval(() => {
      api.get<{ cameras: CamInfo[] }>('/cameras/current')
        .then((r) => { if (r.cameras) setCams(r.cameras) })
        .catch(() => {})
    }, 3000)
    return () => clearInterval(id)
  }, [])

  // 프리뷰 자동 갱신 (실시간 보기 중인 카메라)
  useEffect(() => {
    if (liveIds.size === 0) return
    const interval = setInterval(() => bumpPreview([...liveIds]), 200)
    return () => clearInterval(interval)
  }, [liveIds])

  // 모달은 Esc 로도 닫힌다
  useEffect(() => {
    if (!settingsCam) { setDepthEnc(null); setDepthDraft(DEPTH_DEFAULT); return }
    // 현재 값을 먼저 채운다 — 기본값을 보여주면 사용자가 "그 값이다"라고 믿는다
    const c = cams.find((x) => x.id === settingsCam)
    if (c?.depth_encoding) { setDepthEnc(c.depth_encoding); setDepthDraft(c.depth_encoding) }
    else setDepthDraft(DEPTH_DEFAULT)
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') closeSettings() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [settingsCam])

  // 설정 모달이 열려 있는 동안 프리뷰를 갱신한다 — 밝기·노출·회전을 만지면서
  // 결과를 못 보면 설정 자체가 의미가 없다.
  //
  // `liveIds` 에 넣지 않는 이유: 그건 카드의 "실시간 보기/중단" 버튼 상태를 겸해서,
  // 모달을 열었다고 카드가 "중단"으로 바뀌면 사용자가 헷갈린다.
  useEffect(() => {
    if (!settingsCam) return
    const id = setInterval(() => bumpPreview([settingsCam]), 200)
    return () => clearInterval(id)
  }, [settingsCam])

  // 1단계: 스캔 (auto_connect=true → 병렬 연결 + 프리뷰)
  const handleScan = async () => {
    setScanning(true)
    try {
      const result = await api.get<CamInfo[]>('/cameras/scan?auto_connect=true')
      setCams(result)
      bumpPreview(result.map((c) => c.id))
    } catch {}
    setScanning(false)
  }

  const handleDisconnect = async (id: string) => {
    await api.post('/cameras/disconnect', { id })
    // 연결 해제는 `connected` 만 내린다 — 등록(`ready`)은 백엔드가 유지하므로
    // 여기서 같이 내리면 새로고침 때 값이 되살아나 화면이 어긋난다.
    setCams((prev) => prev.map((c) => c.id === id ? { ...c, connected: false } : c))
  }

  // 설정
  const handleConfig = async (id: string, cfg: Record<string, unknown>) => {
    const updated = await api.post<CamInfo>('/cameras/config', { id, config: cfg })
    setCams((prev) => prev.map((c) => (c.id === id ? updated : c)))
  }

  // 등록 (자동 connect 포함)
  const handleRegister = async (id: string) => {
    setConnectingId(id)
    try {
      const label = (labelDraft[id] ?? '').trim()
      const updated = await api.post<CamInfo>('/cameras/register', { id, label })
      setCams((prev) => prev.map((c) => (c.id === id ? updated : c)))
    } catch { notifyError('등록 실패') }
    finally { setConnectingId(null) }
  }

  // 등록 후에도 별칭을 고칠 수 있다 — 카메라를 옮겨 달면 탑뷰가 손목이 된다.
  const handleLabel = async (id: string, label: string) => {
    try {
      const updated = await api.post<CamInfo>('/cameras/label', { id, label })
      setCams((prev) => prev.map((c) => (c.id === id ? updated : c)))
    } catch { /* 표시 이름일 뿐이라 실패해도 조용히 둔다 */ }
  }
  const handleUnregister = async (id: string) => {
    await api.post('/cameras/unregister', { id })
    setCams((prev) => prev.map((c) => (c.id === id ? { ...c, ready: false } : c)))
  }

  // 설정 모달 열기 — 컨트롤 목록은 열 때 한 번 읽는다.
  // RealSense 는 스트리밍 중 컨트롤 질의가 충돌하면 멈출 수 있어(D405 UVC hang)
  // 필요할 때만 묻는다.
  const openSettings = async (camId: string) => {
    setSettingsCam(camId)
    setControls([])
    setControlsCam(camId)
    try {
      setControls(await api.get<CamControl[]>(`/cameras/${encodeURIComponent(camId)}/controls`))
    } catch { setControls([]) }
  }

  const closeSettings = () => {
    setSettingsCam(null)
    setControlsCam(null)
    setControls([])
  }

  const handleResetControls = async (camId: string) => {
    if (busyId) return
    setBusyId(camId)
    try {
      const data = await api.post<CamControl[]>('/cameras/controls/reset', { id: camId })
      setControls(data)
    } catch {}
    finally { setBusyId(null) }
  }

  // RealSense 하드웨어 리셋 (펌웨어 파워사이클) → 재열거 대기 후 재스캔
  const handleResetDevice = async (camId: string) => {
    if (busyId) return
    if (!await askConfirm('카메라를 하드웨어 리셋합니다.\n수 초간 사라졌다 다시 나타납니다. 계속할까요?')) return
    setBusyId(camId)
    // 실시간 보기/컨트롤 패널 정리 (리셋 중 프리뷰는 무의미)
    setLiveIds((prev) => { const next = new Set(prev); next.delete(camId); return next })
    if (controlsCam === camId) { setControlsCam(null); setControls([]) }
    try {
      await api.post('/cameras/reset-device', { id: camId })
      // 리셋 후 USB 재열거까지 대기 → 재스캔으로 엔트리 갱신
      await new Promise((r) => setTimeout(r, 4000))
      const result = await api.get<CamInfo[]>('/cameras/scan?auto_connect=true')
      setCams(result)
      bumpPreview(result.map((c) => c.id))
    } catch (e) {
      notifyError('하드웨어 리셋 실패: ' + (e instanceof Error ? e.message : '알 수 없는 오류'))
    } finally {
      setBusyId(null)
    }
  }

  // RealSense 카메라에만 노출되는 하드웨어 리셋 버튼
  const ResetDeviceButton = ({ cam }: { cam: CamInfo }) =>
    cam.cam_type === 'realsense' ? (
      <button onClick={() => handleResetDevice(cam.id)} disabled={busyId === cam.id}
        title="하드웨어 리셋 (펌웨어 파워사이클)"
        className="py-1 px-2 text-xs rounded bg-neutral-700 hover:bg-orange-600 text-neutral-300 hover:text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
        {busyId === cam.id ? <Spinner className="inline" /> : '리셋'}
      </button>
    ) : null

  const handleProbe = async (camId: string) => {
    if (busyId) return
    setBusyId(camId)
    try {
      await api.post('/cameras/probe', { id: camId })
      bumpPreview([camId])
    } catch {}
    finally { setBusyId(null) }
  }

  const handleControl = async (camId: string, name: string, value: number) => {
    setControls((prev) => prev.map((c) => c.name === name ? { ...c, value } : c))
    try {
      await api.post('/cameras/control', { id: camId, name, value })
      // auto 계열 변경 시 다른 컨트롤의 inactive 상태가 바뀔 수 있음 → 다시 조회
      if (name.startsWith('auto') || name.includes('automatic') || name.includes('white_balance_automatic')) {
        const data = await api.get<CamControl[]>(`/cameras/${encodeURIComponent(camId)}/controls`)
        setControls(data)
      }
    } catch {}
  }

  // ⚠ 세 그룹은 **배타적이어야 한다.** 등록된 카메라는 프리뷰를 끄면
  // `ready && !connected` 가 되는데(정상 상태 — 등록은 유지, 장치만 놓아준다),
  // 예전 `unconnected` 는 `ready` 를 안 봐서 그 카메라가 "사용 가능"과
  // "미등록"에 **동시에** 떴다. 6대인데 5+2=7 로 보이던 원인.
  // 모달이 최신 카드 데이터를 보게 id 로 찾는다 — 스냅샷을 들고 있으면
  // 설정을 바꿔도 모달 안 값이 안 갱신된다.
  const settingsCamera = settingsCam ? cams.find((c) => c.id === settingsCam) ?? null : null

  // ⚠ **없는 것을 맨 먼저 가른다.** 등록된 카메라는 뽑혀도 목록에 남는데(별칭·매핑을
  //   사람이 정했으므로), 그걸 "사용 가능"에 두면 녹화 매핑에 고를 수 있는 것처럼 보인다.
  //   등록 안 된 것은 백엔드가 목록에서 아예 지운다.
  const absent = cams.filter((c) => c.present === false)
  const here = cams.filter((c) => c.present !== false)
  const ready = here.filter((c) => c.ready)
  const connected = here.filter((c) => c.connected && !c.ready)
  const unconnected = here.filter((c) => !c.connected && !c.ready)

  if (loading) {
    return <div className="flex items-center justify-center h-64 gap-2 text-neutral-400"><Spinner /> 로딩 중...</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">카메라</h1>
        <button onClick={handleScan} disabled={scanning}
          className="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
          {scanning ? <><Spinner className="inline" /> 스캔 중...</> : '스캔'}
        </button>
      </div>

      {/* 프로파일 — 조명 조건별로 여러 개 두는 게 실사용에 맞다(주간/야간/형광등).
          저장하면 그 프로파일이 활성이 되고, 이후 **카메라를 열 때마다** 데몬이
          순서대로(자동 스위치 → 종속 값 → 독립 값) 밀어 넣는다. */}
      <div className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3 space-y-2">
        <PresetBar
          domain="camera"
          values={() => ({})}
          onApply={() => {}}
          onSaveAs={captureProfile}
          onApplyName={applyProfile}
          disabled={scanning}
        />
        <p className="text-[10px] text-neutral-500">
          저장은 <b>지금 장치에 들어 있는 값</b>을 읽어 담는다 — 노출·화이트밸런스를
          맞춘 뒤 저장하면 서버 재시작·USB 재열거·하드웨어 리셋 뒤에도 되돌아온다.
        </p>
        {profileReport && profileReport.cameras.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {profileReport.cameras.map((r) => <ApplyBadge key={r.cam_id} r={r} />)}
          </div>
        )}
      </div>

      {/* ⚠ 사라진 등록 카메라 — **맨 위**에 둔다. 이걸 모르고 녹화를 시작하면
          시작하자마자 실패하는데, 화면 아래쪽에 있으면 아무도 안 본다. */}
      {absent.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-red-400">연결 안 됨 ({absent.length})</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {absent.map((cam) => (
              <div key={cam.id} className="rounded-lg border border-red-500/30 bg-red-500/5 p-3 space-y-1">
                <div className="flex items-center gap-2 text-sm">
                  <span className="text-red-400">✕</span>
                  <span className="font-medium">{cam.display_name || cam.name}</span>
                </div>
                <p className="text-[11px] text-neutral-400">{cam.id}</p>
                <p className="text-[11px] text-red-300">
                  스캔에서 안 보입니다 — USB 를 확인하세요. 등록은 남아 있으므로
                  다시 꽂고 스캔하면 별칭·설정 그대로 돌아옵니다.
                </p>
                <button
                  onClick={() => handleUnregister(cam.id)}
                  className="text-[11px] text-neutral-400 hover:text-white underline">
                  더 안 쓸 거면 등록 해제
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 등록됨 */}
      {ready.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-green-400">사용 가능 ({ready.length})</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {ready.map((cam) => {
              const isLive = liveIds.has(cam.id)
              return (
                <div key={cam.id} className="rounded-lg border border-green-500/30 bg-green-500/5 overflow-hidden hover:border-green-500/60 transition-colors">
                  {/* ⚠ **없는 카메라는 그리지 않는다.** 세그먼트에 마지막 프레임이
                      남아 있으면 뽑힌 카메라가 **정상처럼 보인다** — 실제로 그랬다.
                      영상이 있다는 것과 장치가 있다는 것은 다른 사실이다. */}
                  {/* ⚠ 장치가 없거나 **영상이 끊긴** 카메라는 그리지 않는다.
                      세그먼트에 마지막 프레임이 남아 있어서, 안 가리면 뽑힌 카메라가
                      정상처럼 보인다 — 실제로 그렇게 보였다. */}
                  {cam.present === false || (cam.connected && cam.streaming === false) ? (
                    <div className="w-full aspect-[4/3] bg-neutral-900 flex flex-col items-center justify-center gap-1 text-center px-3">
                      <span className="text-2xl text-red-400">✕</span>
                      <span className="text-xs text-red-300">
                        {cam.present === false ? '연결 안 됨' : '영상 끊김'}
                      </span>
                      <span className="text-[10px] text-neutral-500">
                        {cam.present === false
                          ? 'USB 를 확인하고 스캔하세요'
                          : '프레임이 오지 않습니다 — 상단 메시지를 확인하세요'}
                      </span>
                    </div>
                  ) : (
                  <img
                    src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${previewTs[cam.id] ?? 0}`}
                    alt={cam.name}
                    className="w-full aspect-[4/3] object-cover bg-neutral-900"
                    onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                    onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
                  />
                  )}
                  <div className="p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="text-green-400 text-sm">✓</span>
                      {/* 별칭이 주인공 — 카메라를 옮겨 달면 여기서 바로 고친다.
                          blur 에 저장해 타이핑 중 요청이 쏟아지지 않게 한다. */}
                      <input
                        type="text"
                        defaultValue={cam.label ?? ''}
                        key={cam.label ?? ''}
                        onBlur={(e) => {
                          const v = e.target.value.trim()
                          if (v !== (cam.label ?? '')) handleLabel(cam.id, v)
                        }}
                        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                        placeholder="이름 없음"
                        title="표시 이름 — 데이터셋 피처 이름은 바뀌지 않습니다"
                        className="flex-1 min-w-0 px-1.5 py-0.5 text-sm rounded bg-transparent border border-transparent hover:border-neutral-700 focus:bg-neutral-900 focus:border-blue-500 text-neutral-100 placeholder:text-neutral-600 focus:outline-none" />
                      {cam.usb_port && <span className="font-mono text-[10px] text-neutral-500" title="USB 포트">{cam.usb_port}</span>}
                      {cam.usb_warning && <UsbWarning reason={cam.usb_warning} />}
                    </div>
                    <div className="text-xs text-neutral-400">
                      <span className="font-mono text-[10px] text-neutral-500">{cam.id}</span>
                      <span className="ml-2">{cam.name}</span>
                      {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                      {cam.config.fps && <span className="ml-2">{cam.config.fps}fps</span>}
                    </div>
                    <div className="flex gap-1.5">
                      <button onClick={() => handleProbe(cam.id)} disabled={busyId === cam.id}
                        className="flex-1 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                        {busyId === cam.id ? <><Spinner className="inline" /> 처리 중</> : '업데이트'}
                      </button>
                      <button onClick={async () => {
                        if (isLive) {
                          setLiveIds((prev) => { const next = new Set(prev); next.delete(cam.id); return next })
                          await api.post('/cameras/disconnect', { id: cam.id })
                          setCams((prev) => prev.map((c) => c.id === cam.id ? { ...c, connected: false } : c))
                        } else {
                          // 연결 후 실시간 보기 시작
                          try {
                            const updated = await api.post<CamInfo>('/cameras/connect', { id: cam.id })
                            setCams((prev) => prev.map((c) => c.id === cam.id ? updated : c))
                            setLiveIds((prev) => new Set(prev).add(cam.id))
                            bumpPreview([cam.id])
                          } catch { notifyError('카메라 연결 실패') }
                        }
                      }}
                        className={`flex-1 py-1 text-xs rounded transition-colors ${isLive ? 'bg-red-600 hover:bg-red-500 text-white' : 'bg-neutral-700 hover:bg-green-600 text-neutral-300 hover:text-white'}`}>
                        {isLive ? '중단' : '실시간 보기'}
                      </button>
                      <button onClick={() => openSettings(cam.id)}
                        className="flex-1 py-1 text-xs rounded bg-neutral-700 hover:bg-yellow-600 text-neutral-300 hover:text-white transition-colors">
                        설정
                      </button>
                      <button onClick={() => handleUnregister(cam.id)}
                        className="py-1 px-2 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white transition-colors">
                        해제
                      </button>
                      <ResetDeviceButton cam={cam} />
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 미연결 카메라 — 스캔으로 찾기만 했고 아직 열지 않았다 */}
      {unconnected.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-neutral-400">
            미등록 ({unconnected.length})
            <span className="ml-2 font-normal text-neutral-500">
              — 찾기만 한 상태. 등록해야 녹화·추론에서 씁니다
            </span>
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {unconnected.map((cam) => (
              <div key={cam.id}
                className="rounded-lg border border-neutral-700 bg-neutral-800 overflow-hidden hover:border-blue-500 hover:bg-blue-500/5 transition-colors group"
              >
                {cam.has_preview ? (
                  <img src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${previewTs[cam.id] ?? 0}`} alt={cam.name}
                    className="w-full aspect-[4/3] object-cover bg-neutral-900" />
                ) : (
                  <div className="w-full aspect-[4/3] bg-neutral-900 flex items-center justify-center text-neutral-600 text-xs">
                    프리뷰 없음
                  </div>
                )}
                <div className="p-3 space-y-2">
                  <div className="flex items-center gap-2">
                    <span className="inline-block w-2 h-2 rounded-full bg-neutral-500" />
                    <span className="font-mono text-sm">{cam.id}</span>
                      {cam.usb_port && <span className="ml-auto font-mono text-[10px] text-neutral-500" title="USB 포트">{cam.usb_port}</span>}
                      {cam.usb_warning && <UsbWarning reason={cam.usb_warning} />}
                  </div>
                  <p className="text-xs text-neutral-400">{cam.name}
                    {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                  </p>
                  {/* 별칭 — 등록할 때 함께 보낸다. 나중에 카드에서 고칠 수도 있다. */}
                  <input
                    type="text"
                    value={labelDraft[cam.id] ?? ''}
                    onChange={(e) => setLabelDraft((prev) => ({ ...prev, [cam.id]: e.target.value }))}
                    onKeyDown={(e) => { if (e.key === 'Enter') handleRegister(cam.id) }}
                    placeholder="이름 (예: 탑뷰, 손목)"
                    className="w-full px-2 py-1 text-xs rounded bg-neutral-900 border border-neutral-700 text-neutral-100 placeholder:text-neutral-600 focus:outline-none focus:border-blue-500" />
                  <div className="flex gap-1.5">
                    <button onClick={() => handleRegister(cam.id)} disabled={connectingId === cam.id}
                      className="flex-1 py-1.5 text-xs rounded bg-neutral-700 group-hover:bg-green-600 text-neutral-300 group-hover:text-white transition-colors disabled:opacity-50 flex items-center justify-center gap-1">
                      {connectingId === cam.id ? <><Spinner /> 등록 중...</> : '등록'}
                    </button>
                    <ResetDeviceButton cam={cam} />
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 연결됨 (미등록) */}
      {connected.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-neutral-400">연결됨 (미등록)</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {connected.map((cam) => {
              return (
                <div key={cam.id} className="rounded-lg border border-blue-500/30 bg-blue-500/5 overflow-hidden hover:border-blue-500/60 transition-colors">
                  {/* 프리뷰 */}
                  {/* ⚠ **없는 카메라는 그리지 않는다.** 세그먼트에 마지막 프레임이
                      남아 있으면 뽑힌 카메라가 **정상처럼 보인다** — 실제로 그랬다.
                      영상이 있다는 것과 장치가 있다는 것은 다른 사실이다. */}
                  {/* ⚠ 장치가 없거나 **영상이 끊긴** 카메라는 그리지 않는다.
                      세그먼트에 마지막 프레임이 남아 있어서, 안 가리면 뽑힌 카메라가
                      정상처럼 보인다 — 실제로 그렇게 보였다. */}
                  {cam.present === false || (cam.connected && cam.streaming === false) ? (
                    <div className="w-full aspect-[4/3] bg-neutral-900 flex flex-col items-center justify-center gap-1 text-center px-3">
                      <span className="text-2xl text-red-400">✕</span>
                      <span className="text-xs text-red-300">
                        {cam.present === false ? '연결 안 됨' : '영상 끊김'}
                      </span>
                      <span className="text-[10px] text-neutral-500">
                        {cam.present === false
                          ? 'USB 를 확인하고 스캔하세요'
                          : '프레임이 오지 않습니다 — 상단 메시지를 확인하세요'}
                      </span>
                    </div>
                  ) : (
                  <img
                    src={`/api/cameras/${encodeURIComponent(cam.id)}/preview?t=${previewTs[cam.id] ?? 0}`}
                    alt={cam.name}
                    className="w-full aspect-[4/3] object-cover bg-neutral-900"
                    onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                    onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
                  />
                  )}
                  <div className="p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
                      <span className="font-mono text-sm">{cam.id}</span>
                      {cam.usb_port && <span className="ml-auto font-mono text-[10px] text-neutral-500" title="USB 포트">{cam.usb_port}</span>}
                      {cam.usb_warning && <UsbWarning reason={cam.usb_warning} />}
                    </div>
                    <div className="text-xs text-neutral-400">
                      {cam.name}
                      {cam.config.width && <span className="ml-2">{cam.config.width}x{cam.config.height}</span>}
                      {cam.config.fps && <span className="ml-2">{cam.config.fps}fps</span>}
                    </div>
                    <div className="flex gap-1.5">
                      <button onClick={() => openSettings(cam.id)}
                        className="flex-1 py-1 text-xs rounded bg-neutral-700 hover:bg-yellow-600 text-neutral-300 hover:text-white transition-colors">설정</button>
                      <button onClick={() => handleRegister(cam.id)}
                        className="flex-1 py-1 text-xs rounded bg-green-600 hover:bg-green-500 text-white">등록</button>
                      <button onClick={() => handleDisconnect(cam.id)}
                        className="py-1 px-2 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">해제</button>
                      <ResetDeviceButton cam={cam} />
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {cams.length === 0 && (
        <p className="text-center text-neutral-500 text-sm py-8">
          "스캔"을 눌러 카메라를 검색하세요
        </p>
      )}

      {/* 설정 모달 — 등록·미등록 카드가 같은 창을 연다.
          카드 안에서 펼치면 그리드 행 높이가 늘어나 옆 카드까지 커졌다. */}
      {settingsCamera && (
        <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center" onClick={closeSettings}>
          <div className="bg-neutral-800 rounded-xl border border-neutral-600 p-6 w-[960px] max-w-[95vw] max-h-[90vh] flex flex-col gap-4"
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-lg font-bold truncate">
                  {settingsCamera.label || settingsCamera.name}
                </h2>
                <p className="font-mono text-[11px] text-neutral-500 truncate">{settingsCamera.id}</p>
              </div>
              <button onClick={closeSettings}
                className="px-2 py-1 text-sm rounded text-neutral-400 hover:bg-neutral-700 hover:text-white">✕</button>
            </div>

            {/* 좌: 프리뷰 / 우: 설정. 세로로 쌓으면 슬라이더를 만질 때 화면이
                스크롤 밖으로 밀려 결과를 못 본다 — 붙여 놓는 것이 요점이다.
                좁은 화면(<lg)에서는 자동으로 위아래로 접힌다. */}
            <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px] gap-4 min-h-0 flex-1">
            {/* 프리뷰 — 오른쪽 설정을 만지면 여기서 바로 결과가 보인다 */}
            <div className="relative self-start">
              <img
                src={`/api/cameras/${encodeURIComponent(settingsCamera.id)}/preview?t=${previewTs[settingsCamera.id] ?? 0}`}
                alt={settingsCamera.display_name ?? settingsCamera.name}
                className="w-full max-h-[62vh] aspect-[4/3] object-contain rounded bg-neutral-900"
                onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                onLoad={(e) => { (e.target as HTMLImageElement).style.opacity = '1' }}
              />
              {!settingsCamera.connected && (
                // 연결 안 된 카메라는 프레임이 안 들어온다. 여기서 몰래 열지 않는다 —
                // 장치를 쥐는 것은 사용자가 결정할 일이다.
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/60 rounded">
                  <p className="text-xs text-neutral-300">연결되지 않아 화면이 갱신되지 않습니다</p>
                  <button onClick={async () => {
                    try {
                      const updated = await api.post<CamInfo>('/cameras/connect', { id: settingsCamera.id })
                      setCams((prev) => prev.map((c) => (c.id === updated.id ? updated : c)))
                      bumpPreview([settingsCamera.id])
                    } catch { /* 연결 실패는 카드 쪽 흐름에서 다룬다 */ }
                  }}
                    className="px-3 py-1 text-xs rounded bg-green-600 hover:bg-green-500 text-white">연결</button>
                </div>
              )}
            </div>

            {/* 오른쪽 열 — 항목이 길어지면 여기만 스크롤한다 */}
            <div className="space-y-4 min-h-0 overflow-y-auto pr-1">
            {/* 이름 — 등록 여부와 무관하게 여기서 고칠 수 있다 */}
            <div className="space-y-1">
              <span className="text-xs text-neutral-400">이름</span>
              <input type="text" defaultValue={settingsCamera.label ?? ''} key={`label-${settingsCamera.id}`}
                onBlur={(e) => {
                  const v = e.target.value.trim()
                  if (v !== (settingsCamera.label ?? '')) handleLabel(settingsCamera.id, v)
                }}
                placeholder="예: 탑뷰, 손목"
                className="w-full px-2 py-1.5 text-sm rounded bg-neutral-900 border border-neutral-700 text-neutral-100 placeholder:text-neutral-600 focus:outline-none focus:border-blue-500" />
              <p className="text-[10px] text-neutral-500">
                화면에서 알아보기 위한 이름입니다. 데이터셋 피처 이름은 바뀌지 않습니다.
              </p>
            </div>

            {/* 깊이 인코딩 — depth 스트림에만 뜬다.
                작업 공간에 맞춰 좁힐수록 해상도가 오른다. 기본 150~1200mm 는
                넓게 잡은 값이라, 1m 안쪽 작업이면 절반을 버리는 셈이다. */}
            {settingsCamera.stream_type === 'depth' && (
              <div className="space-y-1.5">
                <label className="text-xs text-neutral-400">깊이 범위</label>
                {/* ⚠ **끄는 동안에는 안 보낸다** (`onCommit`). 한 번이 장치 RPC 라
                    틱마다 보내면 그 자체가 부하고, 배타 가드(`require_idle`)도
                    매번 탄다. 놓는 순간 한 번만 반영된다.
                    ⚠ 두 손잡이는 서로의 한계다 — `far > near` 는 백엔드 규칙이라
                    끌어서 넘길 수 있게 두면 놓는 순간 거부당한다. */}
                <ParamSlider label="가까운 쪽" unit="mm"
                  value={depthDraft.near_mm}
                  min={0}
                  max={Math.max(0, depthDraft.far_mm - DEPTH_STEP)}
                  step={DEPTH_STEP}
                  onChange={(v) => setDepthDraft((d) => ({ ...d, near_mm: v }))}
                  onCommit={(v) => setDepthRange(settingsCamera.id, v, null)} />
                <ParamSlider label="먼 쪽" unit="mm"
                  value={depthDraft.far_mm}
                  min={depthDraft.near_mm + DEPTH_STEP}
                  max={Math.max(DEPTH_SLIDER_MAX, depthDraft.far_mm)}
                  step={DEPTH_STEP}
                  onChange={(v) => setDepthDraft((d) => ({ ...d, far_mm: v }))}
                  onCommit={(v) => setDepthRange(settingsCamera.id, null, v)} />
                {/* 장치 스케일을 **보여준다.** 이게 안 보여서 D405 가 D435 처럼
                    계산되는 걸 오래 못 알아챘다 — 아래 mm 는 이 값으로 환산된 것이다. */}
                {settingsCamera.depth_units_m && settingsCamera.depth_units_m !== 0.001 && (
                  <p className="text-[10px] text-neutral-400">
                    이 장치는 raw 1 단위 = {(settingsCamera.depth_units_m * 1000).toFixed(2)}mm
                    입니다 (보통 1.00mm). 위 값은 <b>실제 거리</b>이고, 환산은 rsd 가 합니다.
                  </p>
                )}
                <p className="text-[10px] text-neutral-500">
                  폭 {Math.max(0, depthDraft.far_mm - depthDraft.near_mm)}mm →
                  {' '}1단계 ≈ {((depthDraft.far_mm - depthDraft.near_mm) / 254).toFixed(1)}mm
                  {depthEnc && (depthDraft.near_mm !== depthEnc.near_mm
                    || depthDraft.far_mm !== depthEnc.far_mm) && (
                    <span className="ml-1 text-amber-400">· 놓으면 반영됩니다</span>
                  )}
                </p>
                <p className="text-[10px] text-neutral-500">
                  이 구간이 0~254 로 매핑됩니다(가까울수록 어둡게). 범위 밖은 잘리고,
                  카메라가 못 읽은 픽셀은 255(가장 멂)입니다.
                  <b className="text-amber-500"> 녹화한 데이터의 해석이 이 값에 달려 있습니다</b> —
                  바꾸면 이전 데이터와 픽셀값의 뜻이 달라집니다.
                </p>
              </div>
            )}

            {/* 해상도·FPS·회전 */}
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div className="flex items-center gap-2">
                <span className="text-neutral-400 w-14">Width</span>
                <input type="number" value={settingsCamera.config.width ?? ''} placeholder="auto"
                  onChange={(e) => handleConfig(settingsCamera.id, { width: e.target.value ? Number(e.target.value) : null })}
                  className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-neutral-400 w-14">Height</span>
                <input type="number" value={settingsCamera.config.height ?? ''} placeholder="auto"
                  onChange={(e) => handleConfig(settingsCamera.id, { height: e.target.value ? Number(e.target.value) : null })}
                  className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-neutral-400 w-14">FPS</span>
                <input type="number" value={settingsCamera.config.fps ?? ''} placeholder="auto"
                  onChange={(e) => handleConfig(settingsCamera.id, { fps: e.target.value ? Number(e.target.value) : null })}
                  className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 focus:outline-none focus:border-blue-500" />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-neutral-400 w-14">Rot</span>
                <select value={settingsCamera.config.rotation}
                  onChange={(e) => handleConfig(settingsCamera.id, { rotation: Number(e.target.value) })}
                  className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100">
                  <option value={0}>0°</option><option value={90}>90°</option>
                  <option value={180}>180°</option><option value={270}>270°</option>
                </select>
              </div>
            </div>

            {/* 이미지 조정 (v4l2 컨트롤) */}
            <div className="border-t border-neutral-700 pt-3 space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-xs text-neutral-400">이미지 조정</span>
                {controls.length > 0 && (
                  <button onClick={() => handleResetControls(settingsCamera.id)} disabled={busyId === settingsCamera.id}
                    className="px-2 py-0.5 text-[10px] rounded bg-neutral-700 hover:bg-orange-600 text-neutral-400 hover:text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                    {busyId === settingsCamera.id ? '처리 중…' : '초기화'}
                  </button>
                )}
              </div>
              {controls.length === 0 ? (
                <p className="text-xs text-neutral-500">지원되는 컨트롤이 없습니다</p>
              ) : controls.map((ctrl) => {
                const locked = ctrl.inactive || ctrl.readonly
                return (
                  <div key={ctrl.name} className={`flex items-center gap-2 text-xs ${locked ? 'opacity-40' : ''}`}>
                    <span className="text-neutral-400 w-28 shrink-0 truncate" title={ctrl.label + (locked ? ' (자동 모드에 의해 잠김)' : '')}>{ctrl.label}</span>
                    {ctrl.type === 2 ? (
                      <input type="checkbox" checked={ctrl.value !== 0} disabled={locked}
                        onChange={(e) => handleControl(settingsCamera.id, ctrl.name, e.target.checked ? 1 : 0)}
                        className="accent-blue-500" />
                    ) : ctrl.type === 3 ? (
                      <select value={ctrl.value} disabled={locked}
                        onChange={(e) => handleControl(settingsCamera.id, ctrl.name, Number(e.target.value))}
                        className="flex-1 px-1 py-0.5 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 text-xs disabled:opacity-50">
                        {Array.from({ length: ctrl.max - ctrl.min + 1 }, (_, i) => ctrl.min + i).map((v) => (
                          <option key={v} value={v}>{v}</option>
                        ))}
                      </select>
                    ) : (
                      <input type="range" min={ctrl.min} max={ctrl.max} step={ctrl.step || 1}
                        value={ctrl.value} disabled={locked}
                        onChange={(e) => handleControl(settingsCamera.id, ctrl.name, Number(e.target.value))}
                        className="flex-1 h-1 accent-blue-500 disabled:opacity-50" />
                    )}
                    <span className="text-neutral-300 w-12 text-right font-mono">{ctrl.value}</span>
                  </div>
                )
              })}
            </div>
            </div>{/* /오른쪽 열 */}
            </div>{/* /좌우 그리드 */}
          </div>
        </div>
      )}
    </div>
  )
}
