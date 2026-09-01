import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * HuggingFace 로그인 상태.
 *
 * ⚠ **대시보드에 있었다.** 대시보드는 "지금 괜찮은가" 에만 답하는 자리이고,
 * 이건 상태가 아니라 **설정**이다 — 바뀌는 일이 거의 없고, 바꾸려면 어차피
 * 설정으로 온다.
 *
 * ⚠ **토큰 이름은 "토큰" 이라고 밝혀서 적는다.** 계정명 옆에 괄호로 붙였더니
 * `wego-hansu (yeonsei_02)` 를 보고 "저게 뭐냐"는 질문이 나왔다 — 데이터셋이나
 * 조직명처럼 읽힌다.
 */

type HfInfo = {
  logged_in: boolean; username: string; fullname: string
  avatar_url: string; token_name: string
}

export default function HfAccountBadge() {
  const [hf, setHf] = useState<HfInfo | null>(null)

  useEffect(() => {
    api.get<HfInfo>('/hub/whoami')
      .then(setHf)
      .catch(() => setHf({
        logged_in: false, username: '', fullname: '', avatar_url: '', token_name: '',
      }))
  }, [])

  if (!hf) return null

  return (
    <div className="flex items-center gap-3 rounded-lg border border-neutral-700 bg-neutral-800 px-4 py-2">
      {hf.logged_in ? (
        <>
          {hf.avatar_url && <img src={hf.avatar_url} alt="" className="w-8 h-8 rounded-full" />}
          <div className="text-sm">
            <p className="font-medium">{hf.fullname || hf.username}</p>
            <p className="text-neutral-400 text-xs">
              {hf.username}
              {hf.token_name && (
                <span className="ml-1.5 rounded bg-neutral-700 px-1 py-px text-[10px] text-neutral-300"
                      title="이 기계가 쓰는 HuggingFace 액세스 토큰의 이름 (HF 설정에서 붙인 것)">
                  토큰 {hf.token_name}
                </span>
              )}
            </p>
          </div>
          <span className="w-2 h-2 rounded-full bg-green-400" title="로그인됨" />
        </>
      ) : (
        <>
          <span className="text-sm text-neutral-400">HuggingFace 미로그인</span>
          <span className="w-2 h-2 rounded-full bg-red-400" />
        </>
      )}
    </div>
  )
}
