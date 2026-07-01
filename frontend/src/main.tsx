import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import './index.css'
import Layout from './components/Layout'
import DashboardPage from './pages/DashboardPage'
import InferencePage from './pages/InferencePage'
import ModelsPage from './pages/ModelsPage'
import DatasetsPage from './pages/DatasetsPage'
import HubPage from './pages/HubPage'
import RobotsPage from './pages/RobotsPage'
import CamerasPage from './pages/CamerasPage'
import LogsPage from './pages/LogsPage'
// 디버그 뷰어는 Plotly를 포함하므로 lazy 로드해 메인 번들에서 코드 분리
const DebugLogsPage = lazy(() => import('./pages/DebugLogsPage'))
import TrainingPage from './pages/TrainingPage'
import RecordingPage from './pages/RecordingPage'
import PolicyServerPage from './pages/PolicyServerPage'
import SettingsPage from './pages/SettingsPage'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<DashboardPage />} />
          <Route path="robots" element={<RobotsPage />} />
          <Route path="cameras" element={<CamerasPage />} />
          <Route path="inference" element={<InferencePage />} />
          <Route path="hub" element={<HubPage />} />
          <Route path="models" element={<ModelsPage />} />
          <Route path="datasets" element={<DatasetsPage />} />
          <Route path="recording" element={<RecordingPage />} />
          <Route path="training" element={<TrainingPage />} />
          <Route path="policy-server" element={<PolicyServerPage />} />
          <Route path="logs" element={<LogsPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
        {/* 디버그 뷰어는 기존 UI와 별개로 전체 화면(Layout 밖)으로 렌더 */}
        <Route path="debug" element={
          <Suspense fallback={<div className="min-h-screen bg-neutral-900 text-neutral-100 flex items-center justify-center text-sm text-neutral-400">로딩 중...</div>}>
            <DebugLogsPage />
          </Suspense>
        } />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
