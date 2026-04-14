import { useState } from 'react'
import ModelsPage from './ModelsPage'
import DatasetsPage from './DatasetsPage'

type MainTab = 'models' | 'datasets'

export default function HubPage() {
  const [mainTab, setMainTab] = useState<MainTab>('models')

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-6">
        <h1 className="text-2xl font-bold">허브</h1>
        <div className="flex gap-1">
          <button onClick={() => setMainTab('models')}
            className={`px-4 py-1.5 rounded text-sm font-medium transition-colors ${
              mainTab === 'models' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'
            }`}>
            모델
          </button>
          <button onClick={() => setMainTab('datasets')}
            className={`px-4 py-1.5 rounded text-sm font-medium transition-colors ${
              mainTab === 'datasets' ? 'bg-neutral-700 text-white' : 'text-neutral-400 hover:text-white'
            }`}>
            데이터셋
          </button>
        </div>
      </div>

      {mainTab === 'models' ? <ModelsPage embedded /> : <DatasetsPage embedded />}
    </div>
  )
}
