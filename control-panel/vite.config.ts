import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  base: process.env.VITE_BASE_URL ?? '/',
  server: {
    proxy: {
      '/aigateway': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
