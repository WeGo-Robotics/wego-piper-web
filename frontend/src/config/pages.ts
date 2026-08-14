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

/**
 * 사이드바 묶음. **순서가 곧 화면 순서다** — 정렬 규칙을 따로 두지 않는다.
 *
 * 흐름을 따라간다: 장치를 붙이고 → 데이터를 모으고 → 학습하고 → 돌린다.
 */
export const PAGE_GROUPS = ['장치', '수집', '학습', '실행', '시스템'] as const
export type PageGroup = (typeof PAGE_GROUPS)[number]

export type PageEntry = {
  /** 라우트 경로. '/'는 인덱스 라우트 */
  path: string
  /** 내비게이션 링크 / 대시보드 카드 제목 */
  label: string
  /** 대시보드 카드 설명 (card: true일 때만 사용) */
  description?: string
  component: ComponentType
  /** 내비게이션에 노출 */
  nav?: boolean
  /** 사이드바에서 어느 묶음에 들어가나. 없으면 묶음 밖(맨 위) */
  group?: PageGroup
  /** 사이드바를 접었을 때 이것만 보인다 */
  icon?: string
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
 * 라우트 등록(main.tsx), 내비게이션(Sidebar.tsx), 대시보드 카드(DashboardPage.tsx)가
 * 모두 이 리스트에서 파생된다. 리스트 순서가 곧 내비/카드 노출 순서다.
 *
 * ⚠ **사이드바가 자기 목록을 갖게 하면 안 된다.** 그러면 페이지 추가가 다시 두 곳
 * 수정이 되고 이 파일이 존재하는 이유가 사라진다 (feature/layout-redesign.md §3).
 */
export const pages: PageEntry[] = [
  { path: '/', label: '대시보드', component: DashboardPage, nav: true, icon: '🏠' },

  // ── 장치 ──
  {
    path: '/robots',
    label: '로봇',
    description: 'CAN 포트 스캔, 팔 연결, 역할 지정',
    component: RobotsPage,
    nav: true,
    group: '장치',
    icon: '🦾',
    card: true,
  },
  {
    path: '/cameras',
    label: '카메라',
    description: '카메라 스캔, 연결, 설정, 프리뷰',
    component: CamerasPage,
    nav: true,
    group: '장치',
    icon: '📷',
    card: true,
  },

  // ── 수집 ──
  { path: '/recording', label: '수집', component: RecordingPage, nav: true, group: '수집', icon: '⏺' },
  // ⚠ 데이터셋·모델은 **자리가 없어서** 상단바에서 빠져 있었다(카드 전용).
  //   세로 내비는 자리가 있으므로 돌아온다 (feature/layout-redesign.md §1).
  {
    path: '/datasets',
    label: '데이터셋',
    description: '데이터셋 열람, 에피소드 관리',
    component: DatasetsPage,
    nav: true,
    group: '수집',
    icon: '🗂',
    card: true,
  },

  // ── 학습 ──
  { path: '/training', label: '학습', component: TrainingPage, nav: true, group: '학습', icon: '📈' },
  {
    path: '/models',
    label: '모델',
    description: '로컬 체크포인트 관리, Hub 탐색',
    component: ModelsPage,
    nav: true,
    group: '학습',
    icon: '🧠',
    card: true,
  },
  { path: '/encoder', label: '엔코더', component: EncoderProbePage, nav: true, group: '학습', icon: '🔬' },

  // ── 실행 ──
  {
    path: '/inference',
    label: '추론',
    description: '모델 배포, 실시간 파라미터 튜닝, 평가',
    component: InferencePage,
    nav: true,
    group: '실행',
    icon: '▶',
    card: true,
  },
  { path: '/policy-server', label: '정책서버', component: PolicyServerPage, nav: true, group: '실행', icon: '🛰' },

  // ── 시스템 ──
  { path: '/hub', label: '허브', component: HubPage, nav: true, group: '시스템', icon: '☁' },
  { path: '/logs', label: '로그', component: LogsPage, nav: true, group: '시스템', icon: '📄' },
  { path: '/debug', label: '디버그', component: DebugLogsPage, nav: true, group: '시스템', icon: '🐛', external: true, standalone: true },
  { path: '/settings', label: '설정', component: SettingsPage, nav: true, group: '시스템', icon: '⚙' },
]

export const navPages = pages.filter((p) => p.nav)
export const cardPages = pages.filter((p) => p.card)
export const layoutPages = pages.filter((p) => !p.standalone)
export const standalonePages = pages.filter((p) => p.standalone)

/** 묶음 밖 항목(대시보드) — 사이드바 맨 위에 따로 놓는다. */
export const ungroupedNavPages = navPages.filter((p) => !p.group)

/**
 * `[묶음, 그 묶음의 페이지들]` — 사이드바가 그대로 그린다.
 *
 * 비어 있는 묶음은 내보내지 않는다. 그래야 `PAGE_GROUPS` 에 이름을 미리
 * 적어두고 페이지는 나중에 붙여도 빈 제목이 안 뜬다.
 */
export const navGroups: [PageGroup, PageEntry[]][] = PAGE_GROUPS
  .map((g) => [g, navPages.filter((p) => p.group === g)] as [PageGroup, PageEntry[]])
  .filter(([, items]) => items.length > 0)
