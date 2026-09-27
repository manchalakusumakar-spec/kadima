"""Local dashboard gateway. Broker token never leaves this process."""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import time
import threading
from datetime import datetime, timezone
import urllib.request
import urllib.error
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

CONFIG_PATH = os.environ.get('KADIMA_DASHBOARD_CONFIG', '/etc/kadima-dashboard/config.json')
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
COOKIE = 'kadima_dashboard_session'
SESSIONS = {}
ATTEMPTS = []
LOCK = threading.Lock()
READS = {'learning': (8081, '/api/kadima/learning'),
         'prices': (8081, '/api/kadima/prices'),
         'price-minutes': (8081, '/api/kadima/prices/minutes'),
         'price-export': (8081, '/api/kadima/prices/export'),
         'audit': (8081, '/api/kadima/audit'),
         'audit-export': (8081, '/api/kadima/audit/export'), 'state': (8080, '/state'), 'dashboard_data': (8081, '/dashboard_data'),
         'celestial': (8081, '/api/kadima/top20'),
         'universe_snapshot': (8081, '/universe_snapshot'), 'macro_snapshot': (8081, '/macro_snapshot')}

def origin_check(request):
    if request.headers.get('origin') != CONFIG['origin']:
        raise HTTPException(403, 'origin_rejected')

def authenticated(request):
    session = request.cookies.get(COOKIE, '')
    with LOCK:
        now = time.time()
        for k in list(SESSIONS):
            if SESSIONS[k] <= now:
                del SESSIONS[k]
        if session not in SESSIONS:
            raise HTTPException(401, 'sign_in_required')

@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.get('/health')
def health():
    return {'ok': True}


