import { StrictMode, Suspense, createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import './index.css'
import Layout from './components/Layout'
import { layoutPages, standalonePages } from './config/pages'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          {layoutPages.map((page) =>
            page.path === '/' ? (
              <Route key={page.path} index element={createElement(page.component)} />
            ) : (
              <Route key={page.path} path={page.path.slice(1)} element={createElement(page.component)} />
            )
          )}
        </Route>
        {/* standalone 페이지(디버그 뷰어 등)는 기존 UI와 별개로 전체 화면(Layout 밖)으로 렌더 */}
        {standalonePages.map((page) => (
          <Route
            key={page.path}
            path={page.path.slice(1)}
            element={
              <Suspense
                fallback={
                  <div className="min-h-screen bg-neutral-900 text-neutral-100 flex items-center justify-center text-sm text-neutral-400">
                    로딩 중...
                  </div>
                }
              >
                {createElement(page.component)}
              </Suspense>
            }
          />
        ))}
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
