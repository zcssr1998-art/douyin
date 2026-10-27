
// minimal CDP eval helper: node cdp_eval.js <file-with-js-expression>
const fs = require('fs');
const arg = process.argv[2];
const expr = fs.existsSync(arg) ? fs.readFileSync(arg, 'utf8') : arg;
(async () => {
  const list = await (await fetch('http://127.0.0.1:9222/json/list')).json();
  const page = list.find(t => t.type === 'page' && t.url.includes('douyin.com'));
  if (!page) { console.log(JSON.stringify({error:'no douyin page'})); return; }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  const send = (method, params) => new Promise((res, rej) => { const mid = ++id; pending.set(mid, {res, rej}); ws.send(JSON.stringify({id: mid, method, params})); });
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } };
  await new Promise(r => ws.onopen = r);
  const r = await send('Runtime.evaluate', {expression: expr, returnByValue: true, awaitPromise: true});
  const out = r.exceptionDetails ? {EXC: r.exceptionDetails.exception && r.exceptionDetails.exception.description} : r.result.value;
  console.log(JSON.stringify(out, null, 1));
  ws.close();
})().catch(e => console.log(JSON.stringify({error:String(e)})));
