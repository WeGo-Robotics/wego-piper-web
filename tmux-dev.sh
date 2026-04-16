#!/usr/bin/env bash
# tmux로 백엔드 + 프론트엔드 동시 실행
# 사용법: ./tmux-dev.sh
# 종료: tmux kill-session -t piper

SESSION="piper"
ROOT="$(cd "$(dirname "$0")" && pwd)"

# 이미 실행 중이면 붙기
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' already running. Attaching..."
  exec tmux attach -t "$SESSION"
fi

# ┌─────────┬─────────┐
# │ backend │frontend │
# ├─────────┤         │
# │  shell  │         │
# └─────────┴─────────┘

# 새 세션: 좌측 전체 (backend → 나중에 세로 분할)
tmux new-session -d -s "$SESSION" -c "$ROOT/backend"

# 우측: frontend (가로 분할)
tmux split-window -h -t "$SESSION" -c "$ROOT/frontend" -l 50%
FRONTEND_PANE=$(tmux list-panes -t "$SESSION" -F '#{pane_id}' | tail -1)
tmux send-keys -t "$FRONTEND_PANE" "npm run dev" Enter

# 좌측을 세로 분할 → 상단 backend, 하단 shell
BACKEND_PANE=$(tmux list-panes -t "$SESSION" -F '#{pane_id}' | head -1)
tmux split-window -v -t "$BACKEND_PANE" -c "$ROOT" -l 30%
tmux send-keys -t "$BACKEND_PANE" "uvicorn app.main:app --reload --host 0.0.0.0 --port 8000" Enter

# shell pane에 포커스
SHELL_PANE=$(tmux list-panes -t "$SESSION" -F '#{pane_id}' | sed -n '2p')
tmux select-pane -t "$SHELL_PANE"

tmux attach -t "$SESSION"
