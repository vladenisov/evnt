import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docsSidebar: [
    'index',
    'quickstart',
    {type: 'category', label: 'Integrate', items: ['integrate/trackers', 'integrate/http-api', 'integrate/encryption']},
    {type: 'category', label: 'Run', items: ['run/docker', 'run/queue', 'run/demo', 'run/observability', 'run/upgrading', 'run/documentation']},
    {type: 'category', label: 'Configure', items: ['configure/settings', 'configure/security', 'configure/performance', 'configure/proxy']},
    'architecture',
    'contributing',
    {type: 'link', label: 'Releases', href: 'https://github.com/vladenisov/evnt/releases'},
    'license',
  ],
};

export default sidebars;
