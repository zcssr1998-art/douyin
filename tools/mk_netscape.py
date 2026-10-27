
import json, time
ck = json.load(open('data/cookies.json', encoding='utf-8'))
lines = ['# Netscape HTTP Cookie File']
n = 0
for c in ck:
    dom = c.get('domain','')
    if 'douyin' not in dom: continue
    inc = 'TRUE' if dom.startswith('.') else 'FALSE'
    path = c.get('path') or '/'
    sec = 'TRUE' if c.get('secure') else 'FALSE'
    exp = int(c.get('expires') or 0)
    if exp <= 0: exp = int(time.time()) + 86400*365
    lines.append('\t'.join([dom, inc, path, sec, str(exp), c['name'], c['value']]))
    n += 1
open('data/cookies.txt','w',encoding='utf-8',newline='\n').write('\n'.join(lines)+'\n')
print('wrote', n, 'cookies')
