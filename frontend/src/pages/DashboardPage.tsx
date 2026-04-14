import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../services/api'

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
                  <p className="text-neutral-400 text-xs">{hf.username}{hf.token_name ? ` (${hf.token_name})` : ''}</p>
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
        <Link
          to="/robots"
          className="block rounded-lg border border-neutral-700 bg-neutral-800 p-6 hover:border-blue-500 transition-colors"
        >
          <h2 className="text-lg font-semibold mb-2">로봇</h2>
          <p className="text-sm text-neutral-400">
            CAN 포트 스캔, 팔 연결, 역할 지정
          </p>
        </Link>
        <Link
          to="/cameras"
          className="block rounded-lg border border-neutral-700 bg-neutral-800 p-6 hover:border-blue-500 transition-colors"
        >
          <h2 className="text-lg font-semibold mb-2">카메라</h2>
          <p className="text-sm text-neutral-400">
            카메라 스캔, 연결, 설정, 프리뷰
          </p>
        </Link>
        <Link
          to="/inference"
          className="block rounded-lg border border-neutral-700 bg-neutral-800 p-6 hover:border-blue-500 transition-colors"
        >
          <h2 className="text-lg font-semibold mb-2">추론</h2>
          <p className="text-sm text-neutral-400">
            모델 배포, 실시간 파라미터 튜닝, 평가
          </p>
        </Link>
        <Link
          to="/models"
          className="block rounded-lg border border-neutral-700 bg-neutral-800 p-6 hover:border-blue-500 transition-colors"
        >
          <h2 className="text-lg font-semibold mb-2">모델</h2>
          <p className="text-sm text-neutral-400">
            로컬 체크포인트 관리, Hub 탐색
          </p>
        </Link>
        <Link
          to="/datasets"
          className="block rounded-lg border border-neutral-700 bg-neutral-800 p-6 hover:border-blue-500 transition-colors"
        >
          <h2 className="text-lg font-semibold mb-2">데이터셋</h2>
          <p className="text-sm text-neutral-400">
            데이터셋 열람, 에피소드 관리
          </p>
        </Link>
      </div>
    </div>
  )
}
