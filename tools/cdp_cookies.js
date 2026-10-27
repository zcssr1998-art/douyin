
const fs = require('fs');
(async () => {
  const list = await (await fetch('http://127.0.0.1:9222/json/list')).json();
  const page = list.find(t => t.type === 'page' && t.url.includes('douyin.com'));
  if (!page) { console.log('no page'); return; }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  const send = (m, p) => new Promise((res, rej) => { const mid = ++id; pending.set(mid, {res, rej}); ws.send(JSON.stringify({id: mid, method: m, params: p})); });
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } };
  await new Promise(r => ws.onopen = r);
  const r = await send('Network.getAllCookies', {});
  const ck = r.cookies || [];
  const dom = ck.filter(c => /douyin/.test(c.domain));
  fs.writeFileSync(process.argv[2], JSON.stringify(dom, null, 1), 'utf8');
  const names = dom.map(c => c.name);
  console.log(JSON.stringify({total: ck.length, douyin: dom.length, hasSession: names.includes('sessionid') || names.includes('sessionid_ss'), names: names.filter(n => /session|sid_|uid_|ttwid|passport/.test(n))}));
  ws.close();
})().catch(e => console.log('ERR', String(e)));