class ResourceSampler:
    """Bounded, read-only host observations shared by all dashboard viewers."""
    def __init__(self, proc='/proc'):
        self.proc = Path(proc)
        self.lock = threading.Lock()
        self.cached = None
        self.sampled_at = None
        self.previous_cpu = None

    def read(self, name):
        # Only constant host-wide filenames are used; never process environments.
        with (self.proc / name).open() as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError('oversized_metric')
        return raw

    def snapshot(self):
        with self.lock:
            mono = time.monotonic()
            if self.cached is not None and 0 <= mono - self.sampled_at < 1:
                return self.cached
            out = dict(ts=datetime.now(timezone.utc).isoformat(), refresh_seconds=2,
                       memory=None, swap=None, cpu=None, disk=None, uptime_seconds=None,
                       missing=[], alerts=[])
            memory = {}
            try:
                wanted = {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}
                for line in self.read('meminfo').splitlines():
                    key, _, value = line.partition(':')
                    if key in wanted:
                        parts = value.split()
                        if len(parts) != 2 or parts[1] != 'kB' or not parts[0].isdigit():
                            raise ValueError('invalid_memory_metric')
                        memory[key] = int(parts[0]) * 1024
                total, available = memory['MemTotal'], memory['MemAvailable']
                if not 0 <= available <= total or total <= 0:
                    raise ValueError('invalid_memory_range')
                out['memory'] = dict(total_bytes=total, available_bytes=available,
                                     used_bytes=total-available,
                                     used_percent=round(100*(total-available)/total, 1),
                                     available_percent=round(100*available/total, 1))
            except (OSError, ValueError, KeyError):
                out['missing'].append('RAM')
            try:
                total, free = memory['SwapTotal'], memory['SwapFree']
                if not 0 <= free <= total:
                    raise ValueError('invalid_swap_range')
                out['swap'] = dict(enabled=total > 0, total_bytes=total,
                                   used_bytes=total-free,
                                   used_percent=round(100*(total-free)/total, 1) if total else None)
            except (ValueError, KeyError):
                out['missing'].append('swap')
            try:
                fields = self.read('stat').splitlines()[0].split()
                if len(fields) < 9 or fields[0] != 'cpu':
                    raise ValueError('invalid_cpu_metric')
                counters = tuple(int(value) for value in fields[1:9])
                if any(value < 0 for value in counters):
                    raise ValueError('invalid_cpu_counter')
                cpu = dict(busy_percent=None, iowait_percent=None, steal_percent=None,
                           interval_seconds=None, logical_cpus=os.cpu_count(), load_1m=None)
                if self.previous_cpu:
                    before, before_at = self.previous_cpu
                    elapsed = mono - before_at
                    delta = tuple(a-b for a, b in zip(counters, before))
                    total = sum(delta)
                    # Long gaps and reset/decreasing counters are not live intervals.
                    if 1 <= elapsed <= 10 and min(delta) >= 0 and total > 0:
                        cpu.update(busy_percent=round(100*(total-delta[3]-delta[4]-delta[7])/total, 1),
                                   iowait_percent=round(100*delta[4]/total, 1),
                                   steal_percent=round(100*delta[7]/total, 1),
                                   interval_seconds=round(elapsed, 2))
                self.previous_cpu = counters, mono
                try:
                    load = os.getloadavg()[0]
                    if math.isfinite(load) and load >= 0:
                        cpu['load_1m'] = round(load, 2)
                except OSError:
                    pass
                out['cpu'] = cpu
            except (OSError, ValueError, IndexError):
                self.previous_cpu = None
                out['missing'].append('CPU')
            try:
                disk = shutil.disk_usage('/')
                if not 0 <= disk.free <= disk.total or disk.total <= 0:
                    raise ValueError('invalid_disk_range')
                out['disk'] = dict(total_bytes=disk.total, available_bytes=disk.free,
                                   used_percent=round(100*(disk.total-disk.free)/disk.total, 1))
            except (OSError, ValueError):
                out['missing'].append('disk')
            try:
                uptime = float(self.read('uptime').split()[0])
                if math.isfinite(uptime) and uptime >= 0:
                    out['uptime_seconds'] = round(uptime)
            except (OSError, ValueError, IndexError):
                pass

            def alert(level, message):
                out['alerts'].append(dict(level=level, message=message))
            if out['memory']:
                available = out['memory']['available_percent']
                if available < 10:
                    alert('critical', 'RAM available below 10%')
                elif available < 20:
                    alert('warning', 'RAM available below 20%')
            if out['disk']:
                used = out['disk']['used_percent']
                if used > 95:
                    alert('critical', 'Disk space available below 5%')
                elif used > 85:
                    alert('warning', 'Disk space available below 15%')
            if out['cpu']:
                for key, limit, message in (
                        ('busy_percent', 85, 'High CPU use in latest sample'),
                        ('iowait_percent', 20, 'High reported I/O wait'),
                        ('steal_percent', 10, 'High hypervisor CPU wait')):
                    value = out['cpu'][key]
                    if value is not None and value >= limit:
                        alert('warning', message)
            levels = {item['level'] for item in out['alerts']}
            out['status'] = ('critical' if 'critical' in levels else 'warning' if levels else
                             'unavailable' if all(out[key] is None for key in ('memory', 'cpu', 'disk')) else
                             'partial' if out['missing'] else
                             'warming_up' if out['cpu']['busy_percent'] is None else 'healthy')
            self.cached, self.sampled_at = out, mono
            return out


RESOURCES = ResourceSampler()


@app.get('/server_health')
def server_health(request: Request):
    authenticated(request)
    return RESOURCES.snapshot()


@app.post('/login')
async def login(request: Request):
    origin_check(request)
    with LOCK:
        now = time.time()
        ATTEMPTS[:] = [t for t in ATTEMPTS if now-t < 300]
        if len(ATTEMPTS) >= 8:
            raise HTTPException(429, 'too_many_attempts')
        ATTEMPTS.append(now)
    raw = await request.body()
    if len(raw) > 4096:
        raise HTTPException(413, 'request_too_large')
    try:
        password = json.loads(raw).get('password', '')
        if not isinstance(password, str):
            raise ValueError()
    except (ValueError, AttributeError):
        raise HTTPException(400, 'invalid_request')
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(CONFIG['salt']), n=16384, r=8, p=1).hex()
    if not hmac.compare_digest(digest, CONFIG['password_hash']):
        raise HTTPException(401, 'invalid_credentials')
    token = secrets.token_urlsafe(32)
    with LOCK:
        if len(SESSIONS) >= 32:
            del SESSIONS[next(iter(SESSIONS))]
        SESSIONS[token] = time.time()+8*3600
    response = JSONResponse({'ok': True})
    response.set_cookie(COOKIE, token, max_age=8*3600, secure=True, httponly=True,
                        samesite='strict', path='/dashboard-api/')
    return response

