#!/usr/bin/env bash
# tmux piper 세션 재시작
# 사용법: ./tmux-restart.sh

SESSION="piper"

tmux kill-session -t "$SESSION" 2>/dev/null
sleep 0.3
exec ./tmux-dev.sh
