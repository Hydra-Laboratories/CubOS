import { configDefaults, defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const DEV_PORT = 5173
const API_TARGET = 'http://127.0.0.1:8742'

// The API's CSRF guard rejects Origins other than its own host:port, and
// `changeOrigin` rewrites Host but not Origin/Referer. Rewrite only the dev
// server's own loopback origin (matched against the request's Host so
// `--port` overrides work); real cross-origin callers stay blocked.
const DEV_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]'])
const isDevServerOrigin = (origin?: string, host?: string): boolean => {
  if (!origin || !host || origin !== `http://${host}`) return false
  try {
    return DEV_HOSTNAMES.has(new URL(origin).hostname)
  } catch {
    return false
  }
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: DEV_PORT,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq, req) => {
            if (isDevServerOrigin(req.headers.origin, req.headers.host)) {
              proxyReq.setHeader('origin', API_TARGET)
              proxyReq.removeHeader('referer')
            }
          })
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    // Playwright owns tests/e2e (run via `npm run test:e2e`); vitest must
    // not pick those specs up.
    exclude: [...configDefaults.exclude, 'tests/e2e/**'],
  },
})
