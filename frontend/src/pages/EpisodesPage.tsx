/**
 * 에피소드 뷰어 — 재생·신호·페이즈를 한 화면에서 본다 (feature/episode-editor.md 2단계).
 *
 * 이 단계는 **읽기 전용**이다: 재생 + 신호 그래프 + 페이즈 트랙 + ⚠ 정렬.
 * 구간 편집(드래그/분할/병합)은 3단계, 삭제·task 이관은 4단계.
 *
 * ## 재생은 이중 모드다 (§3)
 *
 * - **동영상(기본)**: chunk mp4 를 Range 서빙으로 받아 `<video>` 로 재생.
 *   에피소드 경계는 메타의 from/to_timestamp, 프레임 동기는
 *   requestVideoFrameCallback 의 mediaTime. 캐시 생성 없이 즉시 열린다.
 * - **프레임 캐시(폴백·편집용)**: 코덱을 브라우저가 못 읽거나(yuv444p 등)
 *   메타에 타임스탬프가 없는 구버전이면 자동 전환. 3단계 편집의
 *   프레임 단위 정밀 조작도 이쪽이 정본이다.
 *
 * 페이즈 사이드카는 선택적 입력이다 — 없으면 트랙 자리에 [분석] 버튼만 보이고
 * 재생은 그대로 동작한다. 분류기(`python -m piper_phase`)가 만든 사이드카도
 * 같은 파일이라 똑같이 보인다.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../services/api'
import LayoutToggle, { useLayout } from '../components/LayoutToggle'
import { useSystemMessage } from '../components/SystemMessages'
import PlotlyChart from '../components/PlotlyChart'
import type { BakedInfo, Dataset, DatasetDetail } from '../types/models'

// ── 페이즈 사이드카 형태 (backend/app/routers/phase.py 응답) ──

type PhaseEpisode = {
  segments: [number, number, number][] // [start, end(포함), code]
  cycles: number
  frames: number
  reviewed: boolean
  note: string
}
type PhaseLabels = {
  phases: string[]
  params: Record<string, number>
  episodes: Record<string, PhaseEpisode>
}
type PhaseOutlier = { episode: number; cycles: number; reasons: string[] }
type PhaseSummary = {
  episodes: number
  cycle_distribution: Record<string, number>
  median_cycles: number
  outliers: PhaseOutlier[]
}
type Signals = {
  frames: number
  speed: number[]          // 관절 공간 — 정규화 단위/초
  gripper_gap: number[]
  phase: number[]
  /** 말단 속도 (m/s). URDF 서브모듈이 없으면 안 온다. */
  tip_speed?: number[]
  /** 시작 자세로부터 말단이 떨어진 거리 (m). PARKING 판정의 근거다. */
  home_dist?: number[]
  /** 관절별 실측·지령 (정규화 단위). 축 순서는 `names` 가 정한다. */
  joints?: { names: string[]; state: number[][]; action: number[][] }
}

/** 에피소드의 캠별 비디오 위치 (meta/episodes 의 videos/{key}/* 컬럼) */
type VideoMeta = { chunk: number; file: number; from: number; to: number }

// requestVideoFrameCallback 은 아직 lib.dom 에 없다 (지원: Chrome/Edge/Safari/FF132+)
type VideoFrameMeta = { mediaTime: number }
type VideoWithRVFC = HTMLVideoElement & {
  requestVideoFrameCallback?: (cb: (now: number, meta: VideoFrameMeta) => void) => number
  cancelVideoFrameCallback?: (handle: number) => void
}

/** 페이즈 코드(0~6) 색 — 트랙·칩·범례가 같은 배열을 쓴다. */
// ⚠ **인덱스가 곧 페이즈 코드다** (piper_phase.PHASE_NAMES 순서).
//   페이즈를 늘리면 여기도 늘려야 한다 — 짧으면 `undefined` 가 되어 구간이
//   **투명하게** 그려지고, 라벨이 없는 것처럼 보인다.
const PHASE_COLORS = [
  '#525252',  // IDLE
  '#3b82f6',  // APPROACH
  '#22d3ee',  // ALIGN
  '#f59e0b',  // GRASP
  '#22c55e',  // HOLD
  '#a855f7',  // RELEASE
  '#404040',  // DONE
  '#78716c',  // PARKING — 복귀. DONE 과 이웃하되 구분되게 (둘 다 무채색 계열)
]

const PREFETCH_AHEAD = 12

type EpisodeRow = {
  index: number
  length: number
  cycles: number | null
  reviewed: boolean
  reasons: string[] // 비어 있으면 정상
}

type Segment = [number, number, number]

