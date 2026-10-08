import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'evnt',
  tagline: 'A small collector for your own analytics infrastructure.',
  favicon: 'img/logo.svg',
  url: 'https://vladenisov.github.io',
  baseUrl: '/evnt/',
  organizationName: 'vladenisov',
  projectName: 'evnt',
  trailingSlash: false,
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  markdown: {format: 'detect', hooks: {onBrokenMarkdownLinks: 'throw'}},
  i18n: {defaultLocale: 'en', locales: ['en']},
  presets: [
    [
      'classic',
      {
        docs: {
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/vladenisov/evnt/tree/main/website/',
        },
        blog: false,
        theme: {customCss: './src/css/custom.css'},
      } satisfies Preset.Options,
    ],
  ],
  themeConfig: {
    navbar: {
      title: 'evnt',
      logo: {alt: 'evnt', src: 'img/logo.svg'},
      items: [
        {type: 'docSidebar', sidebarId: 'docsSidebar', position: 'left', label: 'Docs'},
        {to: '/integrate/http-api', label: 'HTTP API', position: 'left'},
        {href: 'https://github.com/vladenisov/evnt/releases', label: 'Releases', position: 'right'},
        {href: 'https://github.com/vladenisov/evnt', label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'dark',
      copyright: `Copyright © ${new Date().getFullYear()} evnt. BSD 3-Clause. Independent of Snowplow Analytics Ltd.`,
    },
    prism: {theme: prismThemes.github, darkTheme: prismThemes.dracula, additionalLanguages: ['bash', 'python', 'sql', 'json']},
    colorMode: {respectPrefersColorScheme: true},
  } satisfies Preset.ThemeConfig,
};

export default config;
