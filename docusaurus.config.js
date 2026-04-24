// @ts-check
// Docusaurus configuration — customize the values marked with ← comments.

const { themes: prismThemes } = require('prism-react-renderer');

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'My Documentation Site',       // ← your site title
  tagline: 'Centralized service documentation',
  favicon: 'img/favicon.ico',

  url: 'https://your-org.github.io',    // ← your site URL (no trailing slash)
  baseUrl: '/your-repo-name/',           // ← your GitHub repo name, or '/' if custom domain

  organizationName: 'your-org',         // ← your GitHub org or username
  projectName: 'your-repo-name',        // ← your GitHub repo name

  onBrokenLinks: 'warn',
  onBrokenMarkdownLinks: 'warn',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          sidebarPath: require.resolve('./sidebars.js'),
          routeBasePath: '/',
        },
        blog: false,
        theme: {
          customCss: require.resolve('./src/css/custom.css'),
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      navbar: {
        title: 'My Docs',              // ← your site short name
        logo: {
          alt: 'My Docs Logo',
          src: 'img/logo.svg',
        },
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'servicesSidebar',
            position: 'left',
            label: 'Services',
          },
          {
            href: 'https://github.com/your-org/your-repo-name',  // ← your GitHub repo URL
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        copyright: `Copyright © ${new Date().getFullYear()} My Organization. Built with Docusaurus.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
      },
    }),
};

module.exports = config;
