import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import './index.css'
import Layout from './components/Layout'
import DashboardPage from './pages/DashboardPage'
import InferencePage from './pages/InferencePage'
import ModelsPage from './pages/ModelsPage'
import DatasetsPage from './pages/DatasetsPage'
import RobotsPage from './pages/RobotsPage'
import CamerasPage from './pages/CamerasPage'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<DashboardPage />} />
          <Route path="robots" element={<RobotsPage />} />
          <Route path="cameras" element={<CamerasPage />} />
          <Route path="inference" element={<InferencePage />} />
          <Route path="models" element={<ModelsPage />} />
          <Route path="datasets" element={<DatasetsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
