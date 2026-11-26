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

  const publicRoutes = new Map([
    ['/', '/'], ['/index.html', '/'],
    ['/docs/', '/docs/'], ['/docs/index.html', '/docs/'],
  ]);
  return {
    name: 'seedance-search-metadata',
    transformIndexHtml(_html, context) {
      const route = publicRoutes.get(context.path);
      if (!route) return [];
      if (!origin || context.server) {
        return [{ tag: 'meta', attrs: { name: 'robots', content: 'noindex' } }];
      }
      const target = new URL(route, origin).href;
      return [
        { tag: 'link', attrs: { rel: 'canonical', href: target } },
        { tag: 'meta', attrs: { property: 'og:url', content: target } },
      ];
    },
    generateBundle() {
      const rules = origin
        ? `User-agent: *\nDisallow: /api/\nDisallow: /v1/\nDisallow: /health\nSitemap: ${origin.origin}/sitemap.xml\n`
        : 'User-agent: *\nDisallow: /\n';
      this.emitFile({ type: 'asset', fileName: 'robots.txt', source: rules });
      if (origin) {
        const urls = [...new Set(publicRoutes.values())].map((route) =>
          `<url><loc>${new URL(route, origin).href.replaceAll('&', '&amp;')}</loc></url>`,
        ).join('');
        this.emitFile({
          type: 'asset',
          fileName: 'sitemap.xml',
          source: `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls}</urlset>\n`,
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
        docs: fileURLToPath(new URL('./docs/index.html', import.meta.url)),
      },
    },
  },
});
