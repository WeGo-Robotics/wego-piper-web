import { StrictMode, Suspense, createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import './index.css'
import ErrorBoundary from './components/ErrorBoundary'
import Layout from './components/Layout'
import { SystemMessageProvider } from './components/SystemMessages'
import { layoutPages, standalonePages } from './config/pages'
import type { PageEntry } from './config/pages'

/**
 * 페이지를 렌더하는 **유일한 지점.**
 *
 * 세 군데서 각자 `createElement(page.component)` 를 하고 있었는데, 그러면
 * 에러 경계를 붙일 때 한 곳을 빠뜨리기 쉽다 — 빠진 페이지만 여전히 흰 화면이 되고
 * 그건 붙였다고 믿는 만큼 더 나쁘다.
 */
function renderPage(page: PageEntry) {
  return (
    <ErrorBoundary label={page.label}>
      {createElement(page.component)}
    </ErrorBoundary>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* ⚠ 라우터 **밖**이다 — 페이지가 바뀌어도 메시지가 살아 있어야 한다.
        장치가 빠진 뒤 다른 탭으로 옮겼다고 경고가 사라지면 안 된다. */}
    <SystemMessageProvider>
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          {layoutPages.map((page) =>
            page.path === '/' ? (
              <Route key={page.path} index element={renderPage(page)} />
            ) : (
              <Route key={page.path} path={page.path.slice(1)} element={renderPage(page)} />
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
                {renderPage(page)}
              </Suspense>
            }
          />
        ))}
      </Routes>
    </BrowserRouter>
    </SystemMessageProvider>
  </StrictMode>,
)
