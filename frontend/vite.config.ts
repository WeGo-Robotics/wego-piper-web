import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    // ⚠ 파일 변경을 **폴링으로** 감지한다.
    //
    // 기본 감시자(inotify)가 변경을 놓쳐 vite 가 **낡은 변환 결과를 계속 서빙**하는
    // 일이 반복됐다. 브라우저는 옛 코드를 돌리는데 디스크·빌드는 최신이라
    // "고쳤는데 화면이 그대로"가 되고, 원인을 찾는 데 시간이 든다.
    // (에디터가 아니라 스크립트로 파일을 통째로 다시 쓸 때 특히 잘 놓친다.)
    //
    // 폴링은 CPU 를 조금 더 쓰지만, 조용히 옛 코드를 돌리는 대가가 훨씬 크다.
    watch: { usePolling: true, interval: 300 },
    proxy: {
      '/api': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
})
