import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const release = mode === 'release';

  return {
    base: release ? '/terminal/' : '/',
    build: {
      emptyOutDir: true,
      outDir: release ? '../src/live15_quant/terminal' : 'dist',
    },
    plugins: [react()],
    preview: { port: 4173 },
    server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8765' } },
  };
});
