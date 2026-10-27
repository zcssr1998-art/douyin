// probe a douyin detail page in an existing logged-in Chrome (CDP 9222)
const fs = require('fs');
const [port, url, outFile, waitMs] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const EXPR = "JSON.stringify({title: document.title, videos: [...document.querySelectorAll('video')].map(v=>({src:v.src, cur:v.currentSrc, dur:v.duration})), sources: [...document.querySelectorAll('source')].map(s=>s.src), htmlHasPlay: /aweme\\/v1\\/play/.test(document.documentElement.outerHTML), htmlLen: document.documentElement.outerHTML.length})";
(async () => {
  const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  const page = list.find(t => t.type === 'page' && t.url.includes('douyin')) || list.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  const send = (m, p) => new Promise((res, rej) => { const mid = ++id; pending.set(mid, {res, rej}); ws.send(JSON.stringify({id: mid, method: m, params: p || {}})); });
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } };
  await new Promise(r => { ws.onopen = r; });
  await send('Page.enable');
  await send('Page.navigate', {url});
  const out = [];
  const n = Number(waitMs || 15000);
  for (let i = 0; i < n / 2500; i++) {
    await sleep(2500);
    try { const ev = await send('Runtime.evaluate', {expression: EXPR, returnByValue: true}); out.push(JSON.parse(ev.result.value)); } catch (e) {}
  }
  const htmlRes = await send('Runtime.evaluate', {expression: 'document.documentElement.outerHTML', returnByValue: true});
  if (outFile) fs.writeFileSync(outFile, htmlRes.result.value || '', 'utf8');
  console.log(JSON.stringify({samples: out}, null, 1).slice(0, 3000));
  ws.close();
})().catch(e => console.log('ERR ' + e));