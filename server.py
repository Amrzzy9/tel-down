import os, re, glob, time, threading, queue, subprocess, shutil, logging, pty, fcntl, termios, struct, select
import numpy as np, cv2
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS

logging.getLogger('werkzeug').setLevel(logging.ERROR)
Q = {'moderate': (24, None), 'high': (30, None),
     'higher': (34, "scale=-2:'min(480,ih)',fps=15")}
TDL, W, O, MARK = '/content/tdl', '/content/work', '/content/out', '/content/tdl_ok'
GPU = bool(shutil.which('nvidia-smi'))
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
S = {'state': 'out', 'url': '', 'user': ''}
if os.path.exists(MARK): S.update(state='in', user=open(MARK).read())
jobs, dq, eq, KEY, MFD = [], queue.Queue(), queue.Queue(), None, None
app = Flask(__name__); CORS(app)

def decode(rows):
    w = max(map(len, rows)); rows = [r.ljust(w) for r in rows]
    bits = []
    for r in rows:
        bits.append([c in '█▀' for c in r]); bits.append([c in '█▄' for c in r])
    b = np.array(bits)
    for dark in (b, ~b):
        img = np.pad(np.kron(np.where(dark, 0, 255), np.ones((8, 8))), 40, constant_values=255).astype(np.uint8)
        t = cv2.QRCodeDetector().detectAndDecode(img)[0]
        if t: return t
    return ''

def tlogin():
    global MFD
    S.update(state='qr', url='')
    m, s = pty.openpty()
    fcntl.ioctl(s, termios.TIOCSWINSZ, struct.pack('HHHH', 60, 220, 0, 0))
    p = subprocess.Popen([TDL, 'login', '-T', 'qr'], stdin=s, stdout=s, stderr=s, close_fds=True)
    os.close(s); MFD = m; buf, rows, ok = '', [], False
    while True:
        if not select.select([m], [], [], 0.7)[0]:
            if rows: S['url'] = decode(rows) or S['url']; rows = []
            if p.poll() is not None: break
            continue
        try: d = os.read(m, 4096)
        except OSError: break
        if not d: break
        txt = ANSI.sub('', d.decode('utf8', 'ignore')).replace('\r', '')
        if 'assword' in txt: S['state'] = 'pw'
        r = re.search(r'Login successfully.*?ID: (\d+)(?:, Username: (\S*))?', txt)
        if r:
            ok = True; S.update(state='in', user='@' + r[2] if r[2] else 'ID ' + r[1]); open(MARK, 'w').write(S['user'])
        buf += txt; *lines, buf = buf.split('\n')
        for l in lines:
            if l.strip() and set(l) <= set(' █▀▄'): rows.append(l)
            elif rows: S['url'] = decode(rows) or S['url']; rows = []
    if not ok: S.update(state='out', url='')

def dworker():
    while True:
        j = dq.get()
        if j['st'] == 'cancelled': continue
        j['st'] = 'downloading'; d = f"{W}/{j['id']}"
        try:
            shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
            p = subprocess.Popen([TDL, 'dl', '-u', j['url'], '-d', d], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            j['proc'] = p; tail = ''
            while c := os.read(p.stdout.fileno(), 4096):
                t = ANSI.sub('', c.decode('utf8', 'ignore')); tail = (tail + t)[-300:]
                m = re.findall(r'(\d{1,3}(?:\.\d+)?)%', t)
                if m: j['dl'] = float(m[-1])
                s = re.findall(r'([\d.]+) ([KMG])B/s', t)
                if s: j['spd'] = float(s[-1][0]) * {'K': 1e3, 'M': 1048576, 'G': 1e9}[s[-1][1]]
            p.wait()
            if j.get('cancel'): raise Exception('Stopped')
            f = [x for x in glob.glob(d + '/*') if os.path.isfile(x)]
            if not f: raise Exception('Nothing downloaded: no access, not a video, or Telegram not linked. ' + tail[-160:].strip())
            j['src'] = f[0]; j['dl'] = 100; j['size0'] = os.path.getsize(f[0])
            j['name'] = os.path.splitext(os.path.basename(f[0]))[0] + '_small.mp4'
            eq.put(j)
        except Exception as e:
            j.update(st='cancelled' if j.get('cancel') else 'error', err=str(e))
            shutil.rmtree(d, ignore_errors=True)

def eworker():
    while True:
        j = eq.get()
        if j.get('cancel'):
            j.update(st='cancelled', err='Stopped'); shutil.rmtree(f"{W}/{j['id']}", ignore_errors=True); continue
        j['st'] = 'shrinking'
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
            j['proc'] = p
            for l in p.stdout:
                if l.startswith('out_time_us='):
                    try: j['enc'] = min(99, int(l[12:]) / 1e6 / d * 100)
                    except ValueError: pass
            p.wait()
            if j.get('cancel'): raise Exception('Stopped')
            if p.returncode or not os.path.exists(out): raise Exception('ffmpeg failed')
            if os.path.getsize(out) >= j['size0']: shutil.copy(src, out)  # never return a bigger file
            j.update(enc=100, size1=os.path.getsize(out), file=out, st='done')
            shutil.rmtree(f"{W}/{j['id']}", ignore_errors=True)
        except Exception as e:
            j.update(st='cancelled' if j.get('cancel') else 'error', err=str(e))
            shutil.rmtree(f"{W}/{j['id']}", ignore_errors=True)

def auth():
    if request.headers.get('X-Key', request.args.get('k')) != KEY: abort(401)

@app.get('/api/status')
def status():
    auth()
    return jsonify(tg=S, gpu=GPU, jobs=[{k: v for k, v in j.items() if k not in ('src', 'file', 'proc')} for j in jobs])

@app.post('/api/login')
def api_login():
    auth()
    if S['state'] == 'out': threading.Thread(target=tlogin, daemon=True).start()
    return jsonify(ok=1)

@app.post('/api/password')
def api_pw():
    auth()
    if MFD: os.write(MFD, (request.json.get('pw', '') + '\n').encode())
    return jsonify(ok=1)

@app.post('/api/jobs')
def api_jobs():
    auth(); b = request.json
    if S['state'] != 'in': return jsonify(err='Link Telegram first'), 400
    if b.get('q') not in Q: return jsonify(err='Pick a quality'), 400
    bad = [u for u in b.get('links', []) if not re.search(r't\.me/(c/)?[^/?]+(/\d+)+', u)]
    if bad: return jsonify(err='Not a Telegram message link: ' + bad[0]), 400
    for u in b['links']:
        j = dict(id=len(jobs) + 1, ts=int(time.time() * 1000), url=u.strip(), q=b['q'], st='queued', dl=0, enc=0, spd=0, size0=0, size1=0, name='', err='')
        jobs.append(j); dq.put(j)
    return jsonify(ok=1)

@app.post('/api/cancel/<int:i>')
def api_cancel(i):
    auth(); j = jobs[i - 1]
    if j['st'] == 'queued': j.update(st='cancelled', err='Stopped')
    elif j['st'] in ('downloading', 'shrinking'):
        j['cancel'] = True
        if j.get('proc'): j['proc'].kill()
    return jsonify(ok=1)

@app.get('/api/file/<int:i>')
def api_file(i):
    auth(); j = jobs[i - 1]
    if j['st'] != 'done': abort(404)
    return send_file(j['file'], as_attachment=True, download_name=j['name'])

def serve(key, port=8000):
    global KEY; KEY = key
    for f in (dworker, eworker): threading.Thread(target=f, daemon=True).start()
    threading.Thread(target=lambda: app.run(port=port, threaded=True), daemon=True).start()
