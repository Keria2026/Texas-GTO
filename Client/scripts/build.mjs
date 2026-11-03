import { build } from 'esbuild';
import { mkdir, cp, writeFile, rm, readFile, readdir } from 'node:fs/promises';
import { resolve, dirname, join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { backendConfig, fixtureBackendConfig } from './backend-config.mjs';
const fixture = process.argv.includes('--fixture');
if (!fixture && process.env.TGTO_FIXTURE_BACKEND_PORT !== undefined)
  throw Error('TGTO_FIXTURE_BACKEND_PORT is only allowed with --fixture');
const backend = fixture ? fixtureBackendConfig(process.env.TGTO_FIXTURE_BACKEND_PORT) : backendConfig(process.env.TGTO_BACKEND_URL);
const outdir = resolve(fixture ? 'dist-fixture' : 'dist');
await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });
await build({ entryPoints: { background: 'extension/background.ts', sidepanel: 'extension/sidepanel.tsx' },
  outdir, bundle: true, format: 'esm', platform: 'browser', target: 'chrome125', jsx: 'automatic',
  define: { __FIXTURE__: String(fixture), __BACKEND_URLS__: JSON.stringify(backend.urls), 'process.env.NODE_ENV': '"production"' }, minify: true });
await cp('extension/sidepanel.html', `${outdir}/sidepanel.html`);
await cp('target/wasm32-unknown-unknown/release/tgto_capture_core.wasm', `${outdir}/capture_core.wasm`);
await cp('LICENSE', `${outdir}/LICENSE.txt`);
const notices = await Promise.all(['react', 'react-dom', 'scheduler'].map(async name =>
  `${name}\n${await readFile(`node_modules/${name}/LICENSE`, 'utf8')}`));
await writeFile(`${outdir}/THIRD_PARTY_NOTICES.txt`, notices.join('\n\n'));
const metadata = JSON.parse(execFileSync('cargo', ['metadata', '--locked', '--offline', '--format-version=1'], { encoding: 'utf8' }));
const rustNotices = [];
for (const pkg of metadata.packages.filter(p => p.source)) {
  const dir = dirname(pkg.manifest_path);
  const names = (await readdir(dir)).filter(name => /^(LICENSE|COPYING|UNLICENSE)([-.]|$)/i.test(name));
  rustNotices.push(`${pkg.name} ${pkg.version} (${pkg.license})\n${(await Promise.all(names.map(name => readFile(join(dir, name), 'utf8')))).join('\n')}`);
}
await writeFile(`${outdir}/THIRD_PARTY_RUST_NOTICES.txt`, rustNotices.join('\n\n'));
const manifest = { manifest_version: 3, name: fixture ? 'TGTO Capture — SYNTHETIC TEST ONLY' : 'TGTO Research Capture',
  version_name: 'Pr1.0',
  version: JSON.parse(await readFile('package.json', 'utf8')).version, minimum_chrome_version: '125', permissions: ['debugger', 'storage', 'sidePanel', 'alarms'],
  host_permissions: ['https://h5.wpk.com/*', 'http://127.0.0.1/*', ...backend.permissions],
  background: { service_worker: 'background.js', type: 'module' }, side_panel: { default_path: 'sidepanel.html' },
  action: { default_title: '打开 TGTO 采集' },
  content_security_policy: { extension_pages: `script-src 'self' 'wasm-unsafe-eval'; object-src 'none'; connect-src 'self' ${backend.connect.join(' ')}` } };
await writeFile(`${outdir}/manifest.json`, `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`Built ${outdir}${fixture ? ' (synthetic targets enabled)' : ''}`);
