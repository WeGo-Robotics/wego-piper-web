import { useEffect, useState, useRef } from 'react'

type VizType = 'input' | 'features' | 'attention'

const VIZ_ITEMS: { type: VizType; label: string; desc: string; legend?: string[] }[] = [
  {
    type: 'input', label: '전처리 입력',
    desc: '모델이 보는 512x512 이미지 (resize + pad + normalize)',
  },
  {
    type: 'features', label: '특징맵 (PCA)',
    desc: 'SigLIP Vision Encoder 출력 패치를 PCA 3채널로 시각화',
    legend: ['유사 영역은 비슷한 색', '밝기 = 특징 활성도'],
  },
  {
    type: 'attention', label: 'Attention 히트맵',
    desc: 'VLM 마지막 레이어의 이미지 토큰 attention 가중치',
    legend: ['빨강 = 높은 attention', '파랑 = 낮은 attention'],
  },
]

function VizImage({ vizType, camIndex }: { vizType: VizType; camIndex: number }) {
  const imgRef = useRef<HTMLImageElement>(null)
  const [hasImage, setHasImage] = useState(false)

  useEffect(() => {
    let alive = true
    const poll = () => {
      if (!alive) return
      const url = `/api/inference/viz/${vizType}/${camIndex}?_=${Date.now()}`
      const img = new Image()
      img.onload = () => {
        if (alive && imgRef.current) {
          imgRef.current.src = img.src
          setHasImage(true)
        }
      }
      img.onerror = () => { if (alive) setHasImage(false) }
      img.src = url
      setTimeout(poll, 1000)
    }
    poll()
    return () => { alive = false }
  }, [vizType, camIndex])

  return (
    <div className="relative aspect-square bg-neutral-900 rounded overflow-hidden">
      <img ref={imgRef} alt={vizType}
        className={`w-full h-full object-contain ${hasImage ? '' : 'hidden'}`} />
      {!hasImage && (
        <div className="flex items-center justify-center h-full">
          <p className="text-neutral-500 text-xs">-</p>
        </div>
      )}
    </div>
  )
}

function AttentionColorbar() {
  return (
    <div className="flex items-center gap-1 mt-1">
      <span className="text-[10px] text-neutral-500">Low</span>
      <div className="flex-1 h-2 rounded"
        style={{ background: 'linear-gradient(to right, #0000ff, #00ffff, #00ff00, #ffff00, #ff0000)' }} />
      <span className="text-[10px] text-neutral-500">High</span>
    </div>
  )
}

export default function VizPanel({ cameraCount = 1 }: { cameraCount?: number }) {
  const [camIndex, setCamIndex] = useState(0)

  return (
    <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">모델 시각화</h3>
        {cameraCount > 1 && (
          <div className="flex gap-1">
            {Array.from({ length: cameraCount }, (_, i) => (
              <button key={i} onClick={() => setCamIndex(i)}
                className={`px-2 py-0.5 text-xs rounded ${camIndex === i ? 'bg-blue-600 text-white' : 'bg-neutral-700 text-neutral-400'}`}>
                cam {i}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="grid grid-cols-3 gap-3">
        {VIZ_ITEMS.map(({ type, label }) => (
          <div key={type} className="space-y-1">
            <p className="text-xs text-neutral-300 text-center font-medium">{label}</p>
            <VizImage vizType={type} camIndex={camIndex} />
            {type === 'attention' && <AttentionColorbar />}
          </div>
        ))}
      </div>
    </div>
  )
}
