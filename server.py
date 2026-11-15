import os, re, time, asyncio, threading, queue, subprocess, shutil, logging
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import PeerChannel

logging.getLogger('werkzeug').setLevel(logging.ERROR)
Q = {'moderate': (24, None), 'high': (30, None),
     'higher': (34, "scale=-2:'min(480,ih)',fps=15")}
W, O = '/content/work', '/content/out'
GPU = bool(shutil.which('nvidia-smi'))
S = {'state': 'out', 'url': '', 'user': ''}
jobs, dq, eq = [], queue.Queue(), queue.Queue()
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
app = Flask(__name__); CORS(app)
C = KEY = None

def run(c): return asyncio.run_coroutine_threadsafe(c, loop).result()

async def done():
    me = await C.get_me()
    S.update(state='in', user=me.username and '@' + me.username or me.first_name)
    await C.get_dialogs()  # lets private /c/ links resolve

async def login():
    S['state'] = 'qr'
    try:
        qr = await C.qr_login()
        while True:
            S['url'] = qr.url
            try: await qr.wait(25); break
            except asyncio.TimeoutError: await qr.recreate()
    except SessionPasswordNeededError:
        S['state'] = 'pw'; return
    except Exception: S['state'] = 'out'; raise
    await done()

async def password(p):
    await C.sign_in(password=p); await done()

def parse(u):
    m = re.search(r't\.me/(c/)?([^/?]+)((?:/\d+)+)', u.strip())
    if not m: return None
    return (PeerChannel(int(m[2])) if m[1] else m[2]), int(m[3].split('/')[-1])

async def dl(j):
    peer, mid = parse(j['url'])
    m = await C.get_messages(peer, ids=mid)
    if not m or not m.media: raise Exception('No media in that message, or no access')
    t0 = time.time()
    def cb(a, b):
        j['dl'] = a * 100 / b; j['spd'] = a / max(time.time() - t0, 1)
    j['src'] = await C.download_media(m, file=f"{W}/{j['id']}/", progress_callback=cb)

def dworker():
    while True:
        j = dq.get(); j['st'] = 'downloading'
        try:
            os.makedirs(f"{W}/{j['id']}", exist_ok=True)
            for n in range(3):
                try: run(dl(j)); break
                except Exception as e:
                    if n == 2 or 'No media' in str(e): raise
                    j['err'] = f'Retrying ({n + 1}/3): {e}'; time.sleep(5)
            j['err'] = ''; j['dl'] = 100; j['size0'] = os.path.getsize(j['src'])
            j['name'] = os.path.splitext(os.path.basename(j['src']))[0] + '_small.mp4'
            eq.put(j)
        except Exception as e: j.update(st='error', err=str(e) or repr(e))

def eworker():
    while True:
        j = eq.get(); j['st'] = 'shrinking'
        try:
            src = j['src']; cq, vf = Q[j['q']]
            try: d = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=nw=1:nk=1', src], capture_output=True, text=True).stdout)
            except Exception: d = 1
            os.makedirs(O, exist_ok=True); out = f"{O}/{j['id']}.mp4"
            c = ['ffmpeg', '-y', '-loglevel', 'error', '-progress', 'pipe:1', '-nostats', '-i', src]
            if vf: c += ['-vf', vf]
            c += (['-c:v', 'hevc_nvenc', '-preset', 'p4', '-rc', 'vbr', '-cq', str(cq), '-b:v', '0', '-tag:v', 'hvc1']
                  if GPU else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(cq - 2)])
            p = subprocess.Popen(c + ['-c:a', 'aac', '-b:a', '64k', out], stdout=subprocess.PIPE, text=True)
            for l in p.stdout:
                if l.startswith('out_time_us='):
                    try: j['enc'] = min(99, int(l[12:]) / 1e6 / d * 100)
                    except ValueError: pass
            p.wait()
            if p.returncode or not os.path.exists(out): raise Exception('ffmpeg failed')
            if os.path.getsize(out) >= j['size0']: shutil.copy(src, out)  # never return a bigger file
            j.update(enc=100, size1=os.path.getsize(out), file=out, st='done')
            shutil.rmtree(f"{W}/{j['id']}", ignore_errors=True)
        except Exception as e: j.update(st='error', err=str(e))

def auth():
    if request.headers.get('X-Key', request.args.get('k')) != KEY: abort(401)

@app.get('/api/status')
def status():
    auth()
    return jsonify(tg=S, gpu=GPU, jobs=[{k: v for k, v in j.items() if k not in ('src', 'file')} for j in jobs])

@app.post('/api/login')
def api_login():
    auth()
    if S['state'] == 'out': asyncio.run_coroutine_threadsafe(login(), loop)
    return jsonify(ok=1)

@app.post('/api/password')
def api_pw():
    auth()
    try: run(password(request.json.get('pw', '')))
    except Exception as e: return jsonify(err=str(e)), 400
    return jsonify(ok=1)

@app.post('/api/jobs')
def api_jobs():
    auth(); b = request.json
    if S['state'] != 'in': return jsonify(err='Link Telegram first'), 400
    if b.get('q') not in Q: return jsonify(err='Pick a quality'), 400
    bad = [u for u in b.get('links', []) if not parse(u)]
    if bad: return jsonify(err='Not a Telegram message link: ' + bad[0]), 400
    for u in b['links']:
        j = dict(id=len(jobs) + 1, url=u.strip(), q=b['q'], st='queued', dl=0, enc=0, spd=0, size0=0, size1=0, name='', err='')
        jobs.append(j); dq.put(j)
    return jsonify(ok=1)

@app.get('/api/file/<int:i>')
def api_file(i):
    auth(); j = jobs[i - 1]
    if j['st'] != 'done': abort(404)
    return send_file(j['file'], as_attachment=True, download_name=j['name'])

def serve(api_id, api_hash, key, port=8000):
    global C, KEY; KEY = key
    async def mk(): return TelegramClient('/content/tg', api_id, api_hash, request_retries=15,
                                          connection_retries=15, retry_delay=3, timeout=30)
    C = run(mk()); run(C.connect())
    if run(C.is_user_authorized()): run(done())
    for f in (dworker, eworker): threading.Thread(target=f, daemon=True).start()
    threading.Thread(target=lambda: app.run(port=port, threaded=True), daemon=True).start()