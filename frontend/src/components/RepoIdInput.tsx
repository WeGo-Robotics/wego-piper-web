import { useEffect, useState } from 'react'
import { api } from '../services/api'

/**
 * `네임스페이스/이름` 을 고르고 적는다.
 *
 * ## 왜 나눴나
 *
 * 통째로 타이핑하면 `/` 를 빠뜨린다. LeRobot 은 `repo_id.split("/")` 를 **두 개로
 * 언패킹**하므로 슬래시가 없으면 녹화가 시작에서 죽는다. 네임스페이스는 고를 수
 * 있는 값(내 계정 + 소속 조직)이라 드롭다운이면 그 실수가 사라진다.
 *
 * ⚠ **비워 둘 수 있는 자리가 있다.** 학습의 `policy.repo_id` 가 그렇다 — 비우면
 * `--policy.push_to_hub=false` 가 붙어 로컬에만 남는다. 그래서 `allowEmpty` 인
 * 곳에서는 "올리지 않음" 을 **명시적인 선택지**로 둔다. 빈 칸을 그냥 두는 것과
 * 고르는 것은 다르다 — 후자만 "의도한 것" 으로 읽힌다.
 *
 * ⚠ **로그인 안 됐으면 그냥 글상자로 둔다.** 네임스페이스를 알 수 없는데 드롭다운을
 * 비워 놓으면 아무것도 못 적는 화면이 된다.
 */

type Account = { logged_in: boolean; username: string; orgs: string[] }

const NONE = '__none__'   // "올리지 않음" — 빈 문자열과 구분되는 내부 값

export function repoIdError(v: string): string | null {
  const s = v.trim()
  if (!s) return null
  const parts = s.split('/')
  if (parts.length !== 2 || !parts[0] || !parts[1])
    return "'네임스페이스/이름' 형식이어야 합니다"
  if (/\s/.test(s)) return '공백은 쓸 수 없습니다'
  return null
}

export default function RepoIdInput({
  value, onChange, allowEmpty = false, emptyLabel = '올리지 않음 (로컬에만)', uploads = true,
}: {
  value: string
  onChange: (v: string) => void
  allowEmpty?: boolean
  emptyLabel?: string
  /** Hub 로 올라가는 값인가. 데이터셋은 이게 꺼져도 repo_id 가 **로컬 폴더 경로**라
   *  여전히 필요하다 — 그래서 "비울 수 있느냐"(allowEmpty)와는 다른 축이다. */
  uploads?: boolean
}) {
  const [acc, setAcc] = useState<Account | null>(null)
  useEffect(() => {
    api.get<Account>('/hub/whoami').then(setAcc).catch(() => setAcc(null))
  }, [])

  const mine = acc?.logged_in ? [acc.username, ...acc.orgs].filter(Boolean) : []
  const slash = value.indexOf('/')
  const ns = slash > 0 ? value.slice(0, slash) : ''
  const name = slash > 0 ? value.slice(slash + 1) : value

  // ⚠ 저장된 값이 내 네임스페이스가 아니어도 **목록에서 빼지 않는다.** 빼면 고를 수
  //   없는 값이 되어 통짜 글상자로 되돌아가고, 그러면 고르기 자체가 안 붙은 것처럼
  //   보인다. 대신 남의 것이라고 적어 둔다 — 조용히 다른 곳을 가리키는 것만 막으면 된다.
  const foreign = !!ns && mine.length > 0 && !mine.includes(ns)
  const spaces = foreign ? [ns, ...mine] : mine

  // 글상자로 떨어지는 경우는 하나뿐이다: 로그인이 안 돼 후보를 모를 때
  if (spaces.length === 0) {
    return (
      <div>
        <input type="text" value={value} onChange={(e) => onChange(e.target.value)}
          placeholder={allowEmpty ? '비워 두면 로컬에만 남습니다' : '네임스페이스/이름'}
          className="w-full rounded border border-neutral-700 bg-neutral-900 px-3 py-1.5 text-sm text-neutral-100 focus:border-blue-500 focus:outline-none" />
        {!acc?.logged_in && (
          <p className="mt-1 text-xs text-neutral-500">
            설정 → 저장소에서 로그인하면 네임스페이스를 고를 수 있습니다.
          </p>
        )}
      </div>
    )
  }

  const empty = allowEmpty && !value.trim()

  return (
    <div>
    <div className="flex gap-1.5">
      <select
        value={empty ? NONE : (ns || spaces[0])}
        onChange={(e) => {
          if (e.target.value === NONE) { onChange(''); return }
          onChange(`${e.target.value}/${name}`)
        }}
        className="shrink-0 rounded border border-neutral-700 bg-neutral-900 px-2 py-1.5 text-sm text-neutral-100 focus:border-blue-500 focus:outline-none">
        {allowEmpty && <option value={NONE}>{emptyLabel}</option>}
        {spaces.map((s) => <option key={s} value={s}>{s}{foreign && s === ns ? ' (내 계정 아님)' : ''}</option>)}
      </select>
      {!empty && (
        <>
          <span className="self-center text-neutral-500">/</span>
          <input type="text" value={name}
            onChange={(e) => onChange(`${ns || spaces[0]}/${e.target.value}`)}
            placeholder="이름"
            className="min-w-0 flex-1 rounded border border-neutral-700 bg-neutral-900 px-3 py-1.5 text-sm text-neutral-100 focus:border-blue-500 focus:outline-none" />
        </>
      )}
    </div>
    {foreign && !empty && uploads && (
      <p className="mt-1 text-xs text-amber-400">
        {ns} 은 내 계정도, 소속 조직도 아닙니다 — 업로드가 거부될 수 있습니다.
      </p>
    )}
    {!uploads && (
      <p className="mt-1 text-xs text-neutral-500">
        Hub 업로드가 꺼져 있습니다 — 로컬 폴더 이름으로만 쓰입니다.
      </p>
    )}
    </div>
  )
}
