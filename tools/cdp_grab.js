// grab media stream URLs of a douyin detail page via CDP network events
// usage: node cdp_grab.js <port> <url> [waitMs] [cookies.json]
// cookies.json 存在时先注入登录态——不少作品页游客只给封面，媒体流一条都不发。
const fs = require('fs');
const [port, url, waitArg, cookiesFile] = process.argv.slice(2);
const waitMs = Number(waitArg || 15000);
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  let list = null;
  for (let i = 0; i < 40; i++) {
    try { const r = await fetch('http://127.0.0.1:' + port + '/json/list'); list = await r.json(); if (list && list.length) break; } catch (e) {}
    await sleep(300);
  }
  if (!list || !list.length) { console.log(JSON.stringify({ok:false, error:'no target'})); return; }
  const page = list.find(t => t.type === 'page') || list[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  const audio = [], video = [], other = [];
  const send = (m, p) => new Promise((res, rej) => { const mid = ++id; pending.set(mid, {res, rej}); ws.send(JSON.stringify({id: mid, method: m, params: p || {}})); });
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); return; }
    if (m.method === 'Network.requestWillBeSent') {
      const u = m.params.request.url;
      if (!/douyinvod\.com|\/aweme\/v1\/play|video\/tos/.test(u)) return;
      if (/media-audio/.test(u)) { if (audio.indexOf(u) < 0) audio.push(u); }
      else if (/media-video/.test(u)) { if (video.indexOf(u) < 0) video.push(u); }
      else if (other.indexOf(u) < 0) other.push(u);
    }
  };
  await new Promise(r => { ws.onopen = r; });
  await send('Network.enable');
  await send('Page.enable');
  let cookiesInjected = 0;
  if (cookiesFile) {
    try {
      const raw = JSON.parse(fs.readFileSync(cookiesFile, 'utf8'));
      const cookies = (Array.isArray(raw) ? raw : []).filter(c => c && c.domain && /douyin/.test(c.domain)).map(c => {
        const o = {name: c.name, value: c.value, domain: c.domain, path: c.path || '/', secure: !!c.secure, httpOnly: !!c.httpOnly};
        if (typeof c.expires === 'number' && c.expires > 0) o.expires = c.expires;
        if (c.sameSite && ['Strict','Lax','None'].indexOf(c.sameSite) >= 0) o.sameSite = c.sameSite;
        return o;
      });
      if (cookies.length) { await send('Network.setCookies', {cookies}); cookiesInjected = cookies.length; }
    } catch (e) {}
  }
  await send('Page.navigate', {url});
  // 有些作品页不会自动起播（无头浏览器没有用户手势），网络事件里就一条媒体请求都没有。
  // 每轮主动把 video 揪出来设为静音并 play()，同时滚动一下，逼它开始缓冲，流才会出现在网络事件里。
  const nudge = "(()=>{try{const v=document.querySelector('video');"
    + "if(!v)return 'no-video';v.muted=true;try{v.currentTime=0.1}catch(e){}"
    + "if(v.paused){v.play().catch(()=>{})}"
    + "window.scrollTo(0,Math.min(600,document.body.scrollHeight/3));"
    + "return JSON.stringify({paused:v.paused,rs:v.readyState,cs:(v.currentSrc||v.src||'').slice(0,20)});"
    + "}catch(e){return 'err:'+e}})()";
  const deadline = Date.now() + waitMs;
  let nudges = 0;
  while (Date.now() < deadline) {
    await sleep(1000);
    if (audio.length && video.length) break;
    if (nudges < 8) {
      nudges++;
      try { await send('Runtime.evaluate', {expression: nudge, returnByValue: true, userGesture: true}); } catch (e) {}
    }
  }
  let title = '';
  try { const ev = await send('Runtime.evaluate', {expression: 'document.title', returnByValue: true}); title = ev.result.value || ''; } catch (e) {}
  console.log(JSON.stringify({ok: audio.length > 0 || video.length > 0 || other.length > 0, title: title, audioCount: audio.length, videoCount: video.length, otherCount: other.length, cookiesInjected: cookiesInjected, audio: audio.slice(0, 3), video: video.slice(0, 3), other: other.slice(0, 3)}));
  ws.close();
})().catch(e => console.log(JSON.stringify({ok:false, error:String(e)})));