import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The built SPA is served by the Python package, so it is emitted straight
// into backend/prudify/static/. Relative asset paths keep it working under a
// reverse-proxy url_base without a rebuild.
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../backend/prudify/static',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8317', changeOrigin: true },
      '/ping': { target: 'http://localhost:8317', changeOrigin: true },
    },
  },
})
