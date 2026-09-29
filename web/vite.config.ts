import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Бэкенд FastAPI (поток A). В разработке: `npm run dev` проксирует /api и /health на него.
const API_TARGET = process.env.INSPECTOR_API ?? 'http://localhost:8080'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: API_TARGET },
      '/health': { target: API_TARGET },
      '/metrics': { target: API_TARGET },
    },
  },
  preview: {
    proxy: {
      '/api': { target: API_TARGET },
      '/health': { target: API_TARGET },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
})
