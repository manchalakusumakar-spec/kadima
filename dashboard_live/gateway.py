"""Local dashboard gateway. Broker token never leaves this process."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import threading
import urllib.request
import urllib.error
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

CONFIG_PATH = os.environ.get('KADIMA_DASHBOARD_CONFIG', '/etc/kadima-dashboard/config.json')
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
COOKIE = 'kadima_dashboard_session'
SESSIONS = {}
ATTEMPTS = []
LOCK = threading.Lock()
READS = {'state': (8080, '/state'), 'dashboard_data': (8081, '/dashboard_data'),
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
        raise HTTPException(502, 'backend_request_failed') from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(502, 'backend_unavailable') from exc

@app.get('/{name}')
def read_data(name: str, request: Request):
    authenticated(request)
    if name not in READS:
        raise HTTPException(404, 'unknown_route')
    return upstream(*READS[name])

@app.post('/manual_exit/{signal_id}')
def manual_exit(signal_id: int, request: Request):
    origin_check(request)
    authenticated(request)
    if signal_id < 0:
        raise HTTPException(400, 'invalid_signal')
    return upstream(8081, '/manual_exit/'+str(signal_id), 'POST', {'reason':'manual_exit_dashboard'})
