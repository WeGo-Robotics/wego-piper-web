import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import SystemLogPanel from '../components/SystemLogPanel'
import { useSystemMessage } from '../components/SystemMessages'
import { api } from '../services/api'

/**
 * 로그 — 한 페이지, 두 탭.
 *
 * "로그"가 둘이었다: 여기(/tmp 의 추론 CSV·차트 파일 관리자)와 설정 속 탭(데몬 저널).
 * 정작 고장 났을 때 보는 저널이 설정에 숨어 있고, 사이드바 "로그"를 누르면 CSV 표가
 * 나왔다(사용자 지적 2026). 저널을 **시스템** 탭(기본)으로 여기 모으고, 파일은 **파일** 탭.
 * 기본이 시스템인 이유: "로그"를 누르는 순간의 질문은 거의 "뭐가 잘못됐나"다 — CSV 는
 * 추론 페이지가 끝날 때 이미 내려받기 링크를 준다.
 *
 * URL 이 상태를 든다 — `?tab=files`, `?unit=so101d&level=error` — 서비스 패널의 유닛별
 * [로그]가 딥링크로 들어오고, 뒤로가기가 탭을 되돌린다.
 */

const TABS = [{ id: 'system', label: '시스템' }, { id: 'files', label: '파일' }] as const
type TabId = (typeof TABS)[number]['id']
const LEVELS = ['error', 'warning', 'info'] as const
type Level = (typeof LEVELS)[number]

export default function LogsPage() {
  const [params, setParams] = useSearchParams()
  const tab: TabId = params.get('tab') === 'files' ? 'files' : 'system'
  const unit = params.get('unit') ?? undefined
  const lv = params.get('level')
  const level = lv && (LEVELS as readonly string[]).includes(lv) ? (lv as Level) : undefined
  const since = params.get('since') ?? undefined
  const setTab = (t: TabId) => setParams((p) => {
    const n = new URLSearchParams(p)
    if (t === 'system') n.delete('tab'); else n.set('tab', t)
    return n
  })

  return (
    <div className="space-y-6">
      {/* 탭은 **제목 바로 옆** — 카메라·로봇과 같은 머리줄. 오른쪽 끝으로 밀거나(justify-between)
          제목 아래 줄로 쌓지 않는다(사용자 규칙 2026-09-11). */}
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">로그</h1>
        <div className="flex rounded-lg border border-neutral-700 overflow-hidden text-sm">
          {TABS.map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`px-4 py-1.5 ${tab === t.id
                ? 'bg-neutral-700 text-white' : 'bg-neutral-900 text-neutral-400 hover:text-white'}`}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'system' && (
        <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-5">
          <SystemLogPanel initialUnit={unit} initialLevel={level} initialSince={since} />
        </div>
      )}
      {tab === 'files' && <LogFilesPanel />}
    </div>
  )
}

type LogFile = { name: string; category: string; label: string; size_kb: number; modified: string; path: string }
type Category = { id: string; label: string; count: number }

/** 파일 탭 — 추론이 /tmp 에 남긴 CSV·차트. 목록·내려받기·차트·삭제. */
function LogFilesPanel() {
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
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-neutral-400">추론이 /tmp 에 남긴 파일 — CSV·차트. 오래 둘 것은 내려받으세요.</p>
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