@app.get('/session')
def session(request: Request):
    authenticated(request)
    return {'ok': True}

@app.post('/logout')
def logout(request: Request):
    origin_check(request)
    with LOCK:
        SESSIONS.pop(request.cookies.get(COOKIE, ''), None)
    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE, path='/dashboard-api/', secure=True, httponly=True, samesite='strict')
    return response

def upstream(port, path, method='GET', payload=None):
    req = urllib.request.Request('http://127.0.0.1:'+str(port)+path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization': 'Bearer '+CONFIG['api_token'], 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            raw = response.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:
                raise ValueError('oversized_response')
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Do not forward raw upstream errors or headers containing credentials.
        if path.startswith('/api/'):
            try:
                detail = json.loads(exc.read(4096)).get('detail')
                reason = detail.get('reason') if isinstance(detail,dict) else detail
                if isinstance(reason,str) and re.fullmatch(r'[a-z_]{1,100}',reason):
                    raise HTTPException(exc.code,reason) from None
            except (ValueError,TypeError,AttributeError):
                pass
        raise HTTPException(502, 'backend_request_failed') from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(502, 'backend_unavailable') from exc

@app.get('/{name}')
def read_data(name: str, request: Request):
    authenticated(request)
    if name not in READS:
        raise HTTPException(404, 'unknown_route')
    if name == 'learning':
        from urllib.parse import urlencode
        items = list(request.query_params.multi_items())
        if (len(items) != len({k for k, _ in items}) or
                any(k not in {'day','strategy','limit','offset'} or len(v)>80 for k,v in items)):
            raise HTTPException(422, 'learning_invalid_filter')
        port,path = READS[name]
        return upstream(port,path+('?' + urlencode(items) if items else ''))
    if name in ('prices','price-minutes','price-export'):
        from urllib.parse import urlencode
        allowed = {'day','key'} if name=='price-minutes' else {'day','asset','symbol'}
        if name=='prices': allowed.update(('limit','offset'))
        items = list(request.query_params.multi_items())
        if (len(items)!=len({k for k,_ in items}) or
                any(k not in allowed or len(v)>80 for k,v in items)):
            raise HTTPException(422,'price_history_invalid_filter')
        port,path = READS[name]
        return upstream(port,path+('?' + urlencode(items) if items else ''))
    if name in ('audit', 'audit-export'):
        from urllib.parse import urlencode
        allowed = {'day', 'strategy', 'status', 'symbol'}
        if name == 'audit': allowed.update(('limit', 'offset'))
        items = list(request.query_params.multi_items())
        if (len(items) != len({k for k, _ in items}) or
                any(k not in allowed or len(v) > 80 for k, v in items)):
            raise HTTPException(422, 'audit_invalid_filter')
        port, path = READS[name]
        return upstream(port, path + ('?' + urlencode(items) if items else ''))
    return upstream(*READS[name])

@app.post('/manual_exit/{signal_id}')
def manual_exit(signal_id: int, request: Request):
    origin_check(request)
    authenticated(request)
    if signal_id < 0:
        raise HTTPException(400, 'invalid_signal')
    return upstream(8081, '/manual_exit/'+str(signal_id), 'POST', {'reason':'manual_exit_dashboard'})


CELESTIAL_WRITES = {'kadima/config','kadima/pause','kadima/resume','kadima/kill',
    'kadima/reset-kill','order/buy','order/sell','order/exit','order/sl','order/trailing-sl',
    'order/modify-sl','order/cancel','order/squareoff-all'}


@app.post('/celestial/{path:path}')
async def celestial_write(path: str, request: Request):
    origin_check(request)
    authenticated(request)
    if path not in CELESTIAL_WRITES:
        raise HTTPException(404,'unknown_route')
    raw = await request.body()
    if len(raw)>4096:
        raise HTTPException(413,'request_too_large')
    try:
        payload = json.loads(raw or b'{}')
        if not isinstance(payload,dict): raise ValueError()
    except (ValueError,UnicodeDecodeError):
        raise HTTPException(400,'invalid_request')
    return await run_in_threadpool(upstream,8081,'/api/'+path,'POST',payload)
