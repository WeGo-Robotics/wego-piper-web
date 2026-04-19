import { useEffect, useState } from 'react'
import { api } from '../services/api'
import type { Dataset, DatasetDetail } from '../types/models'
import DiskUsageBar from '../components/DiskUsageBar'
import HubBrowser from '../components/HubBrowser'

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

type SortKey = 'name' | 'size' | 'date' | 'episodes'

export default function DatasetsPage({ embedded = false }: { embedded?: boolean }) {
  const [tab, setTab] = useState<'local' | 'hub'>('local')
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<DatasetDetail | null>(null)
  const [selectedEpisodes, setSelectedEpisodes] = useState<number[]>([])
  const [editingTask, setEditingTask] = useState<string | null>(null)  // 선택된 에피소드에 적용할 task
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('date')
  const [sortAsc, setSortAsc] = useState(false)

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

  const handleSelect = async (id: string) => {
    try {
      const d = await api.get<DatasetDetail>(`/datasets/${id}`)
      setDetail(d)
      setSelectedEpisodes([])
    } catch {
      // ignore
    }
  }

  const handleDelete = async (id: string) => {
    if (!confirm(`"${id}" 데이터셋을 삭제하시겠습니까?`)) return
    await api.delete(`/datasets/${id}`)
    fetchDatasets()
    if (detail?.id === id) setDetail(null)
  }

  const handleDeleteEpisodes = async () => {
    if (!detail || selectedEpisodes.length === 0) return
    if (!confirm(`${selectedEpisodes.length}개 에피소드를 삭제하시겠습니까?`))
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
    } catch { alert('Task 변경 실패') }
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
      {embedded && (
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
      )}

      <DiskUsageBar />

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
                <p className="font-medium truncate">{ds.id}</p>
                <p className="text-xs text-neutral-400 mt-1">
                  {ds.total_episodes} 에피소드 · {formatBytes(ds.size_bytes)}
                </p>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    handleDelete(ds.id)
                  }}
                  className="mt-2 px-3 py-1 text-xs rounded bg-neutral-700 hover:bg-red-600 text-neutral-300 hover:text-white"
                >
                  삭제
                </button>
              </div>
            ))}
          </div>

          {/* 상세 패널 */}
          <div className="lg:col-span-2">
            {detail ? (
              <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-6 space-y-4">
                <h2 className="text-lg font-semibold">{detail.id}</h2>
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
    </div>
  )
}
