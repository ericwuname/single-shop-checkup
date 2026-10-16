import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// M1 要"手机 10 秒录入"，所以开发服务器必须监听 0.0.0.0，
// 手机连同一个 Wi-Fi 就能打开 http://<电脑局域网IP>:5173
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
