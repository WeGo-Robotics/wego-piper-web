/**
 * YOLO 학습 — 캡처→라벨→학습→가중치 루프의 작업대 (feature/yolo-training.md).
 *
 * 1단계: 캡처(라이브 세그먼트·에피소드 가져오기) + 갤러리.
 * 라벨·학습 탭은 2·3단계에서 붙는다.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../services/api'
import { useSystemMessage } from '../components/SystemMessages'
import YoloLabeler from '../components/YoloLabeler'

type YoloDs = { name: string; classes: string[]; images: number; labeled: number }
type ImgSource = {
  type: string; cam?: string; dataset?: string; episode?: number; frame?: number; t?: number
}
type ImgEntry = { file: string; labeled: boolean; source: ImgSource | null }
type LrDataset = { id: string; total_episodes: number; fps: number; features: Record<string, unknown> }

const shortId = (s: string) => (s.length > 24 ? `${s.slice(0, 6)}…${s.slice(-10)}` : s)

/** 출처 배지 텍스트 — 갤러리에서 "어디서 온 장면인가"를 한 눈에 */
const sourceBadge = (s: ImgSource | null) => {
  if (!s) return '?'
  if (s.type === 'live') return `live ${s.cam ? shortId(s.cam) : ''}`
  if (s.type === 'episode') {
    const at = s.frame != null ? `#${s.frame}` : s.t != null ? `${s.t.toFixed(1)}s` : ''
    return `ep${s.episode} ${s.cam ?? ''} ${at}`
  }
  return s.type
}

