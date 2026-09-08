"""Piper MuJoCo 시뮬레이션 — simd 의 코어 (feature/sim-env.md).

시뮬레이터는 "또 하나의 로봇 데몬"이다: robotd 와 같은 shm 계약을 발행하므로
수집·학습·추론·E-stop·감시가 무수정으로 시뮬 위에서 돈다.
"""
