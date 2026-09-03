import { useCallback, useEffect, useRef, useState } from 'react'
import ExposureReadout, { METERING_LABEL, METERING_MODES, type LightSample, type Metering } from '../components/ExposureReadout'
import CameraProfilesPanel from '../components/CameraProfilesPanel'
import { useSystemMessage } from '../components/SystemMessages'
import ParamSlider from '../components/ParamSlider'
import RoiPicker, { centerRoi, toBox } from '../components/RoiPicker'
import type { Roi } from '../components/RoiPicker'
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
  /** 컬러 스트림에만 붙는다 — 배경이 지워진 채로 나오는가. */
  background_mask?: {
    enabled: boolean; far_mm: number
    /** 따로 정한 값이 아니라 깊이 창을 따라가는 중인가. */
    follows_depth: boolean; keep_unknown: boolean
  } | null
  config: { width: number | null; height: number | null; fps: number | null; color_mode: string; rotation: number; fourcc: string | null }
}

type CamControl = {
  cid: number; name: string; label: string; type: number
  min: number; max: number; step: number; default: number; value: number
  inactive?: boolean; readonly?: boolean
  /** 표시 단위 — 백엔드가 아는 컨트롤에만 붙는다 (piper_cam.controls.CONTROL_UNITS). */
  unit?: string
}

/** 노출 단위의 사람 번역 — "333 이 뭔데" 를 ms 로 말해준다. */
function unitHint(ctrl: CamControl): string | undefined {
  if (ctrl.unit === '×100µs') return `${ctrl.value} × 100µs = ${(ctrl.value / 10).toFixed(1)}ms`
  if (ctrl.unit === 'µs') return `${(ctrl.value / 1000).toFixed(1)}ms`
  return undefined
}

