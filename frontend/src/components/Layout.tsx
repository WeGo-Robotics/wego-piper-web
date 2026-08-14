import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import DeviceAlerts from './DeviceAlerts'
import EStopButton from './EStopButton'
import Sidebar from './Sidebar'
import StatusBar from './StatusBar'
import { SystemMessageHost } from './SystemMessages'

/**
 * 2단 골격 — 좌측 세로 내비 + 상단 상태바 (feature/layout-redesign.md).
 *
 * 상단 가로 내비가 자리를 다 써서 **모델·데이터셋이 내비에서 밀려나 있었다.**
 * 세로로 내리면 자리가 생기고, 비워진 상단은 상태 표시에 쓴다.
 */

const COLLAPSE_KEY = 'piper.sidebar.collapsed'

export default function Layout() {
  // 사용자가 접었으면 넓은 화면에서도 접힌 채로 둔다 — 다음에 와도 그대로다
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSE_KEY) === '1',
  )
  const toggle = () => {
    setCollapsed((c) => {
      localStorage.setItem(COLLAPSE_KEY, c ? '0' : '1')
      return !c
    })
  }

  return (
    <div className="h-screen flex flex-col bg-neutral-900 text-neutral-100">
      <StatusBar />
      <div className="flex flex-1 min-h-0">
        <Sidebar collapsed={collapsed} onToggle={toggle} />
        {/* ⚠ 본문에 `max-w` 를 두지 않는다. 사이드바가 왼쪽을 먹은 뒤에도 가운데
            정렬하면 오른쪽이 비어 보인다 — 폭 제한이 필요한 페이지가 스스로 건다. */}
        <main className="flex-1 min-w-0 overflow-y-auto px-4 py-6">
          <Outlet />
        </main>
      </div>
      {/* 장치 경보를 시스템 메시지로 흘려보낸다 (자기 UI 없음).
          ⚠ 문서 흐름에 배너를 두면 긴 페이지에서 스크롤을 올려야 보인다 —
          수집 페이지에서 안 보인다는 지적이 그거였다. 호스트는 `fixed` 다. */}
      <DeviceAlerts />
      <SystemMessageHost />
      {/* ⚠ E-stop 은 자리를 안 옮긴다. 사이드바가 접히든 펴지든, 어느 페이지든
          **같은 자리**여야 한다 — 그게 안전 장치의 요건이다. */}
      <EStopButton />
    </div>
  )
}
