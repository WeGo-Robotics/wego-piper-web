import { StrictMode } from 'react'
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
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
