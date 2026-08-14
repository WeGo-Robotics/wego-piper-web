import { NavLink, Outlet } from 'react-router-dom'
import DeviceAlerts from './DeviceAlerts'
import EStopButton from './EStopButton'
import { SystemMessageHost } from './SystemMessages'
import { navPages } from '../config/pages'

export default function Layout() {
  return (
    <div className="min-h-screen bg-neutral-900 text-neutral-100">
      <nav className="border-b border-neutral-700 bg-neutral-900/80 backdrop-blur sticky top-0 z-40">
        <div className="max-w-7xl mx-auto px-4 flex items-center h-14 gap-6">
          <span className="font-bold text-lg">Piper</span>
          <div className="flex gap-1">
            {navPages.map((page) =>
              page.external ? (
                <a
                  key={page.path}
                  href={page.path}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="px-3 py-1.5 rounded text-sm transition-colors text-neutral-400 hover:text-white"
                >
                  {page.label} ↗
                </a>
              ) : (
                <NavLink
                  key={page.path}
                  to={page.path}
                  end={page.path === '/'}
                  className={({ isActive }) =>
                    `px-3 py-1.5 rounded text-sm transition-colors ${
                      isActive
                        ? 'bg-neutral-700 text-white'
                        : 'text-neutral-400 hover:text-white'
                    }`
                  }
                >
                  {page.label}
                </NavLink>
              )
            )}
          </div>
        </div>
      </nav>
      {/* 장치 경보를 시스템 메시지로 흘려보낸다 (자기 UI 없음).
          ⚠ 문서 흐름에 배너를 두면 긴 페이지에서 스크롤을 올려야 보인다 —
          수집 페이지에서 안 보인다는 지적이 그거였다. 호스트는 `fixed` 다. */}
      <DeviceAlerts />
      <SystemMessageHost />
      <main className="max-w-7xl mx-auto px-4 py-6">
        <Outlet />
      </main>
      <EStopButton />
    </div>
  )
}