type GrayCardReading = {
  luma: number; neutral_error_pct: number; clipped_pct: number
  spread_pct: number; usable: boolean; why: string
}
type GrayCardReport = {
  ok: boolean; verdict: string; target: number
  before: GrayCardReading; after: GrayCardReading
  exposure_us: number
  /** 최종 gain·WB 와 움직인 손잡이 — 옛 rsd 응답에는 없다. */
  gain?: number; white_balance?: number; adjust?: string
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
  // 배경 마스킹은 **컬러 스트림**의 성질이지만 경계(`far_mm`)를 깊이 창과 공유한다.
  const [maskOn, setMaskOn] = useState(false)
  // 마스킹 경계와 무효 픽셀 처리. 경계는 깊이 창과 **따로** 둘 수 있다 —
  // 인코딩 창은 해상도를 위해 좁히고 마스킹은 더 멀리 남기고 싶은 경우가 있다.
  const [maskFar, setMaskFar] = useState<number>(DEPTH_DEFAULT.far_mm)
  const [maskKeepUnknown, setMaskKeepUnknown] = useState(true)
  // 회색 카드 보정 결과. 값을 저장하진 않으므로 **결과를 보여주는 것이 전부**다 —
  // 마음에 들면 사용자가 프로파일로 저장한다.
  const [grayCard, setGrayCard] = useState<GrayCardReport | null>(null)
  const [calibrating, setCalibrating] = useState(false)
  // 노출 고정 모드 — 노출은 모션 블러·프레임 예산을 정하므로 먼저 정해 두고
  // 밝기는 gain 으로만 잡고 싶을 때가 있다. 선택은 브라우저에 남긴다.
  const [gainOnly, setGainOnly] = useState(
    () => localStorage.getItem('piper.graycard.gainOnly') === '1')
  const toggleGainOnly = () => setGainOnly((v) => {
    localStorage.setItem('piper.graycard.gainOnly', v ? '0' : '1')
    return !v
  })
  // 카드 영역. **프레임 좌표**다 — 화면 크기는 창을 줄이면 바뀐다.
  // 조준 중인가. **평소에는 상자를 안 그린다** — 프리뷰는 대부분의 시간
  // 카메라를 확인하는 화면이고, 늘 떠 있는 상자는 그때 방해만 된다.
  const [aiming, setAiming] = useState(false)
  const [roi, setRoi] = useState<Roi | null>(null)
  const [roiReading, setRoiReading] = useState<GrayCardReading | null>(null)
  const previewRef = useRef<HTMLImageElement | null>(null)
  const measureTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 활성 프로파일 이름 — 프로파일 탭이 배지·활성 지정에 쓰고, 회색 카드 보정
  // 안내가 "어디에 덮어쓰면 되는지" 이름을 대는 데 쓴다.
  const [activeProfile, setActiveProfile] = useState('')
  // 장치와 프로파일은 하는 일이 다르다 — 장치 탭은 "무엇이 붙어 있나",
  // 프로파일 탭은 "어떤 값을 이름 붙여 두었나". 한 화면에 섞으니 프로파일
  // 상세(값 편집)를 둘 자리가 없었다.
  const [tab, setTab] = useState<'devices' | 'profiles'>('devices')

  useEffect(() => {
    api.get<{ active: string }>('/cameras/profiles/active')
      .then((r) => setActiveProfile(r.active || ''))
      .catch(() => {})
  }, [])

  /** 지금 상자를 재기만 한다. 장치를 안 건드리므로 옮길 때마다 불러도 된다 —
   *  그림자·반사는 눈으로 잘 안 보이고 얼룩 % 로만 드러난다. */
  const measureRoi = useCallback((colorId: string, box: Roi | null) => {
    // ⚠ **멈춘 뒤에 잰다.** 휠 한 번에 이벤트가 수십 개 오는데 그때마다 요청을
    //   보내면 조준하는 동안 요청이 밀려 숫자가 뒤늦게, 뒤섞여 들어온다.
    if (measureTimer.current) clearTimeout(measureTimer.current)
    measureTimer.current = setTimeout(async () => {
      const img = previewRef.current
      if (!img?.naturalWidth || !box) return
      try {
        const r = await api.post<{ reading: GrayCardReading }>(
          `/cameras/${encodeURIComponent(colorId)}/measure-gray-card`,
          { roi: toBox(box, img.naturalWidth, img.naturalHeight) })
        setRoiReading(r.reading ?? null)
      } catch { /* 조준 도우미다 — 실패해도 화면을 어지럽히지 않는다 */ }
    }, 200)
  }, [])

  /** 상자가 움직였다. 열려 있는 카메라가 바뀔 때만 새로 만든다 — 매 렌더 새
   *  함수를 넘기면 자식이 그걸 의존성으로 쓰는 순간 리스너가 떼였다 붙는다. */
  const onRoiChange = useCallback((next: Roi) => {
    setRoi(next)
    if (settingsCam) measureRoi(settingsCam, next)
  }, [settingsCam, measureRoi])

  /** 조준을 시작한다. 상자를 처음 띄우고 곧바로 한 번 재서 숫자를 채운다 —
   *  빈 상자만 뜨면 어디로 옮겨야 좋은지 알 수 없다. */
  const startAiming = (colorId: string) => {
    const img = previewRef.current
    const box = roi ?? (img?.naturalWidth
      ? centerRoi(img.naturalWidth, img.naturalHeight) : null)
    setGrayCard(null)
    setRoi(box)
    setAiming(true)
    if (box) measureRoi(colorId, box)
  }

  /** 회색 카드로 화이트밸런스·노출을 맞춘다. 값은 장치에만 올라간다 —
   *  마음에 들면 사용자가 프로파일로 저장한다(그쪽이 연결 시 적용까지 한다). */
  const calibrateGrayCard = async (colorId: string) => {
    setCalibrating(true); setGrayCard(null)
    try {
      const img = previewRef.current
      const box = (roi && img?.naturalWidth)
        ? toBox(roi, img.naturalWidth, img.naturalHeight) : null
      const r = await api.post<GrayCardReport>(
        `/cameras/${encodeURIComponent(colorId)}/calibrate-gray-card`,
        { ...(box ? { roi: box } : {}), adjust: gainOnly ? 'gain' : 'exposure' })
      setGrayCard(r)
      setAiming(false)          // 결과를 볼 차례다 — 상자는 치운다
      bumpPreview([colorId])
      // 보정은 노출·gain·WB·자동 스위치를 전부 움직인다 — 아래 "이미지 조정"
      // 목록은 모달을 열 때 한 번만 읽으므로, 여기서 안 갱신하면 옛 값이 남아
      // "보정이 안 먹었나"로 읽힌다 (자동 스위치의 잠김 표시도 바뀌어야 한다).
      try {
        setControls(await api.get<CamControl[]>(
          `/cameras/${encodeURIComponent(colorId)}/controls`))
      } catch { /* 표시 갱신일 뿐이다 — 보정 결과는 이미 위 상자에 있다 */ }
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '보정에 실패했습니다')
    } finally {
      setCalibrating(false)
    }
  }

  /** 같은 장치의 **컬러** 스트림에서 배경을 지울지. 경계는 깊이 창의 `far_mm` 이다. */
  const setBackgroundMask = async (
    depthId: string,
    patch: { enabled?: boolean; far_mm?: number; keep_unknown?: boolean },
  ) => {
    const colorId = depthId.replace(/:depth$/, ':color')
    const enabled = patch.enabled ?? maskOn
    const prev = { enabled: maskOn, far: maskFar, keep: maskKeepUnknown }
    // 먼저 반영해 토글·슬라이더가 굳어 보이지 않게
    if (patch.enabled !== undefined) setMaskOn(patch.enabled)
    if (patch.far_mm !== undefined) setMaskFar(patch.far_mm)
    if (patch.keep_unknown !== undefined) setMaskKeepUnknown(patch.keep_unknown)
    try {
      await api.post(`/cameras/${encodeURIComponent(colorId)}/background-mask`, {
        enabled,
        far_mm: patch.far_mm ?? maskFar,
        keep_unknown: patch.keep_unknown ?? maskKeepUnknown,
      })
      bumpPreview([colorId, depthId])
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '배경 마스킹을 바꾸지 못했습니다')
      // 거부됐으면 화면이 바뀐 척하면 안 된다 — 되돌린다
      setMaskOn(prev.enabled); setMaskFar(prev.far); setMaskKeepUnknown(prev.keep)
    }
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

  /**
   * 프리뷰 `src`. **실시간 보기 중이면 스트림, 아니면 한 장.**
   *
   * ⚠ 전부 스트림으로 돌리지 않는다 — 카드마다 연결을 하나씩 물면 목록만 열어도
   *   연결이 카메라 수만큼 열린다. 한 장짜리는 "설정을 바꿨으니 다시 보여줘"
   *   같은 자리에 그대로 필요하다(`bumpPreview`).
   */
  const previewSrc = (id: string) =>
    liveIds.has(id)
      ? `/api/cameras/${encodeURIComponent(id)}/stream`
      : `/api/cameras/${encodeURIComponent(id)}/preview?t=${previewTs[id] ?? 0}`

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
  // 노출 표시용. 샘플러가 2초마다 재 둔 것을 같은 주기로 받아온다 —
  // 카드마다 폴링하면 카메라 수만큼 요청이 늘고, 장치는 아무도 안 만진다.
  const [light, setLight] = useState<Record<string, LightSample>>({})
  // 측광 모드는 **보는 방식**이지 장치 설정이 아니다 — 브라우저에 남긴다.
  // 모드별 값은 샘플에 다 실려 오므로 바꾸는 즉시 반영된다(다음 샘플을 안 기다린다).
  const [metering, setMetering] = useState<Metering>(() => {
    const v = localStorage.getItem('piper_metering')
    return (METERING_MODES as readonly string[]).includes(v || '') ? (v as Metering) : 'average'
  })
  useEffect(() => { localStorage.setItem('piper_metering', metering) }, [metering])
  useEffect(() => {
    const tick = () => api.get<{ cameras: LightSample[] }>('/cameras/light')
      .then((r) => setLight(Object.fromEntries(r.cameras.map((c) => [c.id, c]))))
      .catch(() => {})
    tick()
    const id = setInterval(tick, 2000)
    return () => clearInterval(id)
  }, [])

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
    // ⚠ 실시간 보기에는 타이머가 없다. 스트림이 밀어주므로 필요 없고, 주기적으로
    //   `src` 를 건드리면 그때마다 연결이 끊겼다 다시 붙어 **오히려 끊긴다.**
    //   발행자가 사라졌다 돌아오는 경우는 서버 쪽 `segment_reader` 가 다시 붙는다.
    bumpPreview([...liveIds])
    return () => {}
  }, [liveIds])

  // 모달은 Esc 로도 닫힌다
  useEffect(() => {
    if (!settingsCam) { setDepthEnc(null); setDepthDraft(DEPTH_DEFAULT); return }
    // 현재 값을 먼저 채운다 — 기본값을 보여주면 사용자가 "그 값이다"라고 믿는다
    const c = cams.find((x) => x.id === settingsCam)
    if (c?.depth_encoding) { setDepthEnc(c.depth_encoding); setDepthDraft(c.depth_encoding) }
    else setDepthDraft(DEPTH_DEFAULT)
    const color = cams.find((x) => x.id === settingsCam.replace(/:depth$/, ':color'))
    const bg = color?.background_mask
    setMaskOn(bg?.enabled ?? false)
    setMaskFar(bg?.far_mm ?? c?.depth_encoding?.far_mm ?? DEPTH_DEFAULT.far_mm)
    setMaskKeepUnknown(bg?.keep_unknown ?? true)
    setAiming(false); setRoi(null); setRoiReading(null); setGrayCard(null)
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
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">카메라</h1>
        <div className="flex rounded-lg border border-neutral-700 overflow-hidden text-sm">
          {([['devices', '장치'], ['profiles', '프로파일']] as const).map(([k, label]) => (
            <button key={k} onClick={() => setTab(k)}
              className={`px-4 py-1.5 ${tab === k
                ? 'bg-neutral-700 text-white' : 'bg-neutral-900 text-neutral-400 hover:text-white'}`}>
              {label}
            </button>
          ))}
        </div>
        {tab === 'devices' && (<>
          {/* 측광 모드 — 카드마다 두면 카메라 수만큼 중복된다. 한 곳에서 고르고
              모든 카드가 같은 방식으로 읽힌다. */}
          <div className="ml-auto flex items-center gap-1 rounded-lg bg-neutral-800 p-0.5"
               title="같은 프레임의 어디를 보고 노출을 잴지. 목표(0.0 EV)는 셋 다 같습니다.">
            <span className="pl-1.5 text-[10px] text-neutral-500">측광</span>
            {METERING_MODES.map((m) => (
              <button key={m} onClick={() => setMetering(m)}
                className={`rounded px-2 py-1 text-xs transition-colors ${metering === m
                  ? 'bg-neutral-600 text-white' : 'text-neutral-400 hover:text-neutral-200'}`}>
                {METERING_LABEL[m]}
              </button>
            ))}
          </div>
          <button onClick={handleScan} disabled={scanning}
            className="px-4 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
            {scanning ? <><Spinner className="inline" /> 스캔 중...</> : '스캔'}
          </button>
        </>)}
      </div>

      {tab === 'profiles' && (
        <CameraProfilesPanel active={activeProfile} onActiveChange={setActiveProfile} />
      )}

      {tab === 'devices' && (<>

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
                    src={previewSrc(cam.id)}
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
                    <ExposureReadout light={light[cam.id]} mode={metering} />
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
                  <img src={previewSrc(cam.id)} alt={cam.name}
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
                    src={previewSrc(cam.id)}
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
                    <ExposureReadout light={light[cam.id]} mode={metering} />
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
      </>)}

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
                ref={previewRef}
                src={previewSrc(settingsCamera.id)}
                alt={settingsCamera.display_name ?? settingsCamera.name}
                className="w-full max-h-[62vh] aspect-[4/3] object-contain rounded bg-neutral-900"
                onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                onLoad={(e) => {
                  const img = e.target as HTMLImageElement
                  img.style.opacity = '1'
                  // 상자는 **조준을 시작할 때** 만든다. 프레임 크기는 여기서 알게
                  // 되지만, 미리 만들어두면 안 쓰는 상태가 하나 생긴다.
                  if (aiming && !roi && img.naturalWidth) {
                    setRoi(centerRoi(img.naturalWidth, img.naturalHeight))
                  }
                }}
              />
              {/* 카드 영역 고르기 — 컬러 스트림에서만 의미가 있다 */}
              {aiming && settingsCamera.stream_type !== 'depth' && (
                <RoiPicker
                  imgRef={previewRef} roi={roi}
                  hint={roiReading ? `밝기 ${roiReading.luma} · 얼룩 ${roiReading.spread_pct}%` : undefined}
                  onChange={onRoiChange}
                />
              )}
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

            {/* 회색 카드 보정 — 컬러 스트림에만 뜬다.
                기하 보정이 아니다. 색·밝기를 재현 가능하게 만드는 것이 전부다. */}
            {settingsCamera.stream_type !== 'depth' && (
              <div className="space-y-1.5 rounded border border-neutral-700 p-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-neutral-400">회색 카드 보정</span>
                  {aiming ? (
                    <span className="flex gap-1">
                      <button
                        onClick={() => { setAiming(false); setRoiReading(null) }}
                        disabled={calibrating}
                        className="px-2 py-1 text-xs rounded bg-neutral-700
                                   hover:bg-neutral-600 disabled:opacity-50">취소</button>
                      <button
                        onClick={() => calibrateGrayCard(settingsCamera.id)}
                        disabled={calibrating}
                        className="px-2 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500
                                   disabled:bg-neutral-700 disabled:text-neutral-500 text-white">
                        {calibrating ? '맞추는 중…' : '이 영역으로 맞추기'}
                      </button>
                    </span>
                  ) : (
                    <button
                      onClick={() => startAiming(settingsCamera.id)}
                      className="px-2 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white">
                      보정 시작
                    </button>
                  )}
                </div>
                <label className="flex items-center gap-1.5 text-[10px] text-neutral-400 cursor-pointer"
                       title="노출은 모션 블러와 fps 상한을 정합니다. 체크하면 지금 노출을 그대로 두고(자동 노출도 안 켭니다) 밝기를 gain 으로만 맞춥니다.">
                  <input type="checkbox" checked={gainOnly} onChange={toggleGainOnly}
                         disabled={calibrating} className="accent-blue-500" />
                  노출 고정 — 밝기는 gain 으로만 보정
                </label>
                {aiming ? (
                  <>
                    <p className="text-[10px] text-neutral-500">
                      왼쪽 화면에서 <b>카드 위를 클릭·드래그</b>해 노란 상자를 옮기고,
                      <b> 휠로 크기</b>를 맞추세요. 상자 안쪽만 계산에 씁니다.
                    </p>
                    {roiReading && (
                      <p className={`text-[10px] tabular-nums ${roiReading.usable
                        ? 'text-neutral-400' : 'text-amber-400'}`}>
                        지금 상자: 밝기 {roiReading.luma} · 색 치우침{' '}
                        {roiReading.neutral_error_pct}% · 얼룩 {roiReading.spread_pct}%
                        {!roiReading.usable && ` — ${roiReading.why}`}
                      </p>
                    )}
                  </>
                ) : (
                  <p className="text-[10px] text-neutral-500">
                    회색 카드를 화면에 두고 누르면 영역을 고르게 됩니다. 자동 노출·WB 를
                    잠깐 켜 카드에 맞춘 뒤 <b>잠그고</b>, 밝기를 목표까지 보정합니다.
                    값은 장치에만 올라갑니다 — 마음에 들면 [프로파일] 탭에서 캡처하세요.
                  </p>
                )}
                {grayCard && (
                  <div className={`rounded px-2 py-1.5 text-[11px] ${grayCard.ok
                    ? 'bg-emerald-500/10 text-emerald-300'
                    : 'bg-amber-500/10 text-amber-300'}`}>
                    <p className="font-medium">{grayCard.ok ? '맞음' : grayCard.verdict}</p>
                    <p className="mt-0.5 text-neutral-400 tabular-nums">
                      밝기 {grayCard.before.luma} → <b>{grayCard.after.luma}</b>
                      {' '}(목표 {grayCard.target}) · 색 치우침{' '}
                      {grayCard.before.neutral_error_pct}% →{' '}
                      <b>{grayCard.after.neutral_error_pct}%</b> · 노출{' '}
                      {(grayCard.exposure_us / 1000).toFixed(1)}ms
                      {grayCard.gain != null && <> · gain {grayCard.gain}
                        {grayCard.adjust === 'gain' && ' (노출 고정)'}</>}
                      {grayCard.white_balance != null && <> · WB {grayCard.white_balance}K</>}
                    </p>
                    {/* ⚠ **결과를 본 직후가 저장이 가장 필요한 순간이다.** 이
                        안내는 보정 **전** 문단에만 있었고, 조준을 시작하면
                        사라졌다. 그래서 좋은 값을 얻고도 [실시간 보기] 를 누르는
                        순간 프로파일 값으로 덮여 "다시 어두워진다"로 보고됐다.
                        연결할 때 프로파일을 다시 거는 것은 의도된 동작이다
                        (feature/gray-card-calibration.md §4) — 말해주지 않은 것이
                        문제였다. */}
                    <div className="mt-1 space-y-1 border-t border-current/20 pt-1">
                      <p className="text-[10px] text-amber-300">
                        아직 <b>장치에만</b> 올라가 있습니다. 저장하지 않으면
                        <b> 다음에 카메라를 열 때</b>(실시간 보기·녹화·추론)
                        프로파일 값으로 되돌아갑니다.
                      </p>
                      {/* 저장 버튼이 여기 있던 적이 있다 — 프로파일 편집이 탭으로
                          생기면서 저장 경로가 둘이 됐고, 하나만 남긴다(탭).
                          캡처는 지금 장치값을 읽으므로 이 창을 닫아도 값은 남는다. */}
                      <p className="text-[10px] text-neutral-400">
                        남기려면 이 창을 닫고 위쪽 <b>[프로파일] 탭에서 캡처</b>하세요 —
                        지금 장치에 들어 있는 이 값이 그대로 담깁니다.
                        {activeProfile && ` (활성 '${activeProfile}' 에 덮어쓰려면 같은 이름으로)`}
                      </p>
                    </div>
                  </div>
                )}
              </div>
            )}

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
                {/* 배경 마스킹 — 경계가 위 `먼 쪽`과 같아서 여기 둔다.
                    지우는 대상은 **같은 장치의 컬러 스트림**이다. */}
                <div className="space-y-1.5 rounded border border-neutral-700 p-2">
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input type="checkbox" checked={maskOn}
                      onChange={(e) => setBackgroundMask(settingsCamera.id,
                                                         { enabled: e.target.checked })}
                      className="mt-0.5 accent-blue-500" />
                    <span className="text-xs text-neutral-300">
                      이 장치의 <b>컬러</b>에서 배경 지우기
                    </span>
                  </label>

                  {maskOn && (
                    <>
                      {/* 경계는 깊이 창과 **따로** 둘 수 있다 — 인코딩 창은 해상도를
                          위해 좁히고, 마스킹은 더 멀리까지 남기고 싶을 때가 있다. */}
                      <ParamSlider label="이 거리보다 멀면 지움" unit="mm"
                        value={maskFar}
                        min={DEPTH_STEP}
                        max={Math.max(DEPTH_SLIDER_MAX, maskFar)}
                        step={DEPTH_STEP}
                        onChange={setMaskFar}
                        onCommit={(v) => setBackgroundMask(settingsCamera.id, { far_mm: v })} />

                      <label className="flex items-start gap-2 cursor-pointer">
                        <input type="checkbox" checked={maskKeepUnknown}
                          onChange={(e) => setBackgroundMask(
                            settingsCamera.id, { keep_unknown: e.target.checked })}
                          className="mt-0.5 accent-blue-500" />
                        <span className="text-xs text-neutral-300">
                          깊이를 못 읽은 곳은 남기기
                          <span className="block text-[10px] text-neutral-500">
                            깊이가 없다고 물체가 없는 건 아닙니다 — 이 카메라는 프레임의
                            상당 부분을 못 읽을 수 있고, 지우면 <b>물체 한가운데가
                            뚫립니다</b>. 깊이가 잘 잡히는 장면이면 꺼서 배경을 더
                            깔끔하게 지울 수 있습니다.
                          </span>
                        </span>
                      </label>
                    </>
                  )}
                </div>

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
                    {/* 값 자체도 숫자로 친다 — 노출처럼 범위가 1~16만인 컨트롤은
                        슬라이더로 정확한 값을 맞출 수 없다. Enter/포커스 아웃에
                        커밋하고, 밖에서 값이 바뀌면 key 로 다시 그린다. */}
                    {ctrl.type === 2 || ctrl.type === 3 ? (
                      <span className="text-neutral-300 w-16 text-right font-mono">{ctrl.value}</span>
                    ) : (
                      <input type="number" key={`${ctrl.name}:${ctrl.value}`}
                        defaultValue={ctrl.value} disabled={locked}
                        min={ctrl.min} max={ctrl.max} step={ctrl.step || 1}
                        onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }}
                        onBlur={(e) => {
                          const v = Number(e.target.value)
                          if (Number.isNaN(v)) return
                          const clamped = Math.min(ctrl.max, Math.max(ctrl.min, v))
                          if (clamped !== ctrl.value) handleControl(settingsCamera.id, ctrl.name, clamped)
                        }}
                        className="w-16 shrink-0 px-1 py-0.5 rounded bg-neutral-900 border
                                   border-neutral-700 text-right font-mono text-neutral-100
                                   disabled:opacity-50" />
                    )}
                    {/* 단위 칸은 항상 그린다 — 있는 행만 넓어지면 슬라이더 폭이 줄마다 다르다 */}
                    <span className="w-12 shrink-0 text-[10px] text-neutral-500"
                          title={unitHint(ctrl)}>{ctrl.unit ?? ''}</span>
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
