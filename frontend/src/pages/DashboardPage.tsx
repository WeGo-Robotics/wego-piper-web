import { Link } from 'react-router-dom'

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Piper Web</h1>
      <p className="text-neutral-400">LeRobot 웹 인터페이스</p>
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