export default function EpisodesPage() {
  const { notify, confirm: askConfirm } = useSystemMessage()
  const notifyError = (text: string) => notify({ level: 'error', text, source: '에피소드' })

  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [dsId, setDsId] = useState<string>('')
  const [detail, setDetail] = useState<DatasetDetail | null>(null)
  const [labels, setLabels] = useState<PhaseLabels | null>(null)
  const [summary, setSummary] = useState<PhaseSummary | null>(null)
  const [ep, setEp] = useState<number | null>(null)
  const [signals, setSignals] = useState<Signals | null>(null)
  const [frame, setFrame] = useState(0)
  const [playing, setPlaying] = useState(false)
  // ⚠ 에피소드 목록(왼쪽)은 이 선택과 무관하다 — 목록은 늘 옆에 붙어 있어야
  //   J/K 로 오가며 비교할 수 있다. 바뀌는 것은 **사진과 그래프의 관계**뿐이다.
  const { layout, switchLayout } = useLayout('episodes')
  const [viewMode, setViewMode] = useState<'video' | 'frames'>('video')
  // ⚠ 관절 그래프는 축마다 하나씩 = 7개다. 늘 펼쳐두면 신호 그래프가 화면
  //   밖으로 밀려난다 — 기본은 접고, 선택은 기억한다.
  const [showJoints, setShowJoints] = useState(
    () => localStorage.getItem('episodes-show-joints') === '1')
  const [videoError, setVideoError] = useState(false)
  const [cacheMissing, setCacheMissing] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [baking, setBaking] = useState(false)

  // ── YOLO 학습 데이터셋으로 캡처 (feature/yolo-training.md 뷰어 훅) ──
  // 동영상 모드 = canvas 캡처 (캐시 불필요, 출처에 재생 시각 t)
  // 프레임 모드 = 캐시 파일 복사 (정확한 프레임 번호)
  const [yoloDatasets, setYoloDatasets] = useState<string[]>([])
  const [yoloTarget, setYoloTarget] = useState('')

  useEffect(() => {
    api.get<{ datasets: { name: string }[] }>('/yolo/datasets')
      .then((r) => {
        setYoloDatasets(r.datasets.map((d) => d.name))
        setYoloTarget((cur) => cur || (r.datasets[0]?.name ?? ''))
      })
      .catch(() => {})
  }, [])
  // 수동 분석 (01-phase-annotation §3.5): 파라미터를 만지고, 한 에피소드로
  // 미리보기(저장 안 함) 후 만족하면 전체 재분석으로 확정한다.
  const [defaultParams, setDefaultParams] = useState<Record<string, number> | null>(null)
  const [defaultPhases, setDefaultPhases] = useState<string[]>([])
  const [paramValues, setParamValues] = useState<Record<string, number>>({})
  const [showParams, setShowParams] = useState(false)
  const [preview, setPreview] = useState<{ segments: [number, number, number][]; cycles: number } | null>(null)
  // 타임라인 수작업 편집 (3단계): draft 가 있으면 편집 모드.
  // 분할(S)·병합(M)·페이즈 지정(0~6)·경계 드래그 — 저장은 PUT (백엔드가 빈틈·겹침 검증)
  const [draft, setDraft] = useState<Segment[] | null>(null)
  const [selectedSeg, setSelectedSeg] = useState<number | null>(null)
  const [undoStack, setUndoStack] = useState<Segment[][]>([])
  const draftRef = useRef<Segment[] | null>(null)
  draftRef.current = draft
  const trackRef = useRef<HTMLDivElement | null>(null)
  // 에피소드 수명주기 (4단계): 재생으로 **확인한 뒤** 삭제·task 수정 — REF §5.3 의 흐름.
  // 삭제는 CLI 래핑(edit 유닛)이라 비동기다: activity 폴링으로 완료를 기다린다.
  const [marked, setMarked] = useState<number[]>([])
  const [lifecycleBusy, setLifecycleBusy] = useState(false)
  const [taskInput, setTaskInput] = useState('')

  const videoRefs = useRef<Record<string, VideoWithRVFC | null>>({})

  useEffect(() => {
    api.get<Dataset[]>('/datasets').then(setDatasets).catch(() => {})
    api.get<{ params: Record<string, number>; phases: string[] }>('/phase/defaults')
      .then((r) => { setDefaultParams(r.params); setDefaultPhases(r.phases) })
      .catch(() => {})
  }, [])

  // 파라미터 시드: 사이드카에 저장된 값 > 기본값 — 임계값은 태스크마다 다르다
  // (min_cube 기본값으로 yeonwonju 를 돌리면 0사이클이 16개 나온다, §3.5)
  useEffect(() => {
    if (!defaultParams) return
    setParamValues({ ...defaultParams, ...(labels?.params ?? {}) })
  }, [defaultParams, labels])

  const toggleMark = (idx: number) =>
    setMarked((m) => m.includes(idx) ? m.filter((i) => i !== idx) : [...m, idx].sort((a, b) => a - b))

  const waitEditDone = async () => {
    // edit 유닛이 끝날 때까지 (최대 4분). 실패해도 폴링만 끊긴다 — 편집은 유닛이라 계속 돈다
    for (let i = 0; i < 240; i++) {
      await new Promise((r) => setTimeout(r, 1000))
      try {
        const s = await api.get<{ running: string[] }>('/activity')
        if (!s.running.includes('dataset_edit')) return
      } catch { /* 게이트웨이 순단은 무시 */ }
    }
  }

  const handleDeleteMarked = async () => {
    if (!dsId || marked.length === 0) return
    const ok = await askConfirm(
      `에피소드 ${marked.map((i) => `#${i}`).join(', ')} — ${marked.length}개를 삭제하시겠습니까?\n` +
      '삭제 후 뒤 에피소드 번호가 당겨지고, 페이즈 라벨·신호는 자동으로 따라옵니다.')
    if (!ok) return
    setLifecycleBusy(true)
    try {
      await api.post(`/datasets/${dsId}/edit`, {
        operation: 'delete_episodes',
        params: { episode_indices: JSON.stringify(marked) },
      })
      await waitEditDone()
      setMarked([])
      await selectDataset(dsId)
      notify({ level: 'info', text: '삭제 완료 — 번호 재정렬 + 사이드카 동기화됨', source: '에피소드' })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '삭제 실패')
    } finally { setLifecycleBusy(false) }
  }

  const handleTaskUpdate = async () => {
    if (!dsId || marked.length === 0 || !taskInput.trim()) return
    setLifecycleBusy(true)
    try {
      await api.post(`/datasets/${dsId}/update-task`, {
        episode_indices: marked, task: taskInput.trim(),
      })
      notify({ level: 'info', text: `에피소드 ${marked.length}개 task 변경됨`, source: '에피소드' })
      setMarked([]); setTaskInput('')
      await selectDataset(dsId)
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'task 변경 실패')
    } finally { setLifecycleBusy(false) }
  }

  const loadPhase = useCallback(async (id: string) => {
    // 사이드카는 선택적 — 404 는 "아직 분석 안 됨"이지 오류가 아니다
    setLabels(await api.get<PhaseLabels>(`/phase/${id}/labels`).catch(() => null))
    setSummary(await api.get<PhaseSummary>(`/phase/${id}/summary`).catch(() => null))
  }, [])

  const selectDataset = useCallback(async (id: string) => {
    setDsId(id)
    setEp(null)
    setSignals(null)
    setPlaying(false)
    setVideoError(false)
    setCacheMissing(false)
    setPreview(null)
    setDraft(null)
    setSelectedSeg(null)
    setUndoStack([])
    setMarked([])
    if (!id) { setDetail(null); setLabels(null); setSummary(null); return }
    try {
      setDetail(await api.get<DatasetDetail>(`/datasets/${id}`))
    } catch { notifyError(`데이터셋을 열 수 없습니다: ${id}`); return }
    await loadPhase(id)
  }, [loadPhase]) // eslint-disable-line react-hooks/exhaustive-deps

  const selectEpisode = useCallback(async (id: string, index: number) => {
    // 편집 중이면 버리기 전에 묻는다 — 수작업이 조용히 날아가면 안 된다
    if (draftRef.current
        && !(await askConfirm('편집 중인 구간이 저장되지 않았습니다. 버리고 이동할까요?', { danger: true }))) {
      return
    }
    setDraft(null)
    setSelectedSeg(null)
    setUndoStack([])
    setEp(index)
    setFrame(0)
    setPlaying(false)
    setVideoError(false)
    setCacheMissing(false)
    setPreview(null)
    setSignals(await api.get<Signals>(`/phase/${id}/signals/${index}`).catch(() => null))
  }, [askConfirm])

  // ── 파생 상태 ──

  const cams = useMemo(
    () => Object.keys(detail?.features ?? {})
      .filter((k) => k.startsWith('observation.images.'))
      .map((k) => k.slice('observation.images.'.length)),
    [detail],
  )

  const episodes = useMemo<EpisodeRow[]>(() => {
    if (!detail) return []
    const flagged = new Map((summary?.outliers ?? []).map((o) => [o.episode, o.reasons]))
    const rows = detail.episodes.map((e, i) => {
      const index = (e.episode_index as number) ?? i
      const labeled = labels?.episodes[String(index)]
      return {
        index,
        length: (e.length as number) ?? labeled?.frames ?? 0,
        cycles: labeled?.cycles ?? null,
        reviewed: labeled?.reviewed ?? false,
        reasons: flagged.get(index) ?? [],
      }
    })
    // ⚠ 먼저 (사유 많은 순) — 50개를 다 볼 필요 없이 이상한 것부터 본다
    return rows.sort((a, b) => b.reasons.length - a.reasons.length || a.index - b.index)
  }, [detail, labels, summary])

  const current = ep != null ? episodes.find((r) => r.index === ep) : undefined
  const labeledEp = ep != null ? labels?.episodes[String(ep)] : undefined
  const totalFrames = labeledEp?.frames ?? current?.length ?? 0
  const fps = detail?.fps ?? 15

  /** 캠별 비디오 위치. 컬럼이 없으면(구버전 메타) null → 프레임 캐시로 폴백. */
  const videoMeta = useMemo<Record<string, VideoMeta> | null>(() => {
    if (ep == null || !detail || cams.length === 0) return null
    const rec = detail.episodes.find((e, i) => ((e.episode_index as number) ?? i) === ep)
    if (!rec) return null
    const out: Record<string, VideoMeta> = {}
    for (const cam of cams) {
      const p = `videos/observation.images.${cam}/`
      const from = rec[`${p}from_timestamp`]
      if (typeof from !== 'number') return null
      out[cam] = {
        chunk: (rec[`${p}chunk_index`] as number) ?? 0,
        file: (rec[`${p}file_index`] as number) ?? 0,
        from,
        to: (rec[`${p}to_timestamp`] as number) ?? from,
      }
    }
    return out
  }, [ep, detail, cams])

  const videoActive = viewMode === 'video' && videoMeta != null && !videoError

  const phaseNames = labels?.phases ?? defaultPhases
  // 미리보기가 있으면 트랙은 그걸 보여준다 — 저장본과 파라미터를 비교하는 화면이다.
  // 편집 중(draft)이면 draft 가 최우선이다.
  const displaySegments = preview?.segments ?? labeledEp?.segments
  const shownSegments = draft ?? displaySegments
  const currentSegment = useMemo(
    () => shownSegments?.find(([s, e]) => s <= frame && frame <= e),
    [shownSegments, frame],
  )

  const frameUrl = useCallback(
    (cam: string, f: number) => `/api/datasets/${dsId}/episodes/${ep}/frames/${cam}/${f}`,
    [dsId, ep],
  )
  const videoUrl = useCallback(
    (cam: string) => {
      const m = videoMeta?.[cam]
      return m ? `/api/datasets/${dsId}/videos/${cam}/${m.chunk}/${m.file}` : ''
    },
    [dsId, videoMeta],
  )

  const frameRef = useRef(frame)
  frameRef.current = frame

  /** 보고 있는 그 장면을 YOLO 데이터셋으로 — 모드에 따라 canvas / 캐시 복사. */
  const captureToYolo = async (cam: string) => {
    if (!yoloTarget || ep == null) return
    try {
      if (videoActive) {
        const v = videoRefs.current[cam]
        if (!v || v.readyState < 2) { notifyError('비디오가 아직 준비되지 않았습니다'); return }
        const canvas = document.createElement('canvas')
        canvas.width = v.videoWidth
        canvas.height = v.videoHeight
        canvas.getContext('2d')!.drawImage(v, 0, 0)
        const blob = await new Promise<Blob | null>((res) => canvas.toBlob(res, 'image/jpeg', 0.9))
        if (!blob) throw new Error('canvas 캡처 실패')
        // t 는 에피소드 기준 시각 — chunk 파일은 여러 에피소드를 담는다
        const t = v.currentTime - (videoMeta?.[cam]?.from ?? 0)
        const q = new URLSearchParams({
          type: 'episode', dataset: dsId, episode: String(ep), cam, t: t.toFixed(3),
        })
        const res = await fetch(`/api/yolo/datasets/${yoloTarget}/images?${q}`, {
          method: 'POST', body: blob,
        })
        if (!res.ok) {
          const detail = (await res.json().catch(() => null))?.detail
          throw new Error(detail ?? `${res.status} ${res.statusText}`)
        }
      } else {
        // 프레임 캐시 모드 — 서버가 캐시 파일을 복사한다 (정확한 프레임 번호)
        await api.post(`/yolo/datasets/${yoloTarget}/import-episode`, {
          dataset_id: dsId, episode: ep, cam, indices: [frame],
        })
      }
      notify({ level: 'info', text: `${cam} → ${yoloTarget} 데이터셋으로 캡처`, source: '에피소드' })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '캡처 실패')
    }
  }

  /** f 프레임의 비디오 시각(프레임 중앙) */
  const videoTime = useCallback(
    (m: VideoMeta, f: number) => m.from + (f + 0.5) / fps,
    [fps],
  )

  const seekVideos = useCallback((f: number) => {
    if (!videoMeta) return
    for (const cam of cams) {
      const v = videoRefs.current[cam]
      const m = videoMeta[cam]
      if (v && m && v.readyState >= 1) v.currentTime = videoTime(m, f)
    }
  }, [videoMeta, cams, videoTime])

  /** 모든 탐색은 여기로 — 프레임 상태와 비디오 위치가 같이 움직인다. */
  const goTo = useCallback((f: number) => {
    const clamped = Math.max(0, Math.min(totalFrames - 1, f))
    setFrame(clamped)
    if (videoActive) seekVideos(clamped)
  }, [totalFrames, videoActive, seekVideos])

  // ── 재생: 동영상 모드 — <video> 가 시계다 ──

  useEffect(() => {
    if (!videoActive) return
    const els = cams.map((c) => videoRefs.current[c]).filter(Boolean) as VideoWithRVFC[]
    if (playing) {
      els.forEach((v) => {
        void v.play().catch((err: unknown) => {
          // ⚠ AbortError 는 에피소드 전환·일시정지가 재생 시작을 끊었을 뿐이다.
          // 이걸 폴백시키면 멀쩡한 h264 데이터셋에서 "코덱" 오진 배너가 뜬다 (실사고).
          if ((err as DOMException)?.name === 'NotSupportedError') setVideoError(true)
        })
      })
    } else els.forEach((v) => v.pause())
  }, [playing, videoActive, cams])

  useEffect(() => {
    if (!videoActive || !videoMeta || cams.length === 0) return
    const master = videoRefs.current[cams[0]]
    const m = videoMeta[cams[0]]
    if (!master || !m) return

    const syncSlaves = (masterTime: number) => {
      for (const cam of cams.slice(1)) {
        const v = videoRefs.current[cam]
        const mm = videoMeta[cam]
        if (v && mm) {
          const want = mm.from + (masterTime - m.from)
          if (Math.abs(v.currentTime - want) > 1 / fps) v.currentTime = want
        }
      }
    }
    const onPresented = (mediaTime: number) => {
      setFrame(Math.max(0, Math.min(totalFrames - 1, Math.round((mediaTime - m.from) * fps))))
      // 에피소드 끝(to_timestamp)에서 멈춘다 — 파일에는 다음 에피소드가 이어져 있다
      if (mediaTime >= m.to - 0.5 / fps) setPlaying(false)
      else syncSlaves(mediaTime)
    }

    const rvfc = master.requestVideoFrameCallback?.bind(master)
    if (rvfc) {
      let handle = 0
      const loop = (_now: number, meta: VideoFrameMeta) => {
        onPresented(meta.mediaTime)
        handle = rvfc(loop)
      }
      handle = rvfc(loop)
      return () => master.cancelVideoFrameCallback?.(handle)
    }
    // 폴백(rVFC 미지원): timeupdate ~4Hz — 프레임 표시 정밀도만 떨어진다
    const onTime = () => onPresented(master.currentTime)
    master.addEventListener('timeupdate', onTime)
    return () => master.removeEventListener('timeupdate', onTime)
  }, [videoActive, videoMeta, cams, fps, totalFrames])

  // ── 재생: 프레임 캐시 모드 — rAF 가 시계다 ──

  useEffect(() => {
    if (videoActive || !playing || totalFrames === 0) return
    let raf = 0
    let last = performance.now()
    const stepMs = 1000 / fps
    const tick = (now: number) => {
      const steps = Math.floor((now - last) / stepMs)
      if (steps > 0) {
        last += steps * stepMs
        const next = Math.min(frameRef.current + steps, totalFrames - 1)
        setFrame(next)
        if (next >= totalFrames - 1) { setPlaying(false); return }
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [videoActive, playing, fps, totalFrames])

  // 앞 프레임 프리페치 — 서버가 immutable 캐시 헤더를 주므로 재생이 끊기지 않는다
  useEffect(() => {
    if (videoActive || ep == null || !dsId) return
    for (const cam of cams) {
      for (let i = 1; i <= PREFETCH_AHEAD; i++) {
        if (frame + i >= totalFrames) break
        new Image().src = frameUrl(cam, frame + i)
      }
    }
  }, [videoActive, frame, ep, dsId, cams, totalFrames, frameUrl])

  // ── 타임라인 수작업 편집 ──

  const pushUndo = useCallback(() => {
    const d = draftRef.current
    if (d) setUndoStack((u) => [...u.slice(-49), d.map((s) => [...s] as Segment)])
  }, [])

  // 구운 사본(ACT-Aux 용 `_stage`)은 읽기 전용 — 여기서 고친 구간은 원본에 반영되지 않고
  // 다음 bake 에 조용히 덮인다. 원본에서 고치고 다시 굽는다 (feature/act-aux.md §4.5).
  const baked: BakedInfo | null = datasets.find((d) => d.id === dsId)?.baked ?? null

  const enterEdit = () => {
    if (!displaySegments) return
    if (baked) {
      notifyError(`구운 사본은 편집할 수 없습니다 — 원본(${baked.source ?? '?'})에서 고치고 다시 구우세요`)
      return
    }
    setPlaying(false)
    setDraft(displaySegments.map((s) => [...s] as Segment))
    setSelectedSeg(null)
    setUndoStack([])
  }

  const cancelEdit = () => { setDraft(null); setSelectedSeg(null); setUndoStack([]) }

  const undoEdit = useCallback(() => {
    setUndoStack((u) => {
      if (!u.length) return u
      setDraft(u[u.length - 1])
      return u.slice(0, -1)
    })
  }, [])

  const changePhase = useCallback((code: number) => {
    if (selectedSeg == null || !draftRef.current) return
    pushUndo()
    setDraft((d) => d && d.map((s, i) => (i === selectedSeg ? [s[0], s[1], code] as Segment : s)))
  }, [selectedSeg, pushUndo])

  /** 추가 = 재생헤드에서 분할. 새 오른쪽 구간이 선택되니 바로 0~6 으로 페이즈를 준다. */
  const splitAtPlayhead = useCallback(() => {
    const d = draftRef.current
    if (!d) return
    const f = frameRef.current
    const idx = d.findIndex(([s, e]) => s <= f && f <= e)
    if (idx < 0 || f <= d[idx][0]) return // 구간 시작 프레임에서는 나눌 것이 없다
    pushUndo()
    const [s, e, code] = d[idx]
    setDraft([...d.slice(0, idx), [s, f - 1, code], [f, e, code], ...d.slice(idx + 1)])
    setSelectedSeg(idx + 1)
  }, [pushUndo])

  /** 삭제 = 선택 구간을 앞 구간에 흡수 (첫 구간이면 뒤 구간이 흡수). */
  const mergeSelected = useCallback(() => {
    const d = draftRef.current
    if (!d || d.length < 2 || selectedSeg == null) return
    pushUndo()
    const i = selectedSeg
    const nd = d.map((s) => [...s] as Segment)
    if (i > 0) {
      nd[i - 1][1] = nd[i][1]
      nd.splice(i, 1)
      setSelectedSeg(i - 1)
    } else {
      nd[1][0] = nd[0][0]
      nd.splice(0, 1)
      setSelectedSeg(0)
    }
    setDraft(nd)
  }, [selectedSeg, pushUndo])

  /** 수정 = 경계 드래그. 이웃 두 구간이 같이 늘고 준다 — 빈틈·겹침이 생길 수 없다. */
  const startBoundaryDrag = useCallback((idx: number, e: React.PointerEvent) => {
    e.preventDefault()
    e.stopPropagation()
    pushUndo()
    const move = (ev: PointerEvent) => {
      const rect = trackRef.current?.getBoundingClientRect()
      const d = draftRef.current
      if (!rect || !d) return
      const lo = d[idx][0] + 1        // 양쪽 모두 최소 1프레임은 남긴다
      const hi = d[idx + 1][1]
      const f = Math.max(lo, Math.min(hi, Math.round(((ev.clientX - rect.left) / rect.width) * totalFrames)))
      setDraft((cur) => {
        if (!cur) return cur
        const nd = cur.map((s) => [...s] as Segment)
        nd[idx][1] = f - 1
        nd[idx + 1][0] = f
        return nd
      })
      goTo(f) // 경계 프레임을 화면으로 보면서 놓는다 — 프레임 단위 편집의 요점
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }, [pushUndo, totalFrames, goTo])

  const saveEdit = async () => {
    if (!draft || ep == null) return
    try {
      // 백엔드가 빈틈·겹침·프레임 수 검증을 한다 — 깨진 구간은 저장 자체가 거부된다
      await api.put(`/phase/${dsId}/labels/${ep}`, { segments: draft, reviewed: true })
      setDraft(null)
      setSelectedSeg(null)
      setUndoStack([])
      setPreview(null)
      await loadPhase(dsId)
      notify({ level: 'info', text: `에피소드 ${ep} 구간 저장됨 (검토됨 ✔)`, source: '에피소드' })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '저장 실패')
    }
  }

  // ── 단축키 (01-phase-annotation §4.3 의 뷰어 부분집합) ──

  const moveEpisode = useCallback((d: number) => {
    if (ep == null || episodes.length === 0) return
    const pos = episodes.findIndex((r) => r.index === ep)
    const next = episodes[pos + d]
    if (next) void selectEpisode(dsId, next.index)
  }, [ep, episodes, dsId, selectEpisode])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (t.tagName === 'TEXTAREA' || t.tagName === 'SELECT') return
      // range(스크러버)만 전역 단축키를 허용 — 숫자/텍스트 입력은 타이핑이 우선이다
      if (t.tagName === 'INPUT' && (t as HTMLInputElement).type !== 'range') return
      // 편집 모드 단축키 (01 §4.3): 0~6 페이즈 지정 · S 분할 · M 병합 · Ctrl+Z 되돌리기
      if (draftRef.current) {
        if (!e.ctrlKey && !e.metaKey && !e.altKey && e.key >= '0' && e.key <= '6') {
          e.preventDefault(); changePhase(Number(e.key)); return
        }
        if (e.key === 's' || e.key === 'S') { e.preventDefault(); splitAtPlayhead(); return }
        if (e.key === 'm' || e.key === 'M') { e.preventDefault(); mergeSelected(); return }
        if ((e.ctrlKey || e.metaKey) && (e.key === 'z' || e.key === 'Z')) {
          e.preventDefault(); undoEdit(); return
        }
      }
      if (e.code === 'Space') { e.preventDefault(); if (totalFrames > 0) setPlaying((p) => !p) }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); goTo(frameRef.current + (e.shiftKey ? -10 : -1)) }
      else if (e.key === 'ArrowRight') { e.preventDefault(); goTo(frameRef.current + (e.shiftKey ? 10 : 1)) }
      else if (e.key === 'j' || e.key === 'J') moveEpisode(-1)
      else if (e.key === 'k' || e.key === 'K') moveEpisode(1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [goTo, moveEpisode, totalFrames, changePhase, splitAtPlayhead, mergeSelected, undoEdit])

  // ── 동작 ──

  // fps 는 데이터셋 소유라 보내지 않는다 (백엔드가 info.json 에서 채운다).
  // NaN(입력 중 빈 칸)은 걸러야 JSON 직렬화가 안 깨진다.
  const sendableParams = useMemo(
    () => Object.fromEntries(
      Object.entries(paramValues).filter(([k, v]) => k !== 'fps' && Number.isFinite(v)),
    ),
    [paramValues],
  )

  const reloadDatasets = () => api.get<Dataset[]>('/datasets').then(setDatasets).catch(() => {})

  // ACT-Aux 용 굽기: 사이드카 → LeRobot subtask 로 `<name>_stage` 사본 생성 (feature/act-aux.md §4)
  const handleBake = async () => {
    if (!dsId || baking) return
    const eps = labels ? Object.values(labels.episodes) : []
    const unreviewed = eps.filter((e) => !e.reviewed).length
    const reviewedOnly = unreviewed > 0
      && (await askConfirm(`검토 안 된 에피소드가 ${unreviewed}개 있습니다. 그 에피소드는 라벨 없음으로 굽을까요? (취소하면 전부 포함)`))
    setBaking(true)
    try {
      const r = await api.post<{ output_id: string; log: string[] }>(`/phase/${dsId}/bake`, { reviewed_only: reviewedOnly, force: true })
      notify({ level: 'info', text: `구웠습니다 → ${r.output_id} (학습 화면에서 ACT-Aux 로 선택)`, source: '에피소드' })
      await reloadDatasets()
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '굽기 실패')
    } finally {
      setBaking(false)
    }
  }

  const handleAnalyze = async () => {
    if (!dsId) return
    setAnalyzing(true)
    try {
      await api.post(`/phase/${dsId}/analyze`, { params: sendableParams })
      setPreview(null)
      await loadPhase(dsId)
      if (ep != null) setSignals(await api.get<Signals>(`/phase/${dsId}/signals/${ep}`).catch(() => null))
      notify({ level: 'info', text: '페이즈 분석 완료', source: '에피소드' })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '분석 실패')
    } finally { setAnalyzing(false) }
  }

  const handlePreview = async () => {
    if (!dsId || ep == null) return
    setAnalyzing(true)
    try {
      const r = await api.post<{
        episode_labels?: Record<string, { segments: [number, number, number][]; cycles: number }>
      }>(`/phase/${dsId}/analyze`, {
        params: sendableParams, episodes: [ep], save: false, include_segments: true,
      })
      const p = r.episode_labels?.[String(ep)]
      if (p) setPreview({ segments: p.segments, cycles: p.cycles })
      else notifyError('미리보기 결과가 비어 있습니다')
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '미리보기 실패')
    } finally { setAnalyzing(false) }
  }

  const handleCreateCache = async () => {
    try {
      await api.post(`/datasets/${dsId}/decode-cache`, { format: 'jpeg', max_dim: 320 })
      notify({ level: 'info', text: '디코딩 캐시 생성 시작 — 완료 후 에피소드를 다시 선택하세요', source: '에피소드' })
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '캐시 생성 실패')
    }
  }

  // ── 렌더 ──

  const distText = summary
    ? Object.entries(summary.cycle_distribution).map(([c, n]) => `${c}사이클×${n}`).join(' ')
    : null

  return (
    <div className="flex gap-4 items-start">
      {/* 에피소드 리스트 */}
      <aside className="w-72 shrink-0 space-y-3">
        <select
          value={dsId}
          onChange={(e) => void selectDataset(e.target.value)}
          className="w-full rounded bg-neutral-800 border border-neutral-700 px-2 py-1.5 text-sm"
        >
          <option value="">데이터셋 선택…</option>
          {datasets.map((d) => (
            <option key={d.id} value={d.id}>{d.id} ({d.total_episodes}){d.baked ? ' · 구운 사본' : ''}</option>
          ))}
        </select>

        {detail && (
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 text-xs space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-neutral-400">
                {distText ?? '페이즈 분석 안 됨'}
                {summary && ` (중앙값 ${summary.median_cycles})`}
              </span>
              {!baked && (
                <span className="flex gap-1">
                  <button
                    onClick={() => void handleAnalyze()}
                    disabled={analyzing}
                    className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white disabled:opacity-50"
                  >
                    {analyzing ? '분석 중…' : labels ? '재분석' : '분석'}
                  </button>
                  {labels && (
                    <button
                      onClick={() => void handleBake()}
                      disabled={baking || analyzing || draft != null}
                      title="사이드카를 LeRobot subtask 로 구워 ACT-Aux 학습용 `_stage` 사본을 만든다 (원본 불변)"
                      className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-emerald-600 text-neutral-300 hover:text-white disabled:opacity-50"
                    >
                      {baking ? '굽는 중…' : 'ACT-Aux용 굽기'}
                    </button>
                  )}
                </span>
              )}
            </div>
            {baked && (
              <div className="rounded border border-neutral-600 bg-neutral-900 p-2 space-y-0.5">
                <div className="text-neutral-300">🔒 ACT-Aux용 구운 사본 — 읽기 전용</div>
                <div className="text-neutral-500">원본: {baked.source ?? '?'}</div>
                {baked.source_missing && <div className="text-amber-400">⚠ 원본이 없다 — 다시 구울 수 없음</div>}
                {baked.stale && <div className="text-amber-400">⚠ 원본 라벨이 bake 뒤에 바뀜 — 원본에서 다시 구워야 학습에 반영된다</div>}
              </div>
            )}
            {summary && summary.outliers.length > 0 && (
              <div className="text-amber-400">⚠ 이상 에피소드 {summary.outliers.length}개 — 위로 정렬됨</div>
            )}
          </div>
        )}

        {/* 수동 분석: 파라미터 → 미리보기(한 에피소드, 저장 안 함) → 전체 재분석 */}
        {detail && defaultParams && (
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-3 text-xs space-y-2">
            <button
              onClick={() => setShowParams((s) => !s)}
              className="w-full text-left text-neutral-400 hover:text-white"
            >
              파라미터 {showParams ? '▴' : '▾'} <span className="text-neutral-600">수동 분석</span>
            </button>
            {showParams && (
              <>
                <div className="grid grid-cols-2 gap-x-3 gap-y-1">
                  {Object.keys(defaultParams).filter((k) => k !== 'fps').map((k) => (
                    <label key={k} className="flex items-center justify-between gap-1">
                      <span className="text-neutral-500 truncate" title={k}>{k}</span>
                      <input
                        type="number"
                        step="any"
                        value={Number.isFinite(paramValues[k]) ? paramValues[k] : ''}
                        onChange={(e) => {
                          const raw = e.target.value
                          setParamValues((v) => ({ ...v, [k]: raw === '' ? NaN : Number(raw) }))
                        }}
                        className="w-16 rounded bg-neutral-900 border border-neutral-700 px-1 py-0.5 text-right"
                      />
                    </label>
                  ))}
                </div>
                <div className="text-neutral-600">fps {detail.fps ?? 15} — 데이터셋 고정</div>
                <div className="flex gap-1 flex-wrap">
                  <button
                    onClick={() => void handlePreview()}
                    disabled={analyzing || ep == null}
                    title="선택한 에피소드만 이 파라미터로 재분석해 트랙에 겹쳐 본다 — 저장 안 함"
                    className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-amber-600 text-neutral-300 hover:text-white disabled:opacity-50"
                  >
                    미리보기
                  </button>
                  <button
                    onClick={() => void handleAnalyze()}
                    disabled={analyzing}
                    className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white disabled:opacity-50"
                  >
                    전체 재분석(저장)
                  </button>
                  <button
                    onClick={() => setParamValues({ ...defaultParams })}
                    className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-400 hover:text-white"
                  >
                    기본값
                  </button>
                </div>
              </>
            )}
          </div>
        )}

        {episodes.length > 0 && (
          <ul className="rounded-lg border border-neutral-700 bg-neutral-800 divide-y divide-neutral-700/60 max-h-[70vh] overflow-y-auto">
            {episodes.map((r) => (
              <li key={r.index} className={`flex items-center ${ep === r.index ? 'bg-neutral-700' : ''}`}>
                <input
                  type="checkbox"
                  checked={marked.includes(r.index)}
                  onChange={() => toggleMark(r.index)}
                  title="삭제·task 수정 대상으로 표시"
                  className="ml-2 accent-red-500 shrink-0"
                />
                <button
                  onClick={() => void selectEpisode(dsId, r.index)}
                  className="flex-1 min-w-0 text-left px-2 py-1.5 text-sm flex items-center gap-2 hover:bg-neutral-700/50"
                >
                  <span className="font-mono">#{r.index}</span>
                  <span className="text-xs text-neutral-500">{r.length}f</span>
                  {r.cycles != null && (
                    <span className="text-xs text-neutral-400">{r.cycles}사이클</span>
                  )}
                  <span className="ml-auto flex items-center gap-1">
                    {r.reasons.length > 0 && (
                      <span className="text-amber-400" title={r.reasons.join(', ')}>⚠</span>
                    )}
                    {r.reviewed && <span className="text-green-500">✔</span>}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}

        {/* 수명주기 (4단계): 재생으로 확인한 에피소드를 지우거나 task 를 옮긴다 */}
        {marked.length > 0 && (
          <div className="rounded-lg border border-red-500/30 bg-neutral-800 p-3 text-xs space-y-2">
            <div className="text-neutral-300">
              선택 {marked.length}개: <span className="font-mono">{marked.map((i) => `#${i}`).join(' ')}</span>
            </div>
            <div className="flex gap-1.5">
              <button
                onClick={() => void handleDeleteMarked()}
                disabled={lifecycleBusy}
                className="px-2 py-1 rounded bg-red-600/80 hover:bg-red-600 text-white disabled:opacity-50"
              >
                {lifecycleBusy ? '작업 중…' : '삭제'}
              </button>
              <button
                onClick={() => setMarked([])}
                disabled={lifecycleBusy}
                className="px-2 py-1 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300"
              >
                해제
              </button>
            </div>
            <div className="flex gap-1.5">
              <input
                type="text"
                value={taskInput}
                onChange={(e) => setTaskInput(e.target.value)}
                placeholder="새 task 문구…"
                className="flex-1 min-w-0 rounded bg-neutral-900 border border-neutral-700 px-2 py-1"
              />
              <button
                onClick={() => void handleTaskUpdate()}
                disabled={lifecycleBusy || !taskInput.trim()}
                className="px-2 py-1 rounded bg-blue-600/80 hover:bg-blue-600 text-white disabled:opacity-50"
              >
                task 변경
              </button>
            </div>
            <div className="text-neutral-500">
              삭제하면 뒤 번호가 당겨지고 페이즈 라벨·신호가 자동으로 따라옵니다.
            </div>
          </div>
        )}
      </aside>

      {/* 뷰어 */}
      <main className="flex-1 min-w-0 space-y-3">
        {ep == null ? (
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-8 text-center text-neutral-500 text-sm">
            {dsId ? '왼쪽에서 에피소드를 선택하세요' : '데이터셋을 선택하세요'}
            <div className="mt-2 text-xs">Space 재생 · ←→ 프레임 (Shift=10) · J/K 에피소드 이동</div>
          </div>
        ) : (
          <>
            <div className="flex justify-end">
              <LayoutToggle layout={layout} onChange={switchLayout} />
            </div>
            {/* 가로 배치는 **사진 | 시간축** 이다.
                오른쪽 칸에는 프레임으로 색인되는 것이 전부 들어간다 —
                진행바·페이즈 트랙·신호 그래프가 같은 x축을 쓴다. */}
            <div className={layout === 'row'
              ? 'grid grid-cols-1 2xl:grid-cols-2 gap-4 items-start' : 'space-y-3'}>
            <div className="space-y-3">
            {/* 카메라 — 동영상 또는 프레임 캐시. **항상 세로로 쌓는다.**

                배치 토글과 무관하다. 가로 배치에서도 사진 칸은 세로로 길고, 거기에
                카메라를 나란히 두면 폭을 반씩 나눠 갖고 **아래는 통째로 빈다.**
                쌓으면 각자 칸 폭을 다 쓴다 — 프레임을 뜯어보는 화면이라 그게 낫다. */}
            <div className="flex flex-col gap-3">
              {cams.map((cam) => (
                <figure key={cam} className="w-full max-w-[720px]">
                  {videoActive ? (
                    <video
                      ref={(el) => { videoRefs.current[cam] = el as VideoWithRVFC | null }}
                      src={videoUrl(cam)}
                      muted
                      playsInline
                      preload="auto"
                      className="w-full rounded border border-neutral-700 bg-black"
                      onError={(e) => {
                        // 3=DECODE, 4=SRC_NOT_SUPPORTED 만 진짜 재생 불가.
                        // 1(abort)·2(network)는 src 교체·프록시 순단으로도 떠서 폴백하면 오진.
                        const code = e.currentTarget.error?.code
                        if (code === 3 || code === 4) setVideoError(true)
                      }}
                      onLoadedMetadata={(e) => {
                        const m = videoMeta?.[cam]
                        if (m) e.currentTarget.currentTime = videoTime(m, frameRef.current)
                      }}
                    />
                  ) : (
                    <img
                      src={frameUrl(cam, frame)}
                      alt={cam}
                      className="w-full rounded border border-neutral-700 bg-black"
                      onError={() => setCacheMissing(true)}
                      onLoad={() => setCacheMissing(false)}
                    />
                  )}
                  <figcaption className="text-xs text-neutral-500 mt-1 flex items-center gap-2">
                    {cam}
                    {yoloTarget && (
                      <button onClick={() => void captureToYolo(cam)}
                        title={`이 장면을 ${yoloTarget} 검출 데이터셋으로 캡처`}
                        className="px-1.5 rounded bg-neutral-800 hover:bg-green-700 text-neutral-400 hover:text-white">
                        📸
                      </button>
                    )}
                  </figcaption>
                </figure>
              ))}
            </div>

            {videoError && viewMode === 'video' && (
              <div className="rounded border border-neutral-700 bg-neutral-800 p-2 text-xs text-neutral-400">
                브라우저가 이 비디오를 재생하지 못합니다 (코덱/픽셀 포맷) — 프레임 캐시로 전환됨
              </div>
            )}

            {!videoActive && cacheMissing && (
              <div className="rounded-lg border border-amber-700/60 bg-amber-950/40 p-3 text-sm flex items-center justify-between">
                <span>디코딩 캐시가 없습니다 — 생성해야 프레임이 보입니다 (JPEG·긴변 320px, 수 분 소요)</span>
                <button
                  onClick={() => void handleCreateCache()}
                  className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-amber-600 text-neutral-300 hover:text-white"
                >
                  캐시 생성
                </button>
              </div>
            )}

            {/* 컨트롤 */}
            <div className="flex items-center gap-3 text-sm">
              <button
                onClick={() => totalFrames > 0 && setPlaying((p) => !p)}
                className="px-3 py-1 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white"
              >
                {playing ? '⏸ 정지' : '▶ 재생'}
              </button>
              <span className="font-mono text-neutral-400">
                {frame + 1}/{totalFrames} · {(frame / fps).toFixed(1)}s
              </span>
              {currentSegment && phaseNames.length > 0 && (
                <span
                  className="px-2 py-0.5 rounded text-xs font-medium text-black"
                  style={{ backgroundColor: PHASE_COLORS[currentSegment[2]] }}
                >
                  {phaseNames[currentSegment[2]]}
                </span>
              )}
              {labeledEp?.reviewed && <span className="text-green-500 text-xs">✔ 검토됨</span>}
              {displaySegments && !draft && (
                <button
                  onClick={enterEdit}
                  className="px-2 py-0.5 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white"
                  title="타임라인 수작업 편집 — 분할·병합·페이즈 지정·경계 드래그"
                >
                  ✏ 편집
                </button>
              )}
              {yoloDatasets.length > 0 && (
                <label className="ml-auto text-xs text-neutral-500">캡처 →
                  <select value={yoloTarget} onChange={(e) => setYoloTarget(e.target.value)}
                    className="ml-1 rounded bg-neutral-900 border border-neutral-700 px-1.5 py-0.5 text-neutral-300">
                    {yoloDatasets.map((d) => <option key={d} value={d}>{d}</option>)}
                  </select>
                </label>
              )}
              {videoMeta && (
                <button
                  onClick={() => { setPlaying(false); setViewMode((m) => (m === 'video' ? 'frames' : 'video')) }}
                  className={`px-2 py-0.5 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-400 hover:text-white ${
                    yoloDatasets.length > 0 ? '' : 'ml-auto'}`}
                  title="동영상 = 캐시 없이 즉시 재생 / 프레임 캐시 = 프레임 단위 정밀"
                >
                  {videoActive ? '프레임 캐시로 보기' : '동영상으로 보기'}
                </button>
              )}
            </div>

            </div>

            {/* ⚠ 프레임으로 색인되는 것은 **전부 이쪽**이다 — 진행바·페이즈 트랙·
                신호 그래프가 같은 x축(프레임)을 공유하므로, 사진과 갈라놓으면
                가로 배치에서 재생헤드가 두 칸에 흩어진다. */}
            <div className="space-y-3">
            <input
              type="range"
              min={0}
              max={Math.max(0, totalFrames - 1)}
              value={frame}
              onChange={(e) => { setPlaying(false); goTo(Number(e.target.value)) }}
              className="w-full"
            />

            {/* 페이즈 트랙 — 열람: 구간 클릭 = 이동 / 편집: 선택·분할·병합·드래그 */}
            {shownSegments ? (
              <div>
                {preview && !draft && (
                  <div className="flex items-center gap-2 mb-1 text-xs text-amber-400">
                    <span>미리보기 — 저장 안 됨 · {preview.cycles}사이클. [전체 재분석(저장)]으로 확정</span>
                    <button
                      onClick={() => setPreview(null)}
                      className="px-1.5 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300"
                    >
                      해제
                    </button>
                  </div>
                )}
                <div
                  ref={trackRef}
                  className={`relative rounded overflow-hidden flex border ${
                    draft ? 'h-8 border-blue-500' : preview ? 'h-6 border-amber-500' : 'h-6 border-neutral-700'
                  }`}
                >
                  {shownSegments.map(([s, e, code], i) => (
                    <button
                      key={i}
                      title={`${phaseNames[code]} ${s}–${e}`}
                      onClick={() => {
                        setPlaying(false)
                        if (draft) setSelectedSeg(i)
                        goTo(s)
                      }}
                      className={draft && selectedSeg === i ? 'ring-2 ring-inset ring-white/90 z-[1]' : ''}
                      style={{
                        width: `${((e - s + 1) / totalFrames) * 100}%`,
                        backgroundColor: PHASE_COLORS[code],
                      }}
                    />
                  ))}
                  {/* 편집 모드: 경계 핸들 — 드래그하면 이웃 두 구간이 같이 늘고 준다 */}
                  {draft && draft.slice(0, -1).map((_, i) => (
                    <div
                      key={`b${i}`}
                      onPointerDown={(e) => startBoundaryDrag(i, e)}
                      className="absolute top-0 bottom-0 w-2 -ml-1 cursor-ew-resize z-10 group"
                      style={{ left: `${(draft[i + 1][0] / totalFrames) * 100}%` }}
                    >
                      <div className="mx-auto w-0.5 h-full bg-white/70 group-hover:bg-white" />
                    </div>
                  ))}
                  <div
                    className="absolute top-0 bottom-0 w-px bg-yellow-400 pointer-events-none"
                    style={{ left: `${(frame / Math.max(1, totalFrames - 1)) * 100}%` }}
                  />
                </div>

                {draft ? (
                  <>
                    <div className="flex items-center gap-1.5 mt-2 text-xs flex-wrap">
                      <span className="text-neutral-500">선택 구간 페이즈:</span>
                      {phaseNames.map((name, code) => (
                        <button
                          key={name}
                          disabled={selectedSeg == null}
                          onClick={() => changePhase(code)}
                          className="px-1.5 py-0.5 rounded text-black font-medium disabled:opacity-40"
                          style={{ backgroundColor: PHASE_COLORS[code] }}
                        >
                          {code} {name}
                        </button>
                      ))}
                    </div>
                    <div className="flex items-center gap-1.5 mt-1.5 text-xs flex-wrap">
                      <button
                        onClick={splitAtPlayhead}
                        title="재생헤드 위치에서 구간을 둘로 나눈다"
                        className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white"
                      >
                        분할 (S)
                      </button>
                      <button
                        onClick={mergeSelected}
                        disabled={selectedSeg == null || draft.length < 2}
                        title="선택 구간을 앞 구간에 흡수한다"
                        className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white disabled:opacity-40"
                      >
                        병합 (M)
                      </button>
                      <button
                        onClick={undoEdit}
                        disabled={undoStack.length === 0}
                        className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 hover:text-white disabled:opacity-40"
                      >
                        되돌리기 (Ctrl+Z)
                      </button>
                      <span className="flex-1" />
                      <button
                        onClick={() => void saveEdit()}
                        className="px-3 py-0.5 rounded bg-green-700 hover:bg-green-600 text-white"
                      >
                        저장 (검토됨 ✔)
                      </button>
                      <button
                        onClick={cancelEdit}
                        className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 hover:text-white"
                      >
                        취소
                      </button>
                    </div>
                    <div className="mt-1 text-[10px] text-neutral-600">
                      구간 클릭 = 선택 · 경계 세로선 드래그 = 이동 · 0~6 = 선택 구간 페이즈 지정
                    </div>
                  </>
                ) : (
                  <div className="flex gap-3 mt-1 text-[10px] text-neutral-500 flex-wrap">
                    {phaseNames.map((name, code) => (
                      <span key={name} className="flex items-center gap-1">
                        <span className="inline-block w-2 h-2 rounded-sm" style={{ backgroundColor: PHASE_COLORS[code] }} />
                        {name}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div className="rounded border border-neutral-700 bg-neutral-800 p-3 text-xs text-neutral-500">
                페이즈 분석이 없습니다 — [분석] 을 실행하면 구간 트랙과 신호 그래프가 보입니다
              </div>
            )}

            {/* 신호 그래프 — 재생헤드(markerX) 공유 */}
            {signals && (
              <div className="space-y-2">
                {/* ⚠ 라벨이 `deg/s` 였는데 **도가 아니다.** `observation.state` 는
                       ±100 정규화 값이라(실측 확인) 이 신호의 단위는 정규화 단위/초다.
                       임계값을 조정하려는 사람이 "20도/초"로 읽으면 어긋난다. */}
                <PlotlyChart
                  x={Array.from({ length: signals.frames }, (_, i) => i)}
                  series={[{ label: '관절 속도 (정규화 단위/s)', color: '#60a5fa', data: signals.speed }]}
                  markerX={frame}
                  height={160}
                  uirevision={`${dsId}/${ep}/speed`}
                />
                {/* 말단 속도 — 관절 속도와 **다른 것을 본다.** 관절 쪽은 어깨 1도와
                    손목 1도를 같게 세지만, 말단이 실제로 움직인 거리는 크게 다르다.
                    URDF(vendor/agx_arm_urdf)로 FK 해서 m/s 로 낸다. */}
                {signals.tip_speed && (
                  <PlotlyChart
                    x={Array.from({ length: signals.frames }, (_, i) => i)}
                    series={[{ label: '말단 속도 (m/s)', color: '#34d399', data: signals.tip_speed }]}
                    markerX={frame}
                    height={160}
                    uirevision={`${dsId}/${ep}/tip`}
                  />
                )}
                <PlotlyChart
                  x={Array.from({ length: signals.frames }, (_, i) => i)}
                  series={[{ label: '그리퍼 지령-실측 갭', color: '#f472b6', data: signals.gripper_gap }]}
                  markerX={frame}
                  height={160}
                  uirevision={`${dsId}/${ep}/gap`}
                />

                {/* 시작 자세로부터의 거리 — PARKING 이 **왜** 거기서 시작하는지가
                    이 선에서 보인다. 복귀 구간에서만 0 으로 수렴한다
                    (실측: 복귀 끝 최대 2.2cm, 긴 접근 끝 최소 13.5cm). */}
                {signals.home_dist && (
                  <PlotlyChart
                    x={Array.from({ length: signals.frames }, (_, i) => i)}
                    series={[{ label: '시작 자세로부터 거리 (m)', color: '#fbbf24', data: signals.home_dist }]}
                    markerX={frame}
                    height={160}
                    uirevision={`${dsId}/${ep}/home`}
                  />
                )}

                {/* 관절별 그래프 — 축마다 하나. 실측과 지령을 겹쳐 그린다:
                    ⚠ 실측만 보면 **추종 오차가 안 보인다.** 물체에 막혀 못
                    따라가는 구간이 두 선의 벌어짐으로 드러난다. */}
                {signals.joints && (
                  <div className="space-y-2">
                    <button
                      onClick={() => {
                        const next = !showJoints
                        setShowJoints(next)
                        localStorage.setItem('episodes-show-joints', next ? '1' : '0')
                      }}
                      className="text-xs text-neutral-400 hover:text-neutral-200"
                    >
                      {showJoints ? '▾' : '▸'} 관절별 그래프 ({signals.joints.names.length}축)
                    </button>
                    {showJoints && signals.joints.names.map((name, i) => (
                      <PlotlyChart
                        key={name}
                        x={Array.from({ length: signals.frames }, (_, k) => k)}
                        series={[
                          { label: `${name} 실측`, color: '#60a5fa', data: signals.joints!.state[i] },
                          { label: `${name} 지령`, color: '#f59e0b', data: signals.joints!.action[i] },
                        ]}
                        markerX={frame}
                        height={140}
                        uirevision={`${dsId}/${ep}/j${i}`}
                      />
                    ))}
                  </div>
                )}
              </div>
            )}
            </div>
            </div>
          </>
        )}
      </main>
    </div>
  )
}
