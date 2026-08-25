import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../services/api'
import { cardPages } from '../config/pages'

type HfInfo = { logged_in: boolean; username: string; fullname: string; avatar_url: string; token_name: string; error?: string }

export default function DashboardPage() {
  const [hf, setHf] = useState<HfInfo | null>(null)

  useEffect(() => {
    api.get<HfInfo>('/hub/whoami').then(setHf).catch(() => setHf({ logged_in: false, username: '', fullname: '', avatar_url: '', token_name: '' }))
  }, [])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Piper Web</h1>
          <p className="text-neutral-400">LeRobot 웹 인터페이스</p>
        </div>
        {hf && (
          <div className="flex items-center gap-3 rounded-lg border border-neutral-700 bg-neutral-800 px-4 py-2">
            {hf.logged_in ? (
              <>
                {hf.avatar_url && <img src={hf.avatar_url} alt="" className="w-8 h-8 rounded-full" />}
                <div className="text-sm">
                  <p className="font-medium">{hf.fullname || hf.username}</p>
                  {/* ⚠ 토큰 이름을 그냥 괄호로 붙이면 **데이터셋이나 조직명처럼
                      읽힌다** — 실제로 `wego-hansu (yeonsei_02)` 를 보고 저게
                      뭐냐는 질문이 나왔다. 무엇인지 밝혀서 적는다. */}
                  <p className="text-neutral-400 text-xs">
                    {hf.username}
                    {hf.token_name && (
                      <span className="ml-1.5 rounded bg-neutral-700 px-1 py-px text-[10px] text-neutral-300"
                            title="이 기계가 쓰는 HuggingFace 액세스 토큰의 이름 (HF 설정에서 붙인 것)">
                        토큰 {hf.token_name}
                      </span>
                    )}
                  </p>
                </div>
                <span className="w-2 h-2 rounded-full bg-green-400" title="로그인됨" />
              </>
            ) : (
              <>
                <span className="text-sm text-neutral-400">HuggingFace 미로그인</span>
                <span className="w-2 h-2 rounded-full bg-red-400" />
              </>
            )}
          </div>
        )}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {cardPages.map((page) => (
          <Link
            key={page.path}
            to={page.path}
            className="block rounded-lg border border-neutral-700 bg-neutral-800 p-6 hover:border-blue-500 transition-colors"
          >
            <h2 className="text-lg font-semibold mb-2">{page.label}</h2>
            <p className="text-sm text-neutral-400">{page.description}</p>
          </Link>
        ))}
      </div>
    </div>
  )
}