export default function YoloTrainPage() {
  const { notify } = useSystemMessage()
  const notifyError = (text: string) => notify({ level: 'error', text, source: 'YOLO 학습' })

  // ── 데이터셋 ──
  const [datasets, setDatasets] = useState<YoloDs[]>([])
  const [current, setCurrent] = useState('')
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newClasses, setNewClasses] = useState('')

  const refreshDatasets = useCallback(async (pick?: string) => {
    try {
      const r = await api.get<{ datasets: YoloDs[] }>('/yolo/datasets')
      setDatasets(r.datasets)
      setCurrent((cur) => pick ?? (r.datasets.some((d) => d.name === cur) ? cur : (r.datasets[0]?.name ?? '')))
    } catch { /* 다음 기회에 */ }
  }, [])

  useEffect(() => { void refreshDatasets() }, [refreshDatasets])

  const handleCreate = async () => {
    const classes = newClasses.split(',').map((s) => s.trim()).filter(Boolean)
    try {
      await api.post('/yolo/datasets', { name: newName.trim(), classes })
      setCreating(false); setNewName(''); setNewClasses('')
      await refreshDatasets(newName.trim())
    } catch (e) { notifyError(e instanceof Error ? e.message : '생성 실패') }
  }

  const handleDeleteDataset = async () => {
    try {
      await api.delete(`/yolo/datasets/${current}`)
      await refreshDatasets()
    } catch (e) { notifyError(e instanceof Error ? e.message : '삭제 실패') }
  }

  // ── 탭 ──
  const [tab, setTab] = useState<'capture' | 'label' | 'gallery'>('capture')

  // ── 갤러리 ──
  const [images, setImages] = useState<ImgEntry[]>([])
  const refreshImages = useCallback(async () => {
    if (!current) { setImages([]); return }
    try {
      const r = await api.get<{ images: ImgEntry[] }>(`/yolo/datasets/${current}/images`)
      setImages(r.images)
    } catch { setImages([]) }
  }, [current])

  useEffect(() => { void refreshImages() }, [refreshImages, tab])

  // ── 라이브 캡처 ──
  const [segments, setSegments] = useState<string[]>([])
  const [tick, setTick] = useState(0)
  const [auto, setAuto] = useState<Set<string>>(new Set())
  const [intervalS, setIntervalS] = useState(5)
  const [captured, setCaptured] = useState<Record<string, number>>({}) // 세션 캡처 수

  useEffect(() => {
    if (tab !== 'capture') return
    api.get<{ segments: string[] }>('/vision/segments').then((r) => setSegments(r.segments)).catch(() => {})
    const t = setInterval(() => setTick((x) => x + 1), 1000)
    return () => clearInterval(t)
  }, [tab])

  const captureLive = useCallback(async (cam: string) => {
    if (!current) { notifyError('먼저 데이터셋을 만드세요'); return }
    try {
      await api.post(`/yolo/datasets/${current}/capture`, { cam })
      setCaptured((c) => ({ ...c, [cam]: (c[cam] ?? 0) + 1 }))
    } catch (e) { notifyError(e instanceof Error ? e.message : '캡처 실패') }
    // eslint 상: notifyError 는 안정적이지 않지만 여기선 무해
  }, [current])  // eslint-disable-line react-hooks/exhaustive-deps

  // 자동 캡처 — 체크된 세그먼트를 intervalS 마다
  useEffect(() => {
    if (tab !== 'capture' || auto.size === 0) return
    const t = setInterval(() => { auto.forEach((cam) => void captureLive(cam)) }, intervalS * 1000)
    return () => clearInterval(t)
  }, [tab, auto, intervalS, captureLive])

  // ── 에피소드 가져오기 ──
  const [lrList, setLrList] = useState<LrDataset[]>([])
  const [lrId, setLrId] = useState('')
  const [episode, setEpisode] = useState(0)
  const [cam, setCam] = useState('')
  const [stride, setStride] = useState(30)
  const [importing, setImporting] = useState(false)
  const [frameIdx, setFrameIdx] = useState(0)
  const [frameOk, setFrameOk] = useState(true)

  useEffect(() => {
    if (tab !== 'capture') return
    api.get<LrDataset[]>('/datasets').then((list) => {
      setLrList(list)
      if (!lrId && list.length > 0) setLrId(list[0].id)
    }).catch(() => {})
  }, [tab])  // eslint-disable-line react-hooks/exhaustive-deps

  const lrDs = lrList.find((d) => d.id === lrId)
  const lrCams = useMemo(() =>
    Object.keys(lrDs?.features ?? {})
      .filter((k) => k.startsWith('observation.images.'))
      .map((k) => k.replace('observation.images.', '')),
    [lrDs])

  useEffect(() => {
    if (lrCams.length > 0 && !lrCams.includes(cam)) setCam(lrCams[0])
  }, [lrCams, cam])

  const framePreview = lrId && cam
    ? `/api/datasets/${encodeURIComponent(lrId)}/episodes/${episode}/frames/${cam}/${frameIdx}`
    : null

  const importEpisode = async (indices?: number[]) => {
    if (!current) { notifyError('먼저 데이터셋을 만드세요'); return }
    setImporting(true)
    try {
      const r = await api.post<{ added: number; total_frames: number }>(
        `/yolo/datasets/${current}/import-episode`,
        { dataset_id: lrId, episode, cam, stride, indices })
      notify({
        level: 'info', source: 'YOLO 학습',
        text: indices ? `프레임 ${indices[0]} 캡처` : `${r.added}장 가져옴 (전체 ${r.total_frames}프레임, ${stride}간격)`,
      })
      await refreshDatasets(current)
    } catch (e) { notifyError(e instanceof Error ? e.message : '가져오기 실패') }
    finally { setImporting(false) }
  }

  // ── 라벨 탭 ──
  // 파일 목록은 탭 진입·필터 변경 때 **스냅샷**한다 — 라벨을 저장할 때마다
  // "미라벨만" 목록이 줄어들며 인덱스가 튀는 걸 막는다.
  const [labelFilter, setLabelFilter] = useState<'all' | 'unlabeled'>('all')
  const [labelFiles, setLabelFiles] = useState<string[]>([])
  const [labelIdx, setLabelIdx] = useState(0)

  const snapshotLabelFiles = useCallback((filter: 'all' | 'unlabeled', startFile?: string) => {
    const files = images.filter((i) => filter === 'all' || !i.labeled).map((i) => i.file)
    setLabelFiles(files)
    const at = startFile ? files.indexOf(startFile) : -1
    setLabelIdx(at >= 0 ? at : 0)
  }, [images])

  const openLabeler = (startFile?: string) => {
    // 특정 이미지에서 열 때는 필터를 전체로 — 라벨된 이미지도 다시 볼 수 있어야 한다
    const filter = startFile ? 'all' : labelFilter
    setLabelFilter(filter)
    snapshotLabelFiles(filter, startFile)
    setTab('label')
  }

  const onLabelSaved = (file: string, labeled: boolean) => {
    setImages((imgs) => imgs.map((i) => (i.file === file ? { ...i, labeled } : i)))
  }

  // ── 사전 라벨 ──
  const [plModel, setPlModel] = useState('yolo11n.pt')
  const [plModels, setPlModels] = useState<string[]>([])
  const [plConf, setPlConf] = useState(0.25)
  const [plBusy, setPlBusy] = useState(false)

  useEffect(() => {
    if (tab !== 'label') return
    api.get<{ models: { file: string }[] }>('/vision/models')
      .then((r) => setPlModels(r.models.map((m) => m.file)))
      .catch(() => {})
  }, [tab])

  const runPrelabel = async () => {
    setPlBusy(true)
    try {
      const r = await api.post<{ labeled: number; boxes: number; no_match: number; targets: number }>(
        `/yolo/datasets/${current}/prelabel`, { model: plModel, conf: plConf })
      notify({
        level: 'info', source: 'YOLO 학습',
        text: `사전 라벨: ${r.targets}장 중 ${r.labeled}장에 박스 ${r.boxes}개` +
          (r.no_match > 0 ? ` (이름 불일치로 버린 박스 ${r.no_match}개)` : ''),
      })
      await refreshImages()
      snapshotLabelFiles(labelFilter)
    } catch (e) { notifyError(e instanceof Error ? e.message : '사전 라벨 실패') }
    finally { setPlBusy(false) }
  }

  const deleteImage = async (file: string) => {
    try {
      await api.delete(`/yolo/datasets/${current}/images/${file}`)
      setImages((imgs) => imgs.filter((i) => i.file !== file))
    } catch (e) { notifyError(e instanceof Error ? e.message : '삭제 실패') }
  }

  const ds = datasets.find((d) => d.name === current)

  return (
    <div className="space-y-4">
      {/* ── 데이터셋 선택/생성 ── */}
      <header className="flex items-center gap-3 flex-wrap">
        <h1 className="text-xl font-bold tracking-tight">YOLO 학습</h1>
        <select value={current} onChange={(e) => setCurrent(e.target.value)}
          className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1 text-sm">
          {datasets.length === 0 && <option value="">데이터셋 없음</option>}
          {datasets.map((d) => (
            <option key={d.name} value={d.name}>{d.name} ({d.labeled}/{d.images})</option>
          ))}
        </select>
        <button onClick={() => setCreating((c) => !c)}
          className="px-2 py-1 text-sm rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-300">
          ＋ 새 데이터셋
        </button>
        {ds && (
          <>
            <span className="text-xs text-neutral-500">
              클래스: {ds.classes.join(', ')}
            </span>
            <button onClick={() => void handleDeleteDataset()}
              className="text-sm text-neutral-600 hover:text-red-400">삭제</button>
          </>
        )}
      </header>

      {creating && (
        <div className="flex items-center gap-2 flex-wrap rounded-lg border border-neutral-700 bg-neutral-800 p-3 text-sm">
          <input value={newName} onChange={(e) => setNewName(e.target.value)}
            placeholder="이름 (영숫자/_/-)"
            className="w-44 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
          <input value={newClasses} onChange={(e) => setNewClasses(e.target.value)}
            placeholder="클래스 (콤마 구분: pet_bottle, can, ...)"
            className="flex-1 min-w-60 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
          <button onClick={() => void handleCreate()}
            className="px-3 py-1 rounded bg-green-700 hover:bg-green-600 text-white">생성</button>
        </div>
      )}

      {/* ── 탭 ── */}
      <div className="flex gap-1 border-b border-neutral-700 text-sm">
        {([
          ['capture', '캡처'],
          ['label', `라벨 (${images.filter((i) => i.labeled).length}/${images.length})`],
          ['gallery', `갤러리 (${images.length})`],
        ] as const).map(([k, label]) => (
          <button key={k}
            onClick={() => { if (k === 'label') openLabeler(); else setTab(k) }}
            className={`px-4 py-2 -mb-px border-b-2 ${
              tab === k ? 'border-blue-500 text-white' : 'border-transparent text-neutral-500 hover:text-neutral-300'}`}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'capture' && (
        <div className="space-y-4">
          {/* ── 라이브 ── */}
          <section className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
            <div className="flex items-center gap-3">
              <h2 className="font-semibold">라이브 캡처</h2>
              <label className="text-xs text-neutral-400 ml-auto">자동 간격
                <select value={intervalS} onChange={(e) => setIntervalS(Number(e.target.value))}
                  className="ml-1 rounded bg-neutral-900 border border-neutral-700 px-1 py-0.5">
                  {[2, 5, 10, 30].map((v) => <option key={v} value={v}>{v}초</option>)}
                </select>
              </label>
            </div>
            {segments.length === 0 ? (
              <div className="text-sm text-neutral-500">살아 있는 카메라 세그먼트가 없습니다</div>
            ) : (
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                {segments.map((s) => (
                  <div key={s} className="rounded-lg overflow-hidden border border-neutral-700">
                    <img src={`/api/vision/segments/${encodeURIComponent(s)}/snapshot?t=${tick}`}
                      alt={s} className="w-full aspect-video object-cover bg-black"
                      onError={(e) => { e.currentTarget.style.opacity = '0.25' }}
                      onLoad={(e) => { e.currentTarget.style.opacity = '1' }} />
                    <div className="flex items-center gap-2 px-2 py-1.5 bg-neutral-900 text-xs">
                      <span className="font-mono text-neutral-400 truncate flex-1">{shortId(s)}</span>
                      {captured[s] != null && <span className="text-green-400">+{captured[s]}</span>}
                      <label className="flex items-center gap-1 text-neutral-500">
                        <input type="checkbox" checked={auto.has(s)}
                          onChange={(e) => {
                            const next = new Set(auto)
                            if (e.target.checked) next.add(s); else next.delete(s)
                            setAuto(next)
                          }} />자동
                      </label>
                      <button onClick={() => void captureLive(s)}
                        className="px-2 py-0.5 rounded bg-neutral-700 hover:bg-green-600 text-neutral-200">
                        📸 캡처
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* ── 에피소드 가져오기 ── */}
          <section className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
            <h2 className="font-semibold">에피소드에서 가져오기</h2>
            <div className="flex items-center gap-3 flex-wrap text-sm">
              <select value={lrId} onChange={(e) => { setLrId(e.target.value); setEpisode(0); setFrameIdx(0) }}
                className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1 max-w-72">
                {lrList.map((d) => <option key={d.id} value={d.id}>{d.id} ({d.total_episodes}ep)</option>)}
              </select>
              <label className="text-neutral-400">ep
                <input type="number" min={0} max={(lrDs?.total_episodes ?? 1) - 1} value={episode}
                  onChange={(e) => { setEpisode(Number(e.target.value)); setFrameIdx(0) }}
                  className="ml-1 w-16 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
              </label>
              <select value={cam} onChange={(e) => setCam(e.target.value)}
                className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1">
                {lrCams.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <label className="text-neutral-400">간격
                <input type="number" min={1} value={stride}
                  onChange={(e) => setStride(Number(e.target.value))}
                  className="ml-1 w-16 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
                <span className="ml-1 text-xs text-neutral-600">프레임 ({lrDs?.fps ?? 30}fps)</span>
              </label>
              <button onClick={() => void importEpisode()} disabled={importing || !lrId}
                className="px-3 py-1 rounded bg-blue-700 hover:bg-blue-600 text-white disabled:opacity-50">
                {importing ? '가져오는 중…' : '일괄 가져오기'}
              </button>
            </div>

            {/* 프레임 미리보기 + 낱장 캡처 (디코딩 캐시) */}
            {framePreview && (
              <div className="flex items-start gap-3 flex-wrap">
                <div className="w-80 max-w-full">
                  <img src={framePreview} alt="frame"
                    className="w-full rounded border border-neutral-700 bg-black"
                    onError={() => setFrameOk(false)} onLoad={() => setFrameOk(true)} />
                  {!frameOk && (
                    <div className="text-xs text-amber-400 mt-1">
                      디코딩 캐시가 없습니다 — 에피소드 페이지에서 decode-cache 를 먼저 생성하세요
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2 text-sm">
                  <label className="text-neutral-400">프레임
                    <input type="number" min={0} value={frameIdx}
                      onChange={(e) => setFrameIdx(Number(e.target.value))}
                      className="ml-1 w-20 rounded bg-neutral-900 border border-neutral-700 px-2 py-1" />
                  </label>
                  <button onClick={() => void importEpisode([frameIdx])} disabled={importing || !frameOk}
                    className="px-3 py-1 rounded bg-neutral-700 hover:bg-green-600 text-neutral-200 disabled:opacity-50">
                    📸 이 프레임 캡처
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
      )}

      {tab === 'label' && ds && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap text-sm">
            <select value={labelFilter}
              onChange={(e) => {
                const f = e.target.value as 'all' | 'unlabeled'
                setLabelFilter(f)
                snapshotLabelFiles(f)
              }}
              className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1">
              <option value="all">전체</option>
              <option value="unlabeled">미라벨만</option>
            </select>
            <div className="flex items-center gap-2 ml-auto text-xs text-neutral-400">
              사전 라벨:
              <select value={plModel} onChange={(e) => setPlModel(e.target.value)}
                className="rounded bg-neutral-900 border border-neutral-700 px-2 py-1">
                {!plModels.includes(plModel) && <option value={plModel}>{plModel}</option>}
                {plModels.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
              <label>conf
                <input type="number" step="0.05" min="0" max="1" value={plConf}
                  onChange={(e) => setPlConf(Number(e.target.value))}
                  className="ml-1 w-16 rounded bg-neutral-900 border border-neutral-700 px-1.5 py-1" />
              </label>
              <button onClick={() => void runPrelabel()} disabled={plBusy}
                title="미라벨 이미지 전체를 모델로 훑어 초안 박스를 채운다 — 모델 클래스 이름이 데이터셋 클래스와 일치할 때만"
                className="px-2.5 py-1 rounded bg-blue-800 hover:bg-blue-700 text-white disabled:opacity-50">
                {plBusy ? '실행 중… (모델 로드 수 초)' : '미라벨 일괄 사전 라벨'}
              </button>
            </div>
          </div>

          <YoloLabeler dataset={current} classes={ds.classes} files={labelFiles} index={labelIdx}
            onNavigate={setLabelIdx} onSaved={onLabelSaved} onError={notifyError} />

          {/* 필름스트립 */}
          {labelFiles.length > 0 && (
            <div className="flex gap-1.5 overflow-x-auto pb-1">
              {labelFiles.map((f, i) => {
                const labeled = images.find((im) => im.file === f)?.labeled
                return (
                  <button key={f} onClick={() => setLabelIdx(i)}
                    className={`relative shrink-0 w-24 rounded overflow-hidden border ${
                      i === labelIdx ? 'border-blue-500' : 'border-neutral-700 opacity-60 hover:opacity-100'}`}>
                    <img src={`/api/yolo/datasets/${current}/images/${f}`} alt=""
                      loading="lazy" className="w-full aspect-video object-cover bg-black" />
                    {labeled && <span className="absolute top-0.5 left-0.5 w-2 h-2 rounded-full bg-green-500" />}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      )}

      {tab === 'gallery' && (
        <section className="space-y-3">
          <div className="text-sm text-neutral-400">
            {images.length}장 · 라벨 {images.filter((i) => i.labeled).length}장
            <button onClick={() => void refreshImages()}
              className="ml-3 text-neutral-500 hover:text-white">새로고침</button>
          </div>
          {images.length === 0 ? (
            <div className="text-sm text-neutral-500 py-10 text-center">
              이미지가 없습니다 — 캡처 탭에서 모으세요
            </div>
          ) : (
            <div className="grid grid-cols-3 md:grid-cols-5 lg:grid-cols-6 gap-2">
              {images.map((img) => (
                <div key={img.file} className="relative group rounded overflow-hidden border border-neutral-700">
                  <img src={`/api/yolo/datasets/${current}/images/${img.file}`} alt=""
                    loading="lazy" onClick={() => openLabeler(img.file)}
                    className="w-full aspect-video object-cover bg-black cursor-pointer"
                    title="클릭해서 라벨 편집" />
                  <div className="absolute bottom-0 inset-x-0 px-1.5 py-0.5 bg-black/60 text-[10px] text-neutral-300 truncate">
                    {sourceBadge(img.source)}
                  </div>
                  {img.labeled && (
                    <span className="absolute top-1 left-1 w-2 h-2 rounded-full bg-green-500" title="라벨됨" />
                  )}
                  <button onClick={() => void deleteImage(img.file)}
                    className="absolute top-1 right-1 w-5 h-5 rounded bg-black/60 text-neutral-400 hover:text-red-400 opacity-0 group-hover:opacity-100 text-xs">
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  )
}
