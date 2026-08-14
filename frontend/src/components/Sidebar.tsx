import { NavLink } from 'react-router-dom'
import type { PageEntry } from '../config/pages'
import { navGroups, ungroupedNavPages } from '../config/pages'

/**
 * 좌측 세로 내비 (feature/layout-redesign.md).
 *
 * ## 왜 세로인가
 *
 * 상단 가로 내비가 자리를 다 썼다. 증거는 `pages.ts` 에 남아 있었다 — **모델과
 * 데이터셋은 `card` 만 있고 `nav` 가 없었다.** 바에 자리가 없어 밀려난 것이다.
 * 앞으로 확정된 것만 넷 더다(`/cloud`, `/cloud/guide`, 페이즈 라벨, 조그 패널).
 * 가로는 안 늘어나고 세로는 늘어난다.
 *
 * ## 목록을 갖지 않는다
 *
 * 여기 있는 것은 **그리는 법**뿐이다. 무엇을 그릴지는 `pages.ts` 가 정한다 —
 * 사이드바가 자기 목록을 들면 페이지 추가가 다시 두 곳 수정이 되고, 그 파일이
 * 존재하는 이유가 사라진다.
 */

function Item({ page, collapsed }: { page: PageEntry; collapsed: boolean }) {
  const body = (
    <>
      <span className="w-5 shrink-0 text-center" aria-hidden>{page.icon ?? '·'}</span>
      {!collapsed && <span className="truncate">{page.label}</span>}
      {!collapsed && page.external && <span className="ml-auto text-xs opacity-50">↗</span>}
    </>
  )
  const base =
    'flex items-center gap-2 rounded px-2 py-1.5 text-sm transition-colors'

  // 새 탭으로 여는 것은 라우터를 안 탄다 — 활성 표시도 없다
  if (page.external) {
    return (
      <a href={page.path} target="_blank" rel="noopener noreferrer"
         title={collapsed ? page.label : undefined}
         className={`${base} text-neutral-400 hover:bg-neutral-800 hover:text-white`}>
        {body}
      </a>
    )
  }
  return (
    <NavLink to={page.path} end={page.path === '/'}
             title={collapsed ? page.label : undefined}
             className={({ isActive }) =>
               `${base} ${isActive
                 ? 'bg-neutral-700 text-white'
                 : 'text-neutral-400 hover:bg-neutral-800 hover:text-white'}`}>
      {body}
    </NavLink>
  )
}

export default function Sidebar({
  collapsed,
  onToggle,
}: {
  collapsed: boolean
  onToggle: () => void
}) {
  return (
    <nav
      aria-label="주 메뉴"
      className={`${collapsed ? 'w-14' : 'w-56'} shrink-0 border-r border-neutral-800
        bg-neutral-900 flex flex-col transition-[width] duration-150`}
    >
      <div className="flex-1 overflow-y-auto px-2 py-3 space-y-4">
        <div className="space-y-0.5">
          {ungroupedNavPages.map((p) => (
            <Item key={p.path} page={p} collapsed={collapsed} />
          ))}
        </div>

        {navGroups.map(([group, items]) => (
          <div key={group} className="space-y-0.5">
            {/* 접었을 때는 제목 대신 구분선만 남긴다 — 세로 글씨는 안 읽힌다 */}
            {collapsed ? (
              <div className="mx-2 my-2 border-t border-neutral-800" role="presentation" />
            ) : (
              <p className="px-2 pb-1 text-[11px] font-medium uppercase tracking-wider
                            text-neutral-600">
                {group}
              </p>
            )}
            {items.map((p) => (
              <Item key={p.path} page={p} collapsed={collapsed} />
            ))}
          </div>
        ))}
      </div>

      <button
        onClick={onToggle}
        className="border-t border-neutral-800 px-2 py-2 text-sm text-neutral-500
                   hover:bg-neutral-800 hover:text-neutral-300"
        title={collapsed ? '메뉴 펼치기' : '메뉴 접기'}
        aria-expanded={!collapsed}
      >
        {collapsed ? '»' : '« 접기'}
      </button>
    </nav>
  )
}
