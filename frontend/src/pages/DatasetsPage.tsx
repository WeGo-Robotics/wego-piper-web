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

export default function DatasetsPage() {
  const [tab, setTab] = useState<'local' | 'hub'>('local')
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<DatasetDetail | null>(null)
  const [selectedEpisodes, setSelectedEpisodes] = useState<number[]>([])

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
    // 새로고침
    handleSelect(detail.id)
  }

  const toggleEpisode = (idx: number) => {
    setSelectedEpisodes((prev) =>
      prev.includes(idx) ? prev.filter((i) => i !== idx) : [...prev, idx],
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">데이터셋</h1>
        <div className="flex gap-1">
          <button
            onClick={() => setTab('local')}
            className={`px-3 py-1.5 rounded text-sm ${tab === 'local' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}
          >
            로컬
          </button>
          <button
            onClick={() => setTab('hub')}
            className={`px-3 py-1.5 rounded text-sm ${tab === 'hub' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'}`}
          >
            Hub
          </button>
        </div>
      </div>

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
            {datasets.map((ds) => (
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
                        <button
                          onClick={handleDeleteEpisodes}
                          className="px-3 py-1 text-xs rounded bg-red-600 hover:bg-red-500 text-white"
                        >
                          선택 삭제 ({selectedEpisodes.length})
                        </button>
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
                                {(ep as Record<string, unknown>).task as string ?? '-'}
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
