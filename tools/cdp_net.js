// capture network URLs of a douyin detail page via CDP
const [port, url, waitMs] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const list = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
  const page = list.find(t => t.type === 'page' && t.url.includes('douyin')) || list.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map(); const urls = [];
  const send = (m, p) => new Promise((res, rej) => { const mid = ++id; pending.set(mid, {res, rej}); ws.send(JSON.stringify({id: mid, method: m, params: p || {}})); });
  ws.onmessage = ev => { const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); }
    else if (m.method === 'Network.requestWillBeSent') { const u = m.params.request.url; if (/douyinvod|\.mp4|\/play\/|video\/tos|mime_type=video/.test(u)) urls.push(u); }
  };
  await new Promise(r => { ws.onopen = r; });
  await send('Network.enable');
  await send('Page.enable');
  await send('Page.navigate', {url});
  await sleep(Number(waitMs || 12000));
  console.log(JSON.stringify({count: urls.length, sample: urls.slice(0, 12)}, null, 1).slice(0, 4000));
  ws.close();
})().catch(e => console.log('ERR ' + e));