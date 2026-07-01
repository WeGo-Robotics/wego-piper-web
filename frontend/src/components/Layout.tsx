import { NavLink, Outlet } from 'react-router-dom'
import EStopButton from './EStopButton'

const navItems = [
  { to: '/', label: '대시보드' },
  { to: '/robots', label: '로봇' },
  { to: '/cameras', label: '카메라' },
  { to: '/recording', label: '수집' },
  { to: '/inference', label: '추론' },
  { to: '/training', label: '학습' },
  { to: '/hub', label: '허브' },
  { to: '/policy-server', label: '정책서버' },
  { to: '/logs', label: '로그' },
  { to: '/debug', label: '디버그 ↗', external: true },
  { to: '/settings', label: '설정' },
]

export default function Layout() {
  return (
    <div className="min-h-screen bg-neutral-900 text-neutral-100">
      <nav className="border-b border-neutral-700 bg-neutral-900/80 backdrop-blur sticky top-0 z-40">
        <div className="max-w-7xl mx-auto px-4 flex items-center h-14 gap-6">
          <span className="font-bold text-lg">Piper</span>
          <div className="flex gap-1">
            {navItems.map((item) =>
              item.external ? (
                <a
                  key={item.to}
                  href={item.to}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="px-3 py-1.5 rounded text-sm transition-colors text-neutral-400 hover:text-white"
                >
                  {item.label}
                </a>
              ) : (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/'}
                  className={({ isActive }) =>
                    `px-3 py-1.5 rounded text-sm transition-colors ${
                      isActive
                        ? 'bg-neutral-700 text-white'
                        : 'text-neutral-400 hover:text-white'
                    }`
                  }
                >
                  {item.label}
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
