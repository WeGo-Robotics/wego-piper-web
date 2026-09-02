import { useState } from 'react'
import DiskUsageBar from '../components/DiskUsageBar'
import ModelsPage from './ModelsPage'
import DatasetsPage from './DatasetsPage'

/**
 * 저장소 — 모델과 데이터셋을 로컬/Hub 양쪽에서 본다.
 *
 * ## 왜 한 줄인가
 *
 * 예전에는 제목·탭 / 로컬·Hub·새로고침 / 디스크 상자가 **세 줄**로 쌓였다.
 * 정작 봐야 할 목록이 그만큼 아래로 밀렸다. 셋 다 "무엇을 어디서 보나" 를
 * 정하는 것이라 한 줄에 있어도 읽힌다.
 *
 * ## 왜 출처를 여기서 들고 있나
 *
 * `로컬|Hub` 는 모델·데이터셋 **둘이 공유하는 선택**이다. 각 페이지가 따로
 * 들고 있으면 탭을 옮길 때마다 `로컬` 로 되돌아간다.
 *
 * ⚠ 메뉴의 `모델` 항목은 없앴다 — 이 화면의 `모델` 탭이 **같은 컴포넌트**를
 * 그리고 있었다. 가는 길이 둘일 이유가 없다.
 */

type MainTab = 'models' | 'datasets'
type Source = 'local' | 'hub'

export default function HubPage() {
  const [mainTab, setMainTab] = useState<MainTab>('models')
  const [source, setSource] = useState<Source>('local')
  const [refreshKey, setRefreshKey] = useState(0)

  // ⚠ **고르는 것과 하는 것은 모양이 달라야 한다.** `로컬|Hub` 는 둘 중 하나를
  //   고르는 것(선택)이고 `새로고침` 은 누르면 무슨 일이 일어나는 것(동작)이다.
  //   같은 줄에 같은 모양으로 두면 `새로고침` 이 세 번째 선택지처럼 읽힌다.
  //   선택은 **묶음 안에서 켜진 칸**으로, 동작은 **늘 테두리 있는 버튼**으로 둔다.
  const seg = (on: boolean) =>
    `px-3 py-1.5 rounded text-sm transition-colors ${
      on ? 'bg-neutral-700 text-white shadow-sm' : 'text-neutral-400 hover:text-white'}`
  const group = 'flex gap-0.5 rounded-lg bg-neutral-900/60 p-0.5 ring-1 ring-neutral-700/60'
  const action =
    'flex items-center gap-1.5 rounded-lg border border-neutral-600 bg-neutral-800 px-3 py-1.5 ' +
    'text-sm text-neutral-300 transition-colors hover:border-neutral-500 hover:bg-neutral-700 hover:text-white'

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 className="text-2xl font-bold">저장소</h1>

        <div className={group}>
          <button className={seg(mainTab === 'models')} onClick={() => setMainTab('models')}>모델</button>
          <button className={seg(mainTab === 'datasets')} onClick={() => setMainTab('datasets')}>데이터셋</button>
        </div>

        <span className="h-5 w-px bg-neutral-700" />

        <div className={group}>
          <button className={seg(source === 'local')} onClick={() => setSource('local')}>로컬</button>
          <button className={seg(source === 'hub')} onClick={() => setSource('hub')}>Hub</button>
        </div>

        <span className="h-5 w-px bg-neutral-700" />

        <button type="button" onClick={() => setRefreshKey((n) => n + 1)} className={action}>
          <span aria-hidden>⟳</span> 새로고침
        </button>

        {/* 용량은 오른쪽 끝으로 — 늘 보이되 시선을 안 끈다 */}
        <div className="ml-auto"><DiskUsageBar compact /></div>
      </div>

      {mainTab === 'models'
        ? <ModelsPage embedded tab={source} refreshKey={refreshKey} />
        : <DatasetsPage embedded tab={source} refreshKey={refreshKey} />}
    </div>
  )
}
