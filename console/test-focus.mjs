// Headless check of the hunter channel (no window on screen), through the Chrome DevTools protocol:
// the Enter / ` / / / Esc focus model (Enter ×10 from closed, 10 Esc→Enter cycles, a click, an SSE diff)
// and the «thinking» loader. Run against a DRY-RUN bridge (actions are logged, never sent):
//   python3 -I console/server.py --root <genius-ai> --port 4422 --dry-run &
//   bin/heavy console-test -- zsh -c '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
//     --remote-debugging-port=9333 --user-data-dir=$(mktemp -d) --enable-unsafe-swiftshader about:blank & c=$!;
//     node console/test-focus.mjs 9333 http://127.0.0.1:4422/ /tmp; r=$?; kill $c; exit $r'
// Exit code 1 when a focus check fails. Stop the bridge afterwards.
const [,, port, url, out] = process.argv;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ver; for (let i = 0; i < 40; i++) { try { ver = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json(); break; } catch { await sleep(250); } }
const t = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent('about:blank')}`, { method: 'PUT' })).json();
const ws = new WebSocket(t.webSocketDebuggerUrl); await new Promise((r) => ws.onopen = r);
let id = 0; const wait = new Map();
ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && wait.has(d.id)) { wait.get(d.id)(d); wait.delete(d.id); } if (d.method === 'Runtime.exceptionThrown') console.log('EXC', JSON.stringify(d.params.exceptionDetails).slice(0, 400)); if (d.method === 'Runtime.consoleAPICalled' && d.params.type === 'error') console.log('CONSOLE', JSON.stringify(d.params.args).slice(0, 300)); };
const cmd = (method, params = {}) => new Promise((r) => { const i = ++id; wait.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr) => { const r = (await cmd('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })).result; return r?.exceptionDetails ? 'EXC ' + JSON.stringify(r.exceptionDetails).slice(0, 300) : r?.result?.value; };
const key = async (k, code, text) => { await cmd('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code, text, windowsVirtualKeyCode: { Backquote: 192, Enter: 13, Slash: 191, PageDown: 34, PageUp: 33, Escape: 27, KeyH: 72 }[code] || 0 }); await cmd('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code }); };
const shot = async (name) => { const r = await cmd('Page.captureScreenshot', { format: 'png' }); (await import('fs')).writeFileSync(`${out}/${name}.png`, Buffer.from(r.result.data, 'base64')); };
await cmd('Runtime.enable'); await cmd('Page.enable');
await cmd('Emulation.setDeviceMetricsOverride', { width: 1512, height: 900, deviceScaleFactor: 1, mobile: false });
await cmd('Emulation.setFocusEmulationEnabled', { enabled: true });
await cmd('Page.navigate', { url }); await sleep(6000);
const B = new URL('app.js', url).href;
const S = async () => JSON.parse(await ev("JSON.stringify({chan: hunterConsole.info().chan, focused: document.activeElement.id === 'cmd'})"));
const fails = []; let checks = 0;
const check = (label, got, want) => { checks++; if (got.chan !== want.chan || got.focused !== want.focused) fails.push(label + ' ' + JSON.stringify(got)); };
const ENTER = () => key('Enter', 'Enter', '\r');
await key('Escape', 'Escape'); await key('Escape', 'Escape'); await sleep(400);
check('start', await S(), { chan: 0, focused: false });
// A: Enter ×10 in a row from the closed state: the first opens and focuses, the rest keep the focus
for (let k = 0; k < 10; k++) { await ENTER(); await sleep(150); check('A' + k, await S(), { chan: 1, focused: true }); }
// B: 10 cycles Esc (empty line: closes) → Enter (opens and focuses)
for (let k = 0; k < 10; k++) {
  await key('Escape', 'Escape'); await sleep(150); check('B-esc' + k, await S(), { chan: 0, focused: false });
  await ENTER(); await sleep(150); check('B-enter' + k, await S(), { chan: 1, focused: true });
}
// C: ` and / from the closed state
for (const [k, c] of [['`', 'Backquote'], ['/', 'Slash']]) {
  await key('Escape', 'Escape'); await sleep(150); await key(k, c, k); await sleep(150);
  check('C' + c, await S(), { chan: 1, focused: true });
  check('C-char' + c, { chan: 1, focused: (await ev("document.getElementById('cmd').value")) === '' }, { chan: 1, focused: true });
}
// D: a click on the panel focuses the line
await ev("document.getElementById('cmd').blur(), 1"); await sleep(100);
await ev("(document.querySelector('#conv .c-rep') || document.querySelector('#conv .c-empty') || document.getElementById('conv')).click(), 1"); await sleep(150);
check('D click', await S(), { chan: 1, focused: true });
// E: an SSE diff and a new reply re-render the panel: the focus stays
await ev(`import('${B}').then(({store}) => { store.emit({agents: store.s.agents}); return 1; })`); await sleep(500);
check('E diff', await S(), { chan: 1, focused: true });
console.log(`focus: ${checks - fails.length}/${checks} passed`, fails.slice(0, 6));
// F: the thinking loader
await ev("document.getElementById('cmd').value = 'test loader', document.getElementById('cmd').dispatchEvent(new Event('input')), 1");
await ENTER(); await sleep(3500);
console.log('thinking', await ev("JSON.stringify({think: !!document.querySelector('#conv .c-think'), head: (document.querySelector('#conv .c-think .c-who')||{}).textContent, stream: ((document.querySelector('#conv .c-stream')||{}).textContent||'').slice(0,30), doing: (document.querySelector('#conv .c-doing')||{}).textContent, ring: hunterConsole.info().chan})"));
console.log('activity', await ev(`import('${B}').then(({store}) => JSON.stringify(store.s.coordinator_activity))`));
await shot('thinking');
await ev(`import('${B}').then(({store}) => { store.s.reply = {at: '2026-11-18T12:30:00+02:00', id: 'test-loader-reply', text: ['Готово: **лоадер** працює.', '', '🔊 Лоадер працює.'].join(String.fromCharCode(10))}; store.emit({reply: store.s.reply}); return 1; })`);
await sleep(2500);
console.log('after reply', await ev("JSON.stringify({think: !!document.querySelector('#conv .c-think'), head: (document.querySelector('#conv .c-rep .c-who')||{}).textContent, focused: document.activeElement.id})"));
await shot('loader-reply');
console.log('dry-log', await ev("fetch('/api/dry-log').then(r => r.json()).then(j => JSON.stringify(j.commands).slice(0, 220))"));
ws.close(); process.exit(fails.length ? 1 : 0);
