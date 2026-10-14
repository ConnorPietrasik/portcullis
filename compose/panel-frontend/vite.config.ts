import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // Dev loop: `PANEL_DEV=1 uvicorn main:app --port 8010` in panel-api/ on the
    // server, then `npm run dev` here — /api is proxied, no deploy needed.
    proxy: {
      '/api': {
        target: 'http://localhost:8010',
        changeOrigin: true,
      },
    },
  },
})
