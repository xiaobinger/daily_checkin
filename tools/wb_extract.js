// 启动 WorkBuddy（--inspect），连接主进程 inspector，读取 at-rest 密钥载荷。
// 用法：先完全退出 WorkBuddy 桌面端，然后 node tools/wb_extract.js
// 输出的 JSON 里 atRestSecretKey 交给 python tools/wb_cache_key.py 缓存。
const { spawn } = require('child_process');

const EXE = 'D:\\WorkBuddy\\WorkBuddy.exe';
const EXPR = process.argv[2] ||
  "process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()";

const child = spawn(EXE, ['--inspect=9229'], { stdio: ['ignore', 'pipe', 'pipe'] });
let buf = '', done = false, connected = false, ws = null, nextId = 10, tries = 0;

function finish(code) {
  if (done) return;
  done = true;
  setTimeout(() => { try { child.kill(); } catch {} process.exit(code); }, 200);
}

function evaluate() {
  tries++;
  const id = nextId++;
  ws.send(JSON.stringify({
    id, method: 'Runtime.evaluate',
    params: { expression: EXPR, returnByValue: true, timeout: 15000 },
  }));
  pending.set(id, tries);
}

const pending = new Map();

function tryConnect(wsUrl) {
  if (connected || done) return;
  connected = true;
  ws = new WebSocket(wsUrl);
  ws.onopen = () => {
    ws.send(JSON.stringify({ id: 1, method: 'Runtime.enable' }));
    setTimeout(evaluate, 4000);
  };
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (!pending.has(msg.id)) return;
    pending.delete(msg.id);
    if (msg.error) {
      console.error(`try#${pending.size || tries}: ${msg.error.message}`);
      if (tries < 8) setTimeout(evaluate, 3000); else finish(3);
      return;
    }
    const r = msg.result || {};
    if (r.exceptionDetails) {
      console.error('EXC ' + JSON.stringify(r.exceptionDetails).slice(0, 700));
      if (tries < 8) setTimeout(evaluate, 3000); else finish(4);
      return;
    }
    const v = (r.result || {}).value;
    console.log(typeof v === 'string' ? v : JSON.stringify(v));
    finish(0);
  };
  ws.onerror = e => console.error('WSERR ' + (e.message || e));
  ws.onclose = () => { if (!done) finish(5); };
}

child.stderr.on('data', d => {
  buf += d.toString();
  const m = buf.match(/Debugger listening on (ws:\/\/\S+)/);
  if (m) tryConnect(m[1]);
});

setTimeout(() => { if (!done) { console.error('TIMEOUT'); finish(7); } }, 60000);