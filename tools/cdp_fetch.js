// CDP: inject cookies then dump page HTML. usage: node cdp_fetch.js <port> <cookies.json> <url> <out.html> [settleMs]
const fs = require('fs');
const [port, cookiesFile, url, outFile, settleArg] = process.argv.slice(2);
const settleMs = Number(settleArg || 9000);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const PROBE = "JSON.stringify({links: document.querySelectorAll('[data-e2e=\"user-post-list\"] a[href*=\"/video/\"]').length, all: new Set([...document.querySelectorAll('a[href*=\"/video/\"]')].map(a=>a.href)).size, wall: /登录后查看|扫码登录/.test(document.body.innerText.slice(0,4000)), title: document.title})";

(async () => {
  let list = null;
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch('http://127.0.0.1:' + port + '/json/list'); list = await r.json(); if (list && list.length) break; } catch (e) {}
    await sleep(300);
  }
  if (!list || !list.length) { console.log(JSON.stringify({ok:false, error:'no CDP target'})); return; }
  const page = list.find(t => t.type === 'page') || list[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  const send = (m, p) => new Promise((res, rej) => { const mid = ++id; pending.set(mid, {res, rej}); ws.send(JSON.stringify({id: mid, method: m, params: p || {}})); });
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } };
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws error')); });
  await send('Network.enable');
  await send('Page.enable');
  const raw = JSON.parse(fs.readFileSync(cookiesFile, 'utf8'));
  const cookies = raw.filter(c => c && c.domain && /douyin/.test(c.domain)).map(c => {
    const o = {name: c.name, value: c.value, domain: c.domain, path: c.path || '/', secure: !!c.secure, httpOnly: !!c.httpOnly};
    if (typeof c.expires === 'number' && c.expires > 0) o.expires = c.expires;
    if (c.sameSite && ['Strict','Lax','None'].indexOf(c.sameSite) >= 0) o.sameSite = c.sameSite;
    return o;
  });
  const setRes = await send('Network.setCookies', {cookies});
  await send('Page.navigate', {url});
  const deadline = Date.now() + Math.max(settleMs, 30000);
  let links = 0, wall = false, title = '';
  let scrolled = 0, stable = 0, last = -1;
  const MAX_SCROLL = 12;
  while (Date.now() < deadline) {
    await sleep(1200);
    try {
      const ev = await send('Runtime.evaluate', {expression: PROBE, returnByValue: true});
      const st = JSON.parse(ev.result.value);
      links = st.links; wall = st.wall; title = st.title;
    } catch (e) { continue; }
    if (links === last) { stable++; } else { stable = 0; last = links; }
    // 首屏只渲染约 21 张卡片；滚动才把更早的作品加载进来
    if (links > 0 && stable < 3 && scrolled < MAX_SCROLL) {
      await send('Runtime.evaluate', {expression: 'window.scrollTo(0, document.body.scrollHeight); 1', returnByValue: true});
      scrolled++;
    } else if (links > 0 && stable >= 3) {
      break;
    }
  }
  const htmlRes = await send('Runtime.evaluate', {expression: 'document.documentElement.outerHTML', returnByValue: true});
  const html = htmlRes.result.value || '';
  fs.writeFileSync(outFile, html, 'utf8');
  console.log(JSON.stringify({ok:true, bytes: html.length, postListLinks: links, loginWall: wall, pageTitle: title, scrolls: scrolled, cookiesInjected: cookies.length, setCookiesResult: setRes && setRes.cookies ? setRes.cookies.length : null}));
  ws.close();
})().catch(e => console.log(JSON.stringify({ok:false, error: String(e)})));