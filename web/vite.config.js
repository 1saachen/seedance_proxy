import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const backendOrigin = process.env.SEEDANCE_BACKEND_ORIGIN || 'http://127.0.0.1:8000';
const proxy = { '/api': backendOrigin, '/v1': backendOrigin };

function searchMetadata() {
  const configuredOrigin = process.env.SEEDANCE_SITE_ORIGIN;
  const origin = configuredOrigin ? new URL(configuredOrigin) : null;
  if (origin && (origin.protocol !== 'https:' || origin.href !== `${origin.origin}/`)) {
    throw new Error('SEEDANCE_SITE_ORIGIN must be a public HTTPS origin');
  }

  return {
    name: 'seedance-search-metadata',
    transformIndexHtml(_html, context) {
      if (context.path !== '/index.html' && context.path !== '/') return [];
      if (!origin || context.server) {
        return [{ tag: 'meta', attrs: { name: 'robots', content: 'noindex' } }];
      }
      return [
        { tag: 'link', attrs: { rel: 'canonical', href: origin.href } },
        { tag: 'meta', attrs: { property: 'og:url', content: origin.href } },
      ];
    },
    generateBundle() {
      const rules = origin
        ? `User-agent: *\nDisallow: /api/\nDisallow: /v1/\nDisallow: /health\nSitemap: ${origin.origin}/sitemap.xml\n`
        : 'User-agent: *\nDisallow: /\n';
      this.emitFile({ type: 'asset', fileName: 'robots.txt', source: rules });
      if (origin) {
        const href = origin.href.replaceAll('&', '&amp;');
        this.emitFile({
          type: 'asset',
          fileName: 'sitemap.xml',
          source: `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>${href}</loc></url></urlset>\n`,
        });
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), searchMetadata()],
  server: { proxy },
  preview: { proxy },
  build: {
    rolldownOptions: {
      input: {
        home: fileURLToPath(new URL('./index.html', import.meta.url)),
        console: fileURLToPath(new URL('./console/index.html', import.meta.url)),
      },
    },
  },
});
