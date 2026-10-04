# Sidebar change for `cAImanLabs-Documentation`

That repo's sidebar is written by hand, so synced pages would exist but never appear in the menu. Add one group to
`sidebar` in `astro.config.mjs` (Starlight 0.41 requires `autogenerate` inside `items`):

```js
{
	label: 'Projects',
	translations: { es: 'Proyectos' },
	items: [{ autogenerate: { directory: 'projects' } }],
},
```

Every page the pipeline writes under `src/content/docs/projects/<service>/` then shows up automatically.
