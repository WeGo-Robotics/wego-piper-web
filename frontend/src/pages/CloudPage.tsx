import { useState } from 'react'
import CloudRentTab from '../components/CloudRentTab'

/**
 * 클라우드 GPU — 남의 GPU 를 빌려 학습한다 (feature/vast-training.md §9-2).
 *
 * 탭 둘:
 * - **RENT** — 빌릴 기계를 고른다.
 * - **인스턴스** — 지금 도는 것·비용·고아 파기. (§8 W3 과 같이 들어온다)
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

      {tab === 'rent' && <CloudRentTab />}
      {tab === 'instances' && (
        <section className="rounded-lg border border-neutral-700 bg-neutral-800 p-6 text-sm text-neutral-400">
          <p className="mb-2 font-medium text-neutral-300">아직 없습니다.</p>
          <p>
            인스턴스를 띄우고 끄는 일은 <strong className="text-neutral-300">자동 파기·예산
            상한·고아 스캐너</strong>와 한 몸이라 같이 들어옵니다. 끌 수 없는 채로 켜는 화면을
            먼저 만들지 않습니다.
          </p>
        </section>
      )}
    </div>
  )
}
