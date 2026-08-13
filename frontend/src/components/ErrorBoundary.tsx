import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * 페이지 하나가 죽어도 앱 전체를 날리지 않는다.
 *
 * ⚠ **React 는 렌더 중 예외가 나면 트리를 통째로 버린다.** 그래서 지금까지는
 * 어떤 페이지의 사소한 버그 하나가 **흰 화면**으로 나타났고, 새로고침 전까지
 * 아무것도 못 했다 — 인코더 페이지에서 `ImageData` 크기 불일치로 실제로 겪었다.
 *
 * 여기서 잡으면 그 페이지만 대체 화면으로 바뀐다. 진행 중이던 학습·추론은
 * 백엔드에서 계속 돌고 있으므로, **새로고침을 강요하지 않는 것이 중요하다** —
 * 다른 탭으로 옮겨 상태를 보거나 정지시킬 수 있어야 한다.
 *
 * ## 왜 클래스인가
 *
 * `componentDidCatch`/`getDerivedStateFromError` 는 훅으로 대체된 적이 없다.
 * React 19 에서도 에러 경계는 클래스만 가능하다.
 */

type Props = { children: ReactNode; label?: string }
type State = { error: Error | null }

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 콘솔에는 남긴다 — 사용자가 개발자도구를 열면 스택이 있어야 한다
    console.error('페이지 렌더 실패:', error, info.componentStack)
  }

  private reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="rounded-lg border border-red-900/60 bg-red-950/20 p-6 space-y-3">
        <h2 className="text-sm font-semibold text-red-300">
          {this.props.label ? `${this.props.label} 화면에서 오류가 났습니다` : '화면에서 오류가 났습니다'}
        </h2>
        <p className="text-xs text-neutral-400 leading-relaxed">
          이 페이지만 멈췄습니다. <b>학습·녹화·추론은 백엔드에서 계속 돌고 있습니다</b> —
          다른 메뉴로 이동해 상태를 보거나 정지시킬 수 있습니다.
        </p>
        <pre className="text-[11px] text-red-300/80 bg-neutral-900 rounded p-3 overflow-x-auto">
          {error.message || String(error)}
        </pre>
        <div className="flex gap-2">
          <button onClick={this.reset}
            className="px-3 py-1.5 rounded bg-neutral-700 hover:bg-neutral-600 text-xs">
            다시 시도
          </button>
          <button onClick={() => window.location.reload()}
            className="px-3 py-1.5 rounded bg-neutral-800 hover:bg-neutral-700 text-xs text-neutral-300">
            새로고침
          </button>
        </div>
      </div>
    )
  }
}
