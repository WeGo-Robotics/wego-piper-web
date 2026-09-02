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
import VisionPage from '../pages/VisionPage'
import YoloDemoPage from '../pages/YoloDemoPage'
import YoloTrainPage from '../pages/YoloTrainPage'
import EncoderProbePage from '../pages/EncoderProbePage'
import LogsPage from '../pages/LogsPage'
import SettingsPage from '../pages/SettingsPage'

// 디버그 뷰어는 Plotly를 포함하므로 lazy 로드해 메인 번들에서 코드 분리
const DebugLogsPage = lazy(() => import('../pages/DebugLogsPage'))
// 에피소드 뷰어도 같은 이유 (신호 그래프 = Plotly)
const EpisodesPage = lazy(() => import('../pages/EpisodesPage'))

/**
 * 사이드바 묶음. **순서가 곧 화면 순서다** — 정렬 규칙을 따로 두지 않는다.
 *
 * 플랫폼 별로 나눈다: 하드웨어(장치) → LeRobot(모방학습 본선) → 객체 검출(인식)
 * → 통합(플랫폼을 엮는 파이프라인: 검출→LLM→VLA) → 시스템.
 *
 * ⚠ **화면에 특정 검출 모델의 이름을 쓰지 않는다.** 지금 구현이 무엇이든
 * 갈아끼울 수 있어야 하고, 라이선스가 그 교체를 강제할 수도 있다.
 * 묶음 **안**에서는 여전히 작업 흐름 순서다 (수집하고 → 학습하고 → 돌린다).
 */
export const PAGE_GROUPS = ['장치', 'LeRobot', '객체 검출', '통합', '시스템'] as const
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

  // ── LeRobot (모방학습 본선: 수집 → 학습 → 실행) ──
  { path: '/recording', label: '수집', component: RecordingPage, nav: true, group: 'LeRobot', icon: '⏺' },
  // ⚠ `데이터셋`·`모델` 은 이제 내비에 없다. `저장소` 화면이 **같은 컴포넌트**를
  //   탭으로 그리므로 메뉴에 같은 화면이 두 번 있었다.
  //   경로는 남긴다 — 북마크나 링크가 있으면 열려야 한다.
  //   ⚠ 예전에는 반대 문제였다: 상단 가로바에 자리가 없어 `card` 만 달았고,
  //     그때는 **대시보드 카드가 유일한 입구**였다(feature/layout-redesign.md §1).
  //     지금은 `저장소` 가 그 입구다 — 입구가 없어지지 않았는지 테스트가 본다.
  { path: '/datasets', label: '데이터셋', component: DatasetsPage, group: 'LeRobot', icon: '🗂' },
  {
    path: '/episodes',
    label: '에피소드',
    description: '에피소드 재생·신호·페이즈 열람',
    component: EpisodesPage,
    nav: true,
    group: 'LeRobot',
    icon: '🎬',
    card: true,
  },
  { path: '/training', label: '학습', component: TrainingPage, nav: true, group: 'LeRobot', icon: '📈' },
  // ⚠ `모델` 항목을 없앴다. `저장소` 의 `모델` 탭이 **같은 컴포넌트**를 그리고
  //   있어서 메뉴에 같은 화면이 두 번 있었다. 경로는 남겨 둔다 — 다른 화면이
  //   `/models` 로 링크하거나 북마크가 있을 수 있고, 열면 그대로 동작한다.
  { path: '/models', label: '모델', component: ModelsPage, group: 'LeRobot', icon: '🧠' },
  { path: '/encoder', label: '엔코더', component: EncoderProbePage, nav: true, group: 'LeRobot', icon: '🔬' },
  {
    path: '/inference',
    label: '추론',
    description: '모델 배포, 실시간 파라미터 튜닝, 평가',
    component: InferencePage,
    nav: true,
    group: 'LeRobot',
    icon: '▶',
    card: true,
  },
  { path: '/policy-server', label: '정책서버', component: PolicyServerPage, nav: true, group: 'LeRobot', icon: '🛰' },
  // 로컬 + HuggingFace Hub 를 한 화면에서 본다. 모델·데이터셋 탭이 여기 있다.
  { path: '/hub', label: '저장소', component: HubPage, nav: true, group: 'LeRobot', icon: '☁' },

  // ── 객체 검출 (인식: 데모 → 커스텀 학습) ──
  {
    path: '/yolo-demo',
    label: '검출 데모',
    description: '시연용 대형 검출 뷰',
    component: YoloDemoPage,
    nav: true,
    group: '객체 검출',
    icon: '🎯',
  },
  {
    path: '/yolo-train',
    label: '검출 학습',
    description: '이미지 캡처·라벨링·커스텀 가중치 학습',
    component: YoloTrainPage,
    nav: true,
    group: '객체 검출',
    icon: '🏷',
    card: true,
  },

  // ── 통합 (플랫폼을 엮는 파이프라인 — 객체 검출 → LLM 판단 → VLA 실행) ──
  {
    path: '/vision',
    label: '비전·판단',
    description: '객체 검출·LLM 판단 테스트 (분리수거 파이프라인)',
    component: VisionPage,
    nav: true,
    group: '통합',
    icon: '👁',
    card: true,
  },

  // ── 시스템 ──
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
