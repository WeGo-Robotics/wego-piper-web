import { useState, useEffect } from 'react'
import { api } from '../services/api'

type HubItem = {
  repo_id: string
  author: string
  downloads: number
  last_modified: string | null
  tags: string[]
}

type Props = {
  type: 'models' | 'datasets'
}

export default function HubBrowser({ type }: Props) {
  const [items, setItems] = useState<HubItem[]>([])
  const [query, setQuery] = useState('')
  const [author, setAuthor] = useState('lerobot')
  const [loading, setLoading] = useState(false)
  const [downloading, setDownloading] = useState<Set<string>>(new Set())

  const search = () => {
    setLoading(true)
    api
      .get<HubItem[]>(
        `/hub/${type}?q=${encodeURIComponent(query)}&author=${encodeURIComponent(author)}`,
      )
      .then(setItems)
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    search()
  }, [type])

  const handleDownload = async (repoId: string) => {
    setDownloading((prev) => new Set(prev).add(repoId))
    try {
      await api.post('/hub/download', {
        repo_id: repoId,
        repo_type: type === 'models' ? 'model' : 'dataset',
      })
    } catch {
      // ignore
    }
  }

  return (
    <div className="space-y-4">
      {/* 검색 */}
      <div className="flex gap-2">
        <input
          type="text"
          value={author}
          onChange={(e) => setAuthor(e.target.value)}
          placeholder="Author"
          className="w-32 px-3 py-1.5 rounded bg-neutral-800 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
        />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && search()}
          placeholder="검색..."
          className="flex-1 px-3 py-1.5 rounded bg-neutral-800 border border-neutral-700 text-sm text-neutral-100 focus:outline-none focus:border-blue-500"
        />
        <button
          onClick={search}
          disabled={loading}
          className="px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-sm text-white disabled:opacity-50"
        >
          {loading ? '검색 중...' : '검색'}
        </button>
      </div>

      {/* 결과 목록 */}
      {items.length === 0 && !loading ? (
        <p className="text-neutral-400 text-sm">결과 없음</p>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <div
              key={item.repo_id}
              className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 flex items-center justify-between"
            >
              <div className="min-w-0">
                <p className="font-medium truncate">{item.repo_id}</p>
                <div className="flex gap-3 mt-1 text-xs text-neutral-400">
                  <span>
                    Downloads: {item.downloads?.toLocaleString() ?? 0}
                  </span>
                  {item.last_modified && (
                    <span>
                      {new Date(item.last_modified).toLocaleDateString()}
                    </span>
                  )}
                </div>
                {item.tags.length > 0 && (
                  <div className="flex gap-1 mt-1 flex-wrap">
                    {item.tags.slice(0, 5).map((tag) => (
                      <span
                        key={tag}
                        className="px-1.5 py-0.5 text-[10px] rounded bg-neutral-700 text-neutral-300"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <button
                onClick={() => handleDownload(item.repo_id)}
                disabled={downloading.has(item.repo_id)}
                className="ml-4 shrink-0 px-3 py-1.5 text-xs rounded bg-green-600 hover:bg-green-500 text-white disabled:opacity-50"
              >
                {downloading.has(item.repo_id) ? '다운로드 중...' : '다운로드'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
