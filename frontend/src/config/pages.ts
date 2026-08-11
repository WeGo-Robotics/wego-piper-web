import { lazy } from 'react'
import type { ComponentType } from 'react'
import DashboardPage from '../pages/DashboardPage'
import RobotsPage from '../pages/RobotsPage'
import CamerasPage from '../pages/CamerasPage'
import RecordingPage from '../pages/RecordingPage'
import InferencePage from '../pages/InferencePage'
import ModelsPage from '../pages/ModelsPage'
import DatasetsPage from '../pages/DatasetsPage'
import TrainingPage from '../pages/TrainingPage'
import HubPage from '../pages/HubPage'
import PolicyServerPage from '../pages/PolicyServerPage'
import EncoderProbePage from '../pages/EncoderProbePage'
import LogsPage from '../pages/LogsPage'
import SettingsPage from '../pages/SettingsPage'

// 디버그 뷰어는 Plotly를 포함하므로 lazy 로드해 메인 번들에서 코드 분리
const DebugLogsPage = lazy(() => import('../pages/DebugLogsPage'))

export type PageEntry = {
  /** 라우트 경로. '/'는 인덱스 라우트 */
  path: string
  /** 내비게이션 링크 / 대시보드 카드 제목 */
  label: string
  /** 대시보드 카드 설명 (card: true일 때만 사용) */
  description?: string
  component: ComponentType
  /** 상단 내비게이션에 노출 */
  nav?: boolean
  /** 대시보드 카드로 노출 */
  card?: boolean
  /** 새 탭으로 열기 (Layout 밖 전체화면 페이지 등) */
  external?: boolean
  /** Layout(내비/E-stop) 밖에서 단독 렌더 */
  standalone?: boolean
}

/**
 * 페이지 정의 단일 소스.
 * 페이지를 추가하려면 컴포넌트를 import하고 아래 리스트에 한 줄 추가하면 된다.
 * 라우트 등록(main.tsx), 내비게이션(Layout.tsx), 대시보드 카드(DashboardPage.tsx)가
 * 모두 이 리스트에서 파생된다. 리스트 순서가 곧 내비/카드 노출 순서다.
 */
export const pages: PageEntry[] = [
  { path: '/', label: '대시보드', component: DashboardPage, nav: true },
  {
    path: '/robots',
    label: '로봇',
    description: 'CAN 포트 스캔, 팔 연결, 역할 지정',
    component: RobotsPage,
    nav: true,
    card: true,
  },
  {
    path: '/cameras',
    label: '카메라',
    description: '카메라 스캔, 연결, 설정, 프리뷰',
    component: CamerasPage,
    nav: true,
    card: true,
  },
  { path: '/recording', label: '수집', component: RecordingPage, nav: true },
  {
    path: '/inference',
    label: '추론',
    description: '모델 배포, 실시간 파라미터 튜닝, 평가',
    component: InferencePage,
    nav: true,
    card: true,
  },
  {
    path: '/models',
    label: '모델',
    description: '로컬 체크포인트 관리, Hub 탐색',
    component: ModelsPage,
    card: true,
  },
  {
    path: '/datasets',
    label: '데이터셋',
    description: '데이터셋 열람, 에피소드 관리',
    component: DatasetsPage,
    card: true,
  },
  { path: '/training', label: '학습', component: TrainingPage, nav: true },
  { path: '/hub', label: '허브', component: HubPage, nav: true },
  { path: '/policy-server', label: '정책서버', component: PolicyServerPage, nav: true },
  { path: '/encoder', label: '엔코더', component: EncoderProbePage, nav: true },
  { path: '/logs', label: '로그', component: LogsPage, nav: true },
  { path: '/debug', label: '디버그', component: DebugLogsPage, nav: true, external: true, standalone: true },
  { path: '/settings', label: '설정', component: SettingsPage, nav: true },
]

export const navPages = pages.filter((p) => p.nav)
export const cardPages = pages.filter((p) => p.card)
export const layoutPages = pages.filter((p) => !p.standalone)
export const standalonePages = pages.filter((p) => p.standalone)
