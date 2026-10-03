// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Central documentation site. Synced service docs land in src/content/docs/services/<service>/
// and appear automatically via `autogenerate` (no sidebar edits needed after a sync).
export default defineConfig({
	// site: 'https://docs.example.com',   // <- set your public URL
	integrations: [
		starlight({
			title: 'My Documentation Site', // <- your site title
			customCss: ['./src/styles/custom.css'],
			locales: {
				root: { label: 'English', lang: 'en' },
			},
			sidebar: [
				{ label: 'Start here', items: [{ label: 'Welcome', slug: 'index' }] },
				{ label: 'Services', items: [{ autogenerate: { directory: 'services' } }] },
			],
		}),
	],
});
