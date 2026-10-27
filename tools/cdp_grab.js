// grab media stream URLs of a douyin detail page via CDP network events
// usage: node cdp_grab.js <port> <url> [waitMs]
const [port, url, waitArg] = process.argv.slice(2);
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
  const audio = [], video = [];
  const send = (m, p) => new Promise((res, rej) => { const mid = ++id; pending.set(mid, {res, rej}); ws.send(JSON.stringify({id: mid, method: m, params: p || {}})); });
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); return; }
    if (m.method === 'Network.requestWillBeSent') {
      const u = m.params.request.url;
      if (!/douyinvod\.com/.test(u)) return;
      if (/media-audio/.test(u) && audio.indexOf(u) < 0) audio.push(u);
      else if (/media-video/.test(u) && video.indexOf(u) < 0) video.push(u);
    }
  };
  await new Promise(r => { ws.onopen = r; });
  await send('Network.enable');
  await send('Page.enable');
  await send('Page.navigate', {url});
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    await sleep(1000);
    if (audio.length && video.length) break;
  }
  let title = '';
  try { const ev = await send('Runtime.evaluate', {expression: 'document.title', returnByValue: true}); title = ev.result.value || ''; } catch (e) {}
  console.log(JSON.stringify({ok: audio.length > 0 || video.length > 0, title: title, audioCount: audio.length, videoCount: video.length, audio: audio.slice(0, 3), video: video.slice(0, 3)}));
  ws.close();
})().catch(e => console.log(JSON.stringify({ok:false, error:String(e)})));