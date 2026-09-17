import { useState } from 'react'
import CloudRentTab from '../components/CloudRentTab'
import CloudInstancesTab from '../components/CloudInstancesTab'
import CloudRentProgress from '../components/CloudRentProgress'

/**
 * 클라우드 GPU — 남의 GPU 를 빌려 학습한다 (feature/vast-training.md §9-2).
 *
 * 탭 둘:
 * - **RENT** — 빌릴 기계를 고른다.
 * - **인스턴스** — 지금 도는 것·비용·고아 파기.
 *
 * ⚠ **자격증명은 여기가 아니다.** API 키와 SSH 키는 설정 → 클라우드 탭이다(§9-1).
 * 한 번 맞춰 두는 것과 매일 보는 것을 가른다 — 저장소(HF) 탭과 같은 이유다.
 */
export default function CloudPage() {
  const [tab, setTab] = useState<'rent' | 'instances'>('rent')

  return (
    <div className="space-y-6">
      {/* ⚠ 탭은 제목과 **같은 줄** 바로 옆이다 — 아래로 쌓거나 오른쪽 끝으로 밀지 않는다 */}
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">클라우드 GPU</h1>
        <div className="flex overflow-hidden rounded-lg border border-neutral-700 text-sm">
          {([['rent', 'RENT'], ['instances', '인스턴스']] as const).map(([k, label]) => (
            <button key={k} onClick={() => setTab(k)}
              className={`px-4 py-1.5 ${tab === k
                ? 'bg-neutral-700 text-white' : 'bg-neutral-900 text-neutral-400 hover:text-white'}`}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* ⚠ 탭 **바깥**이다. 빌린 기계의 단계는 어느 탭을 보고 있든 같은 자리에 있어야
          한다 — 고르던 중에 시작해 놓고 인스턴스 탭으로 넘어가면 사라지면 안 된다. */}
      <CloudRentProgress />

      {tab === 'rent' && <CloudRentTab />}
      {tab === 'instances' && <CloudInstancesTab />}
    </div>
  )
}
