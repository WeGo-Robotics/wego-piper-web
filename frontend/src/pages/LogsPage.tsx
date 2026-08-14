import { useEffect, useState } from 'react'
import { useSystemMessage } from '../components/SystemMessages'
import { api } from '../services/api'

type LogFile = { name: string; category: string; label: string; size_kb: number; modified: string; path: string }
type Category = { id: string; label: string; count: number }

export default function LogsPage() {
  const { confirm: askConfirm } = useSystemMessage()
  const [logs, setLogs] = useState<LogFile[]>([])
  const [categories, setCategories] = useState<Category[]>([])
  const [selectedCat, setSelectedCat] = useState('')
  const [loading, setLoading] = useState(true)

  const fetchLogs = () => {
    setLoading(true)
    Promise.all([
      api.get<LogFile[]>(`/logs${selectedCat ? `?category=${selectedCat}` : ''}`),
      api.get<Category[]>('/logs/categories'),
    ]).then(([files, cats]) => {
      setLogs(files)
      setCategories(cats)
    }).catch(() => {
      setLogs([])
      setCategories([])
    }).finally(() => setLoading(false))
  }

  useEffect(() => { fetchLogs() }, [selectedCat])

  const handleDelete = async (name: string) => {
    if (!await askConfirm(`${name} 파일을 삭제하시겠습니까?`)) return
    try {
      await api.delete(`/logs/delete/${name}`)
      fetchLogs()
    } catch { /* ignore */ }
  }

  const totalFiles = categories.reduce((s, c) => s + c.count, 0)
  const totalSize = logs.reduce((s, f) => s + f.size_kb, 0)

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">로그 관리</h1>
        <div className="flex items-center gap-3">
          <span className="text-xs text-neutral-400">
            {totalFiles}개 파일 / {totalSize > 1024 ? `${(totalSize / 1024).toFixed(1)} MB` : `${totalSize.toFixed(0)} KB`}
          </span>
          <button onClick={fetchLogs}
            className="px-3 py-1.5 text-sm rounded bg-neutral-700 hover:bg-neutral-600 transition-colors">
            새로고침
          </button>
        </div>
      </div>

      {/* 카테고리 필터 */}
      <div className="flex gap-2 flex-wrap">
        <button onClick={() => setSelectedCat('')}
          className={`px-3 py-1 text-xs rounded ${!selectedCat ? 'bg-blue-600 text-white' : 'bg-neutral-700 text-neutral-400 hover:text-white'}`}>
          전체 ({totalFiles})
        </button>
        {categories.map((cat) => (
          <button key={cat.id} onClick={() => setSelectedCat(cat.id)}
            className={`px-3 py-1 text-xs rounded ${selectedCat === cat.id ? 'bg-blue-600 text-white' : 'bg-neutral-700 text-neutral-400 hover:text-white'}`}>
            {cat.label} ({cat.count})
          </button>
        ))}
      </div>

      {loading ? (
        <p className="text-neutral-400 text-sm">로딩 중...</p>
      ) : logs.length === 0 ? (
        <p className="text-neutral-400 text-sm">파일이 없습니다.</p>
      ) : (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-neutral-700 text-neutral-400">
                <th className="text-left px-4 py-2 font-medium">파일명</th>
                <th className="text-left px-4 py-2 font-medium">카테고리</th>
                <th className="text-right px-4 py-2 font-medium">크기</th>
                <th className="text-right px-4 py-2 font-medium">수정일</th>
                <th className="text-right px-4 py-2 font-medium">작업</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log) => (
                <tr key={`${log.category}-${log.name}`} className="border-b border-neutral-700/50 hover:bg-neutral-700/30">
                  <td className="px-4 py-2 font-mono text-xs">{log.name}</td>
                  <td className="px-4 py-2">
                    <span className="px-1.5 py-0.5 text-[10px] rounded bg-neutral-700 text-neutral-300">{log.label}</span>
                  </td>
                  <td className="px-4 py-2 text-right text-neutral-400 text-xs">{log.size_kb.toFixed(1)} KB</td>
                  <td className="px-4 py-2 text-right text-neutral-400 text-xs">
                    {new Date(log.modified).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                  </td>
                  <td className="px-4 py-2 text-right space-x-2">
                    <a href={`/api/logs/download/${log.name}`} download
                      className="text-blue-400 hover:text-blue-300 underline text-xs">
                      다운로드
                    </a>
                    {log.category === 'inference' ? (
                      <a href={`/api/logs/chart/${log.name}`} target="_blank" rel="noopener noreferrer"
                        className="text-green-400 hover:text-green-300 underline text-xs">
                        차트
                      </a>
                    ) : (
                      <span className="text-neutral-600 text-xs cursor-not-allowed">차트</span>
                    )}
                    <button onClick={() => handleDelete(log.name)}
                      className="text-red-400 hover:text-red-300 underline text-xs">
                      삭제
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
