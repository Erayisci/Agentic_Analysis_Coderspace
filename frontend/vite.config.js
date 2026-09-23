import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Local dev: the backend runs on the same machine, so 127.0.0.1 is correct.
// In Docker Compose the frontend and backend are separate containers, and
// 127.0.0.1 inside one container never reaches another -- VITE_API_PROXY_TARGET
// (set to http://backend:8000 in docker-compose.yml, the service name Compose's
// own DNS resolves) overrides it without changing the local-dev default.
const apiTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    proxy: Object.fromEntries(
      ['/ask', '/health', '/session', '/debug'].map((path) => [path, apiTarget]),
    ),
  },
})
