import { useEffect, useState, useCallback } from 'react'
import { api } from '../services/api'
import { useWebSocket, type WsMessage } from '../hooks/useWebSocket'
import type { ProcessState } from '../types/ws'
import LogViewer from '../components/LogViewer'

type ServerStatus = { state: string; pid: number | null; address: string; host: string; port: number; fps: number }
type RemoteCheck = { reachable: boolean; address: string; error?: string }

export default function PolicyServerPage() {
  // 모드
  const [mode, setMode] = useState<'local' | 'remote'>(() => (localStorage.getItem('ps_mode') as 'local' | 'remote') || 'local')

  // 로컬 서버
  const [status, setStatus] = useState<ServerStatus | null>(null)
  const [host, setHost] = useState('127.0.0.1')
  const [port, setPort] = useState(8088)
  const [fps, setFps] = useState(30)
  const [psState, setPsState] = useState<ProcessState>('idle')
  const [logs, setLogs] = useState<string[]>([])
  const MAX_LOGS = 500

  // 원격 서버
  const [remoteAddress, setRemoteAddress] = useState(() => localStorage.getItem('ps_remote_addr') || '192.168.1.100:8088')
  const [remoteCheck, setRemoteCheck] = useState<RemoteCheck | null>(null)
  const [checking, setChecking] = useState(false)

  const { connected } = useWebSocket('/ws', {
    onMessage: useCallback((msg: WsMessage) => {
      if (msg.type === 'ps_state') setPsState(msg.data as ProcessState)
      else if (msg.type === 'ps_log') setLogs((prev) => {
        const next = [...prev, msg.data as string]
        return next.length > MAX_LOGS ? next.slice(-MAX_LOGS) : next
      })
    }, []),
  })

  useEffect(() => {
    api.get<ServerStatus>('/policy-server/status').then((s) => {
      setStatus(s)
      setPsState(s.state as ProcessState)
      if (s.host) setHost(s.host)
      if (s.port) setPort(s.port)
      if (s.fps) setFps(s.fps)
    }).catch(() => {})
  }, [])

  useEffect(() => { localStorage.setItem('ps_mode', mode) }, [mode])
  useEffect(() => { localStorage.setItem('ps_remote_addr', remoteAddress) }, [remoteAddress])

  const isRunning = psState === 'running' || psState === 'starting'

  const handleStart = async () => {
    try { await api.post('/policy-server/start', { host, port, fps }) }
    catch (e) { setLogs((prev) => [...prev, `[ERROR] ${e instanceof Error ? e.message : '시작 실패'}`]) }
  }

  const handleStop = async () => { await api.post('/policy-server/stop') }

  const handleCheckRemote = async () => {
    setChecking(true)
    setRemoteCheck(null)
    try {
      const result = await api.post<RemoteCheck>('/policy-server/check-remote', { address: remoteAddress })
      setRemoteCheck(result)
    } catch {
      setRemoteCheck({ reachable: false, address: remoteAddress, error: '확인 실패' })
    } finally {
      setChecking(false)
    }
  }

  // 추론 페이지에서 사용할 서버 주소
  const activeAddress = mode === 'local' ? `${host}:${port}` : remoteAddress

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">정책 서버</h1>
        <div className="flex items-center gap-3">
          <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
          {mode === 'local' && (
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${
              isRunning ? 'bg-green-500/20 text-green-400' : 'bg-neutral-700 text-neutral-400'
            }`}>{psState}</span>
          )}
          {mode === 'remote' && remoteCheck && (
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${
              remoteCheck.reachable ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'
            }`}>{remoteCheck.reachable ? '연결됨' : '연결 불가'}</span>
          )}
        </div>
      </div>

      {/* 모드 선택 */}
      <div className="flex gap-4 text-sm">
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input type="radio" name="psMode" value="local" checked={mode === 'local'}
            onChange={() => setMode('local')} className="accent-blue-500" />
          로컬 서버 (이 PC에서 실행)
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input type="radio" name="psMode" value="remote" checked={mode === 'remote'}
            onChange={() => setMode('remote')} className="accent-blue-500" />
          원격 서버 (다른 PC)
        </label>
      </div>

      {mode === 'local' ? (
        /* ── 로컬 모드 ── */
        <div className="grid grid-cols-1 lg:grid-cols-[1fr_2fr] gap-6">
          <div className="space-y-4">
            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
              <h3 className="text-sm font-semibold">로컬 서버 설정</h3>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="text-xs text-neutral-400">Host</label>
                  <input type="text" value={host} onChange={(e) => setHost(e.target.value)} disabled={isRunning}
                    className="w-full px-2 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 disabled:opacity-50" />
                </div>
                <div>
                  <label className="text-xs text-neutral-400">Port</label>
                  <input type="number" value={port} onChange={(e) => setPort(Number(e.target.value))} disabled={isRunning}
                    className="w-full px-2 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 disabled:opacity-50" />
                </div>
                <div>
                  <label className="text-xs text-neutral-400">FPS</label>
                  <input type="number" value={fps} min={1} max={60} onChange={(e) => setFps(Number(e.target.value))} disabled={isRunning}
                    className="w-full px-2 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 disabled:opacity-50" />
                </div>
              </div>

              {isRunning && (
                <div className="rounded bg-green-500/10 border border-green-500/30 px-3 py-2 text-xs text-green-400">
                  서버 주소: <span className="font-mono font-medium">{activeAddress}</span>
                  {status?.pid && <span className="ml-2 text-neutral-500">PID: {status.pid}</span>}
                </div>
              )}

              {!isRunning ? (
                <button onClick={handleStart}
                  className="w-full px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium">
                  서버 시작
                </button>
              ) : (
                <button onClick={handleStop}
                  className="w-full px-4 py-2 rounded bg-red-600 hover:bg-red-500 text-white text-sm font-medium">
                  서버 정지
                </button>
              )}
            </div>

            <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 text-xs text-neutral-400 space-y-1">
              <p>gRPC 정책 서버 (lerobot async_inference)</p>
              <p>모델 캐싱: 같은 모델은 재로딩 없이 재사용</p>
            </div>
          </div>

          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4">
            <h3 className="text-sm font-semibold mb-2">서버 로그</h3>
            <LogViewer logs={logs} onClear={() => setLogs([])} />
          </div>
        </div>
      ) : (
        /* ── 원격 모드 ── */
        <div className="max-w-lg space-y-4">
          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 space-y-3">
            <h3 className="text-sm font-semibold">원격 서버 연결</h3>
            <div>
              <label className="text-xs text-neutral-400">서버 주소 (host:port)</label>
              <div className="flex gap-2 mt-1">
                <input type="text" value={remoteAddress}
                  onChange={(e) => { setRemoteAddress(e.target.value); setRemoteCheck(null) }}
                  placeholder="192.168.1.100:8088"
                  className="flex-1 px-3 py-1.5 rounded bg-neutral-900 border border-neutral-700 text-sm text-neutral-100 font-mono" />
                <button onClick={handleCheckRemote} disabled={checking || !remoteAddress}
                  className="px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium">
                  {checking ? '확인 중...' : '연결 확인'}
                </button>
              </div>
            </div>

            {remoteCheck && (
              <div className={`rounded px-3 py-2 text-xs ${
                remoteCheck.reachable
                  ? 'bg-green-500/10 border border-green-500/30 text-green-400'
                  : 'bg-red-500/10 border border-red-500/30 text-red-400'
              }`}>
                {remoteCheck.reachable ? (
                  <>
                    <span className="font-medium">연결 성공</span>
                    <span className="ml-2 font-mono">{remoteCheck.address}</span>
                  </>
                ) : (
                  <>
                    <span className="font-medium">연결 실패</span>
                    {remoteCheck.error && <span className="ml-2">{remoteCheck.error}</span>}
                  </>
                )}
              </div>
            )}
          </div>

          <div className="rounded-lg border border-neutral-700 bg-neutral-800 p-4 text-xs text-neutral-400 space-y-1">
            <p>원격 PC에서 정책 서버를 직접 실행해야 합니다:</p>
            <code className="block mt-1 px-2 py-1 rounded bg-neutral-900 text-neutral-300 font-mono text-[11px]">
              python wrapper/start_policy_server.py --host=0.0.0.0 --port=8088 --fps=30
            </code>
            <p className="pt-2">추론 페이지에서 서버 주소에 <span className="font-mono text-neutral-300">{remoteAddress}</span>를 입력하세요.</p>
          </div>
        </div>
      )}
    </div>
  )
}
