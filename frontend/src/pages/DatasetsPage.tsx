import { useEffect, useState, useCallback } from 'react'
import NotesEditor from '../components/NotesEditor'
import { useSystemMessage } from '../components/SystemMessages'
import { api } from '../services/api'
import { copyText } from '../services/clipboard'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import type { Dataset, DatasetDetail } from '../types/models'
import DiskUsageBar from '../components/DiskUsageBar'
import HubBrowser from '../components/HubBrowser'
import LogViewer from '../components/LogViewer'

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

type SortKey = 'name' | 'size' | 'date' | 'episodes'

export default function DatasetsPage({ embedded = false, tab: tabProp, refreshKey }: { embedded?: boolean; tab?: 'local' | 'hub'; refreshKey?: number }) {
  // ⚠ `window.alert` 를 쓰지 않는다 — 이벤트 루프를 막아 E-stop heartbeat 가
  //   끊기고, 2초 타임아웃에 추론이 강제 종료된다 (confirm 으로 실제로 겪었다).
  const { notify, confirm: askConfirm } = useSystemMessage()
  const notifyError = (text: string) =>
    notify({ level: 'error', text, source: '데이터셋' })
  const [ownTab, setTab] = useState<'local' | 'hub'>('local')
  // ⚠ **묶여 있을 때는 부모가 정한다.** `로컬|Hub` 는 모델·데이터셋 두 탭이
  //   공유하는 선택이라, 각 페이지가 따로 들고 있으면 탭을 옮길 때마다 되돌아간다.
  const tab = tabProp ?? ownTab

  // 부모(저장소 화면)의 "새로고침" 을 받는다
  useEffect(() => { if (refreshKey !== undefined) fetchDatasets() }, [refreshKey])
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<DatasetDetail | null>(null)
  // 끊긴 녹화가 남긴 반쪽 상태. `null` 은 "아직 안 봤다".
  const [health, setHealth] = useState<Consistency | null>(null)
  const [repairing, setRepairing] = useState(false)
  const [selectedEpisodes, setSelectedEpisodes] = useState<number[]>([])
  const [editingTask, setEditingTask] = useState<string | null>(null)  // 선택된 에피소드에 적용할 task
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('date')
  const [sortAsc, setSortAsc] = useState(false)
  // Hub 업로드
  const [uploadId, setUploadId] = useState<string | null>(null)
  const [uploadLogs, setUploadLogs] = useState<string[]>([])
  const [uploadState, setUploadState] = useState<string>('idle')
  // hf-cli 설정
  const [hfCliPath, setHfCliPath] = useState('')
  const [hfCliResolved, setHfCliResolved] = useState('')

  useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
      if (msg.type === 'upload_log') setUploadLogs((prev) => [...prev.slice(-200), msg.data as string])
      else if (msg.type === 'upload_state') {
        setUploadState(msg.data as string)
        if (msg.data === 'idle' || msg.data === 'error') fetchDatasets()
      }
    }, []),
  })

  // hf-cli 경로 로드
  useEffect(() => {
    api.get<{ configured: string; resolved: string }>('/datasets/hf-cli')
      .then((r) => { setHfCliPath(r.configured); setHfCliResolved(r.resolved) })
      .catch(() => {})
  }, [])

  const handleSaveHfCli = async () => {
    try {
      const r = await api.post<{ resolved: string }>('/datasets/hf-cli', { path: hfCliPath })
      setHfCliResolved(r.resolved)
    } catch { notifyError('경로 저장 실패') }
  }

  const handleUpload = async (id: string) => {
    if (!await askConfirm(`"${id}"를 HuggingFace Hub에 업로드하시겠습니까?`)) return
    setUploadId(id)
    setUploadLogs([])
    setUploadState('starting')
    try {
      await api.post(`/datasets/${id}/upload`, { private: false })
    } catch (e) {
      setUploadLogs((prev) => [...prev, `[ERROR] ${e instanceof Error ? e.message : '업로드 실패'}`])
      setUploadState('error')
    }
  }

  const fetchDatasets = () => {
    setLoading(true)
    api
      .get<Dataset[]>('/datasets')
      .then(setDatasets)
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchDatasets()
  }, [])

  type Consistency = {
    ok: boolean
    declared_episodes: number
    indexed_episodes: number
    stored_episodes: number
    recoverable: number[]
    unrecoverable: number
    /** 되살릴 수 없는 손상 — 파일 자체가 미완성인 경우. */
    broken: string[]
    orphan_tmp_dirs: string[]
  }

  /** 색인 복구. **미리 보여주고 확인받은 뒤에** 쓴다 — 남의 데이터를 고치는 일이다. */
  const handleRepair = async (id: string) => {
    const n = health?.recoverable.length ?? 0
    if (!await askConfirm(
      `에피소드 ${n}개의 색인을 다시 만듭니다. 프레임은 그대로 두고 목록만 채웁니다.\n`
      + '원본 색인은 .bak 으로 남습니다.')) return
    setRepairing(true)
    try {
      await api.post(`/datasets/${id}/repair-index?apply=true`)
      await handleSelect(id)
    } catch (e) {
      notifyError(e instanceof Error ? e.message : '색인 복구 실패')
    } finally { setRepairing(false) }
  }

  const handleSelect = async (id: string) => {
    try {
      const d = await api.get<DatasetDetail>(`/datasets/${id}`)
      setDetail(d)
      setSelectedEpisodes([])
      // ⚠ 정합성을 **같이** 본다. 이것 없이 목록만 보여주면 "개수는 50인데
      //   목록에 2개" 인 상태가 조용히 지나가고, 사용자는 데이터가 날아간 줄
      //   알고 지운다 — 실제로 그렇게 지워졌다.
      setHealth(await api.get<Consistency>(`/datasets/${id}/consistency`).catch(() => null))
    } catch {
      // ignore
    }
  }

  const handleDelete = async (id: string) => {
    if (!await askConfirm(`"${id}" 데이터셋을 삭제하시겠습니까?`)) return
    await api.delete(`/datasets/${id}`)
    fetchDatasets()
    if (detail?.id === id) setDetail(null)
  }

  const handleDeleteEpisodes = async () => {
    if (!detail || selectedEpisodes.length === 0) return
    if (!await askConfirm(`${selectedEpisodes.length}개 에피소드를 삭제하시겠습니까?`))
      return
    await api.post(`/datasets/${detail.id}/edit`, {
      operation: 'delete_episodes',
      params: { episode_indices: JSON.stringify(selectedEpisodes) },
    })
    handleSelect(detail.id)
  }

  const handleUpdateTask = async () => {
    if (!detail || selectedEpisodes.length === 0 || !editingTask?.trim()) return
    try {
      await api.post(`/datasets/${detail.id}/update-task`, {
        episode_indices: selectedEpisodes,
        task: editingTask.trim(),
      })
      setEditingTask(null)
      setSelectedEpisodes([])
      handleSelect(detail.id)
    } catch { notifyError('Task 변경 실패') }
  }

  const toggleEpisode = (idx: number) => {
    setSelectedEpisodes((prev) =>
      prev.includes(idx) ? prev.filter((i) => i !== idx) : [...prev, idx],
    )
  }

  const filteredDatasets = datasets
    .filter((d) => !search || d.id.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => {
      let cmp = 0
      if (sortKey === 'name') cmp = a.id.localeCompare(b.id)
      else if (sortKey === 'size') cmp = a.size_bytes - b.size_bytes
      else if (sortKey === 'date') cmp = new Date(a.modified).getTime() - new Date(b.modified).getTime()
      else if (sortKey === 'episodes') cmp = a.total_episodes - b.total_episodes
      return sortAsc ? cmp : -cmp
    })

  const handleSort = (key: SortKey) => {
    if (sortKey === key) setSortAsc(!sortAsc)
    else { setSortKey(key); setSortAsc(false) }
  }

  return (
    <div className="space-y-6">
      {!embedded && (
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold">데이터셋</h1>
          <div className="flex gap-1">
            <button onClick={() => setTab('local')}
              className={`px-3 py-1.5 rounded text-sm ${tab === 'local' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}>
              로컬
            </button>
            <button onClick={() => setTab('hub')}
              className={`px-3 py-1.5 rounded text-sm ${tab === 'hub' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}>
              Hub
            </button>
            <button onClick={fetchDatasets} disabled={loading}
              className="px-3 py-1.5 rounded text-sm text-neutral-400 hover:text-white hover:bg-neutral-700 disabled:opacity-50">
              {loading ? '스캔 중...' : '새로고침'}
            </button>
          </div>
        </div>
      )}

      {!embedded && <DiskUsageBar />}

      {/* hf-cli 설정 */}
      {tab === 'local' && (
        <details className="text-xs">
          <summary className="text-neutral-500 cursor-pointer hover:text-neutral-300">huggingface-cli 설정</summary>
          <div className="mt-2 flex items-center gap-2">
            <input type="text" value={hfCliPath} onChange={(e) => setHfCliPath(e.target.value)}
              placeholder={hfCliResolved || '자동 탐색'}
              className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-neutral-100 text-xs placeholder:text-neutral-500" />
            <button onClick={handleSaveHfCli}
              className="px-3 py-1 rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white text-xs">저장</button>
            {hfCliResolved && <span className="text-neutral-500 truncate max-w-[300px]" title={hfCliResolved}>{hfCliResolved}</span>}
          </div>
        </details>
      )}

      {tab === 'hub' ? <HubBrowser type="datasets" /> : null}

      {tab === 'local' && loading ? (
        <p className="text-neutral-400">스캔 중...</p>
      ) : tab === 'local' && datasets.length === 0 ? (
        <p className="text-neutral-400">
          로컬 데이터셋이 없습니다. 데이터 수집을 시작하거나 Hub에서
          다운로드하세요.
        </p>
      ) : tab === 'local' ? (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* 데이터셋 목록 */}
          <div className="lg:col-span-1 space-y-2">
            <input type="text" value={search} onChange={(e) => setSearch(e.target.value)}
              placeholder="검색 (이름)..."
              className="w-full px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 placeholder:text-neutral-500" />
            <div className="flex gap-1 text-[10px]">
              {([['name', '이름'], ['episodes', '에피소드'], ['size', '크기'], ['date', '날짜']] as [SortKey, string][]).map(([k, l]) => (
                <button key={k} onClick={() => handleSort(k)}
                  className={`px-2 py-0.5 rounded ${sortKey === k ? 'bg-blue-600 text-white' : 'bg-neutral-700 text-neutral-400'}`}>
                  {l}{sortKey === k ? (sortAsc ? ' ↑' : ' ↓') : ''}
                </button>
              ))}
              <span className="ml-auto text-neutral-500">{filteredDatasets.length}/{datasets.length}</span>
            </div>
            {filteredDatasets.map((ds) => (
              <div
                key={ds.id}
                onClick={() => handleSelect(ds.id)}
                className={`rounded-lg border p-4 cursor-pointer transition-colors ${
                  detail?.id === ds.id
                    ? 'border-blue-500 bg-blue-500/10'
                    : 'border-neutral-700 bg-neutral-800 hover:border-neutral-600'
                }`}
              >
                <p className="font-medium truncate">
                  {ds.notes?.name
                    ? <>{ds.notes.name} <span className="text-neutral-500 font-normal">({ds.id})</span></>
                    : ds.id}
                </p>
                {ds.notes?.description && (
                  <p className="truncate text-[11px] text-neutral-500">{ds.notes.description}</p>
                )}
                <p className="text-xs text-neutral-400 mt-1">
                  {ds.total_episodes} 에피소드 · {formatBytes(ds.size_bytes)}
                </p>
                <div className="flex gap-1 mt-2">
                  <button
                    onClick={(e) => { e.stopPropagation(); handleUpload(ds.id) }}
                    className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white">
                    Hub 업로드
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleDelete(ds.id) }}
                    className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white">
                    삭제
                  </button>
                </div>
              </div>
            ))}
          </div>

          {/* 상세 패널 */}
          <div className="lg:col-span-2">
            {detail ? (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-6 space-y-4">
                <div className="flex items-center justify-between">
                  <h2 className="text-lg font-semibold">{detail.id}</h2>
                  <div className="flex gap-1">
                    <button onClick={() => { copyText((detail as Record<string, unknown>).path as string ?? detail.id) }}
                      className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300 hover:text-white">
                      경로 복사
                    </button>
                    <button onClick={async () => {
                      if (!await askConfirm('영상을 프레임별 JPG로 디코딩합니다. 학습 속도가 향상됩니다.')) return
                      setUploadId(detail.id); setUploadLogs([]); setUploadState('starting')
                      api.post(`/datasets/${detail.id}/decode-cache`, {}).catch((e) => {
                        setUploadLogs(prev => [...prev, `[ERROR] ${e instanceof Error ? e.message : '실패'}`])
                        setUploadState('error')
                      })
                    }}
                      className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-green-600 text-neutral-300 hover:text-white">
                      디코딩 캐시
                    </button>
                    <button onClick={async () => {
                      if (!await askConfirm('디코딩 캐시(프레임 JPG)를 삭제합니다.')) return
                      const r = await api.post<{ deleted_files: number }>(`/datasets/${detail.id}/decode-cache/delete`, {})
                      notify({ level: 'info', text: `${r.deleted_files}개 파일 삭제됨`, source: '데이터셋' })
                      handleSelect(detail.id)
                    }}
                      className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-orange-600 text-neutral-300 hover:text-white">
                      캐시 삭제
                    </button>
                    <button onClick={() => handleUpload(detail.id)}
                      className="px-2 py-1 text-xs rounded bg-neutral-700 hover:bg-blue-600 text-neutral-300 hover:text-white">
                      Hub 업로드
                    </button>
                  </div>
                </div>
                {/* 이름·설명 — LeRobot 에 없는 자리라 사이드카로. key 로 대상 전환 시
                    초안이 초기화된다 (다른 데이터셋에 눌러붙는 사고 방지). */}
                <NotesEditor key={detail.id} endpoint={`/datasets/${detail.id}/notes`} source="데이터셋" />
                <div className="grid grid-cols-3 gap-3 text-sm">
                  <div>
                    <span className="text-neutral-400">에피소드</span>
                    <p>{detail.total_episodes}</p>
                  </div>

                  <div>
                    <span className="text-neutral-400">프레임</span>
                    <p>{detail.total_frames.toLocaleString()}</p>
                  </div>
                  <div>
                    <span className="text-neutral-400">FPS</span>
                    <p>{detail.fps ?? '-'}</p>
                  </div>
                </div>

                {/* ⚠ 끊긴 녹화 경고. **목록보다 위에** 둔다 — 목록이 짧은 이유를
                       모른 채 스크롤하면 데이터가 없어진 줄 안다. */}
                {health && !health.ok && (
                  <div className="rounded border border-amber-600/60 bg-amber-950/30 p-3 text-sm space-y-2">
                    <p className="font-semibold text-amber-300">
                      에피소드 색인이 끊겼습니다 — 녹화가 정상 종료되지 않았습니다
                    </p>
                    <p className="text-neutral-300">
                      개수는 <b>{health.declared_episodes}</b>인데 목록에는{' '}
                      <b>{health.indexed_episodes}</b>개만 있습니다.
                      프레임은 <b>{health.stored_episodes}</b>개 남아 있습니다.
                    </p>
                    {health.recoverable.length > 0 && (
                      <p className="text-neutral-400 text-xs">
                        LeRobot 은 에피소드 목록을 10개씩 모아 쓰고 개수는 매번 씁니다.
                        강제 종료되면 아직 안 쓴 목록이 사라집니다 —
                        <b className="text-neutral-200"> 프레임은 그대로라 되살릴 수 있습니다.</b>
                      </p>
                    )}
                    {health.unrecoverable > 0 && (
                      <p className="text-neutral-400 text-xs">
                        그중 {health.unrecoverable}개는 프레임까지 없어 되살릴 수 없습니다.
                      </p>
                    )}
                    {/* ⚠ 색인만 빠진 것과 **파일이 미완성인 것**은 다르다.
                        후자는 되돌릴 방법이 없는데, 그냥 "되살릴 것 없음" 으로
                        보여주면 왜 복구 버튼이 없는지 알 수가 없다 — 실제로
                        그 질문을 받았다. 무엇이 어떻게 깨졌는지 적는다. */}
                    {health.broken.length > 0 && (
                      <div className="rounded bg-neutral-900/60 p-2 space-y-1">
                        <p className="text-neutral-300">
                          아래는 <b>파일 자체가 미완성</b>이라 되살릴 수 없습니다 —
                          녹화가 쓰는 도중 끊겼습니다.
                        </p>
                        <ul className="text-[11px] text-neutral-400 font-mono space-y-0.5">
                          {health.broken.map((b) => <li key={b}>· {b}</li>)}
                        </ul>
                      </div>
                    )}
                    {health.recoverable.length > 0 ? (
                      <button
                        onClick={() => void handleRepair(detail.id)}
                        disabled={repairing}
                        className="px-3 py-1.5 rounded bg-amber-700 hover:bg-amber-600 disabled:opacity-50 text-xs"
                      >
                        {repairing ? '복구 중…' : `색인 복구 (${health.recoverable.length}개)`}
                      </button>
                    ) : (
                      <p className="text-xs text-neutral-400">
                        되살릴 수 있는 에피소드가 없습니다. 남은 파일은 위 [삭제] 로 치울 수 있습니다.
                      </p>
                    )}
                  </div>
                )}

                {/* 에피소드 목록 */}
                {detail.episodes.length > 0 && (
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-sm text-neutral-400">
                        에피소드 목록
                      </span>
                      {selectedEpisodes.length > 0 && (
                        <div className="flex items-center gap-2">
                          <input type="text" value={editingTask ?? ''}
                            onChange={(e) => setEditingTask(e.target.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter') handleUpdateTask() }}
                            placeholder="새 Task 입력..."
                            className="px-2 py-1 text-xs rounded bg-neutral-900 border border-neutral-600 text-neutral-100 w-48 placeholder:text-neutral-500" />
                          <button onClick={handleUpdateTask} disabled={!editingTask?.trim()}
                            className="px-3 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
                            Task 변경 ({selectedEpisodes.length})
                          </button>
                          <button
                            onClick={handleDeleteEpisodes}
                            className="px-3 py-1 text-xs rounded bg-red-600 hover:bg-red-500 text-white"
                          >
                            삭제 ({selectedEpisodes.length})
                          </button>
                        </div>
                      )}
                    </div>
                    <div className="max-h-64 overflow-auto rounded border border-neutral-700">
                      <table className="w-full text-xs">
                        <thead className="bg-neutral-900 sticky top-0">
                          <tr>
                            <th className="p-2 w-8"></th>
                            <th className="p-2 text-left">Index</th>
                            <th className="p-2 text-left">Task</th>
                            <th className="p-2 text-left">Length</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.episodes.map((ep, i) => (
                            <tr
                              key={i}
                              className="border-t border-neutral-700 hover:bg-neutral-700/30"
                            >
                              <td className="p-2 text-center">
                                <input
                                  type="checkbox"
                                  checked={selectedEpisodes.includes(i)}
                                  onChange={() => toggleEpisode(i)}
                                />
                              </td>
                              <td className="p-2">{(ep as Record<string, unknown>).episode_index as number ?? i}</td>
                              <td className="p-2 truncate max-w-[200px]">
                                {(() => {
                                  const r = ep as Record<string, unknown>
                                  const t = r.tasks ?? r.task
                                  return Array.isArray(t) ? t.join(', ') : (t as string) ?? '-'
                                })()}
                              </td>
                              <td className="p-2">
                                {(ep as Record<string, unknown>).length as number ?? '-'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {/* Feature 구조 */}
                <details>
                  <summary className="text-sm text-neutral-400 cursor-pointer">
                    Feature 구조
                  </summary>
                  <pre className="mt-2 text-xs bg-neutral-900 p-3 rounded overflow-auto max-h-48">
                    {JSON.stringify(detail.features, null, 2)}
                  </pre>
                </details>
              </div>
            ) : (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-6 text-neutral-400 text-center">
                데이터셋을 선택하면 상세 정보가 표시됩니다
              </div>
            )}
          </div>
        </div>
      ) : null}

      {/* 업로드 모달 */}
      {uploadId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => { if (uploadState !== 'running' && uploadState !== 'starting') setUploadId(null) }}>
          <div className="bg-neutral-800 border border-neutral-600 rounded-lg w-[600px] max-h-[80vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between p-4 border-b border-neutral-700">
              <h3 className="text-sm font-semibold">Hub 업로드: {uploadId}</h3>
              <div className="flex items-center gap-2">
                <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                  uploadState === 'running' || uploadState === 'starting' ? 'bg-blue-500/20 text-blue-400' :
                  uploadState === 'error' ? 'bg-red-500/20 text-red-400' :
                  uploadState === 'idle' ? 'bg-green-500/20 text-green-400' : 'bg-neutral-700 text-neutral-400'
                }`}>{uploadState}</span>
                {uploadState !== 'running' && uploadState !== 'starting' && (
                  <button onClick={() => setUploadId(null)} className="text-neutral-400 hover:text-white text-lg leading-none">&times;</button>
                )}
              </div>
            </div>
            <div className="flex-1 overflow-hidden p-2">
              <LogViewer logs={uploadLogs} height="min-h-[280px] max-h-[50vh]" placeholder="시작 대기..." searchable />
            </div>
            <div className="p-3 border-t border-neutral-700 flex justify-end gap-2">
              {(uploadState === 'running' || uploadState === 'starting') && (
                <button onClick={() => api.post('/datasets/upload-stop')} className="px-3 py-1 text-xs rounded bg-red-600 hover:bg-red-500 text-white">중지</button>
              )}
              {uploadState !== 'running' && uploadState !== 'starting' && (
                <button onClick={() => setUploadId(null)} className="px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-300">닫기</button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
