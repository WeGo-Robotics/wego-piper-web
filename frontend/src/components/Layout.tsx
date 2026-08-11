import { NavLink, Outlet } from 'react-router-dom'
import EStopButton from './EStopButton'
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
      <main className="max-w-7xl mx-auto px-4 py-6">
        <Outlet />
      </main>
      <EStopButton />
    </div>
  )
}
