import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:net';
import { mkdtemp } from 'node:fs/promises';
import assert from 'node:assert/strict';

const root = fileURLToPath(new URL('../../', import.meta.url));
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.OMARKETS_PLAYWRIGHT_MODULE || 'playwright-core');
const tmp = await mkdtemp(`${process.env.TMPDIR || '/tmp'}/phase2-browser-`);
const children = [], logs = [];
let browser;
async function port() {
  const server = createServer().listen(0, '127.0.0.1');
  await once(server, 'listening');
  const value = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return value;
}
function start(command, args, options = {}) {
  const child = spawn(command, args, { cwd: root, stdio: ['ignore', 'pipe', 'pipe'], ...options });
  children.push(child);
  child.stdout.on('data', data => logs.push(String(data)));
  child.stderr.on('data', data => logs.push(String(data)));
  return child;
}
async function ready(url) {
  for (let attempt = 0; attempt < 200; attempt++) {
    try { if ((await fetch(url)).ok) return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`Not ready: ${url}`);
}
try {
  const backendPort = await port(), frontendPort = await port();
  const backend = `http://127.0.0.1:${backendPort}`;
  start(`${root}/.venv/bin/python`, ['-m', 'tests.api.browser_backend', String(backendPort), tmp], { env: { PATH: process.env.PATH, PYTHONPATH: root } });
  await ready(`${backend}/health`);
  assert((await fetch(`${backend}/test/stage/0`, { method: 'POST' })).ok);
  start(process.execPath, [`${root}/web/node_modules/vite/bin/vite.js`, '--host', '127.0.0.1', '--port', String(frontendPort)], { cwd: `${root}/web`, env: { PATH: process.env.PATH, HOME: tmp, OMARKETS_BACKEND_TARGET: backend } });
  const origin = `http://127.0.0.1:${frontendPort}`;
  await ready(origin);
  browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome-stable', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, timezoneId: 'UTC' });
  const errors = [], optional503s = [], generic503Errors = [], sockets = [], messages = [];
  page.on('pageerror', error => errors.push(error.message));
  // Optional services are deliberately absent in this Phase 2 fixture.
  const optionalPaths = /^\/api\/v1\/(technical(?:\/|$)|cross-market|events(?:\/|$)|news(?:\/|$)|decisions(?:\/|$)|escalations(?:\/|$)|ai(?:\/|$)|analysts(?:\/|$)|analyses(?:\/|$)|briefings(?:\/|$)|workflow(?:\/|$)|historical(?:\/|$)|research(?:\/|$))/;
  page.on('response', response => {
    if (response.status() === 503 && optionalPaths.test(new URL(response.url()).pathname)) optional503s.push(response.url());
  });
  page.on('console', message => {
    if (message.type() !== 'error') return;
    const url = message.location().url;
    if (url && optionalPaths.test(new URL(url).pathname) && message.text().includes('503 (Service Unavailable)')) return;
    // Chromium's generic resource error points to the document, not the
    // failed request. Reconcile it against observed optional 503 responses.
    if (message.text() === 'Failed to load resource: the server responded with a status of 503 (Service Unavailable)') {
      generic503Errors.push(message.text());
      return;
    }
    errors.push(message.text());
  });
  page.on('websocket', socket => {
    if (!socket.url().includes('/ws/market')) return;
    sockets.push(socket.url());
    socket.on('framereceived', frame => messages.push(JSON.parse(String(frame.payload)).type));
  });
  await page.clock.setFixedTime(new Date('2026-09-17T12:00:00Z'));
  await page.goto(origin);
  await page.getByRole('heading', { name: 'DEMO CPI release', exact: true }).waitFor();
  await page.getByRole('navigation').getByRole('button', { name: 'Calendar', exact: true }).click();
  await page.getByText('scheduled · demo', { exact: true }).waitFor();
  // The client negotiates unavailable optional topics before retaining its
  // market+macro socket. Wait for the actual state frame, not just REST paint.
  for (let attempt = 0; !messages.includes('state') && attempt < 1800; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert(messages.includes('state'));
  const negotiatedSockets = sockets.length;
  assert.equal(await page.locator('.event-numbers dd').first().innerText(), '—');
  for (const [seconds, actual, revision, kind] of [[30, '0.2', 2, 'economic_event_released'], [60, '0.3', 3, 'economic_event_revised']]) {
    const result = await (await fetch(`${backend}/test/stage/${seconds}`, { method: 'POST' })).json();
    assert.equal(result.subscribers, 1);
    await page.getByRole('button', { name: `Revision history · ${revision}`, exact: true }).waitFor();
    assert.equal(await page.locator('.event-numbers dd').first().innerText(), actual);
    assert(messages.includes(kind));
    assert.equal(sockets.length, negotiatedSockets);
  }
  await page.getByRole('button', { name: 'Revision history · 3', exact: true }).click();
  await page.getByText('Revision 1', { exact: true }).waitFor();
  await page.getByText('Revision 2', { exact: true }).waitFor();
  await page.getByText('Revision 3', { exact: true }).waitFor();
  const history = await (await fetch(`${backend}/api/v1/calendar/demo:cpi:2026-08`)).json();
  assert.deepEqual(history.versions.map(item => item.actual), [null, 0.2, 0.3]);
  await page.getByRole('navigation').getByRole('button', { name: 'Macro', exact: true }).click();
  await page.getByRole('heading', { name: 'DEMO CPI', exact: true }).waitFor();
  assert.match(await page.locator('.macro-series').innerText(), /0\.3/);
  assert.equal(sockets.length, negotiatedSockets);
  assert(generic503Errors.length <= optional503s.length, 'unexpected 503 console errors');
  assert.deepEqual(errors, []);
  if (process.argv.includes('--contracts')) {
    const command = ['run', 'test:live'];
    console.log(`RUN OMARKETS_TEST_BACKEND=${backend} npm ${command.join(' ')} (cwd=web)`);
    const contracts = spawn('npm', command, { cwd: `${root}/web`, stdio: 'inherit', env: { ...process.env, OMARKETS_TEST_BACKEND: backend } });
    const [code] = await once(contracts, 'exit');
    assert.equal(code, 0);
  }
  console.log(JSON.stringify({ result: 'PASS', stages: ['scheduled', 'released', 'revised'], sockets: sockets.length, messages, errors, isolatedDatabases: tmp }));
} catch (error) {
  console.error(error);
  console.error(logs.join(''));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  for (const child of children.reverse()) {
    if (child.exitCode !== null || child.signalCode !== null) continue;
    const exit = once(child, 'exit');
    child.kill('SIGTERM');
    const timeout = setTimeout(() => child.kill('SIGKILL'), 5000);
    await exit;
    clearTimeout(timeout);
  }
}
