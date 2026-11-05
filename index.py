# ============================================================
# api/index.py — FF Like API + Web UI (Vercel-ready)
# Credit: NG RAXX | @NG_RAXXX
# ============================================================
import os, sys, json, time, random, binascii, threading, tempfile, traceback
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in [_ROOT, _HERE]:
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from flask import Flask, request, jsonify, Response
import asyncio, aiohttp, requests, jwt
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ===== CREDITS =====
CHANNEL = "@NG_RAXXX"
CREDIT  = "NG RAXX"
GROUP   = "@NG_RAXXX"

# ===== AUTH =====
AUTH_KEY = "NG1988"

def require_auth():
    key = request.headers.get("X-API-Key") or request.args.get("key")
    if request.method == 'POST':
        try:
            body = request.get_json(silent=True) or {}
            key = key or body.get("key")
        except Exception:
            pass
    if key != AUTH_KEY:
        return jsonify({
            "error": "Invalid or missing access key",
            "hint": "Provide X-API-Key header, ?key= param, or 'key' in JSON"
        }), 401
    return None

# ===== ENV =====
IS_VERCEL = bool(os.environ.get("VERCEL")) or bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

# ===== SAFE PROTO IMPORTS =====
like_pb2 = like_count_pb2 = uid_generator_pb2 = None
_PROTO_IMPORT_ERROR = {}
for _m in ("like_pb2", "like_count_pb2", "uid_generator_pb2"):
    try:
        __import__(_m)
        globals()[_m] = sys.modules[_m]
    except Exception as _e:
        _PROTO_IMPORT_ERROR[_m] = f"{type(_e).__name__}: {_e}"
        print(f"❌ proto import fail {_m}: {_e}")

# ===== OPTIONAL REDIS =====
_HAS_UPSTASH = False
try:
    from upstash_redis import Redis
    _HAS_UPSTASH = True
except ImportError:
    pass

REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
REDIS_OK = False
redis = None
if _HAS_UPSTASH and REDIS_URL and REDIS_TOKEN:
    try:
        redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)
        redis.ping()
        REDIS_OK = True
        print("✅ Upstash Redis connected")
    except Exception as e:
        print(f"❌ Redis connect failed: {e}")

def redis_key(server): return f"accounts:{server}"

# ===== TUNABLES =====
JWT_CONCURRENCY  = 20 if IS_VERCEL else 50
LIKE_CONCURRENCY = 20 if IS_VERCEL else 50
JWT_TIMEOUT      = 15 if IS_VERCEL else 30
FF_TIMEOUT       = 6  if IS_VERCEL else 10
MAX_ACCS_VERCEL  = int(os.environ.get("MAX_ACCS", "60"))
TOKEN_MIN_VALIDITY = 1800
KEY_LIMIT = 9999

# ===== DATA DIR =====
def _get_data_dir():
    if IS_VERCEL: return "/tmp"
    for d in [_HERE, os.getcwd(), tempfile.gettempdir()]:
        try:
            if d and os.path.isdir(d):
                t = os.path.join(d, f".wtest_{os.getpid()}")
                with open(t, "w") as f: f.write("x")
                os.remove(t); return d
        except Exception:
            continue
    return "/tmp"

DATA_DIR = _get_data_dir()
print(f"📁 DATA_DIR={DATA_DIR} | VERCEL={IS_VERCEL} | REDIS={REDIS_OK}")

def data_path(n): return os.path.join(DATA_DIR, n)

def _seed(name):
    if not IS_VERCEL: return
    dst = data_path(name)
    if os.path.exists(dst): return
    for base in [_HERE, _ROOT, os.getcwd()]:
        src = os.path.join(base, name)
        try:
            if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
                with open(src, "rb") as rf, open(dst, "wb") as wf:
                    wf.write(rf.read())
                return
        except Exception:
            continue

for _f in ["account_ind.txt","account_br.txt","account_bd.txt","account_ru.txt",
           "token_ind.json","token_br.json","token_bd.json","token_ru.json"]:
    _seed(_f)

# ===== FLASK =====
app = Flask(__name__)

@app.errorhandler(Exception)
def _on_error(e):
    tb = traceback.format_exc()
    print("❌ UNHANDLED:", tb)
    return jsonify({
        "error": f"{type(e).__name__}: {e}",
        "traceback_tail": tb.strip().splitlines()[-8:],
        "hint": "GET /health",
        "Credit": f"🔥 {CREDIT} | {CHANNEL}"
    }), 500

# ===== STATE =====
TOKEN_CACHE = {}
tracker = defaultdict(lambda: [0, time.time()])
liked_cache = defaultdict(set)

TOKEN_FILES = {
    "IND": data_path("token_ind.json"),
    "BR":  data_path("token_br.json"),
    "BD":  data_path("token_bd.json"),
    "RU":  data_path("token_ru.json"),
}
ACCOUNT_FILES = {
    "IND": data_path("account_ind.txt"),
    "BR":  data_path("account_br.txt"),
    "BD":  data_path("account_bd.txt"),
    "RU":  data_path("account_ru.txt"),
}

URLS = {
    "IND": "https://client.ind.freefiremobile.com",
    "BR":  "https://client.us.freefiremobile.com",
    "US":  "https://client.us.freefiremobile.com",
    "SAC": "https://client.us.freefiremobile.com",
    "NA":  "https://client.us.freefiremobile.com",
    "BD":  "https://clientbp.ppmainecoonghj.com",
    "RU":  "https://client.ru.freefiremobile.com",
}
JWT_API = "http://node54.lunes.host:3069/token"

UA = "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)"
XGA_SV = "1789556611"; UNITY_V = "2018.4.12f1"; REL_V = "OB55"

# ===== SAFE ASYNC =====
def run_async(coro):
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if in_loop:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, coro).result()
    return asyncio.run(coro)

# ===== HELPERS =====
def midnight():
    n = datetime.now(); return datetime(n.year, n.month, n.day).timestamp()

def is_placeholder(u, p):
    bad = {"uid","password","user","pass","your_uid","your_password"}
    return u.lower() in bad or p.lower() in bad

def load_file(fn):
    accs, seen = [], set()
    if not os.path.exists(fn): return accs
    try:
        with open(fn) as f:
            for l in f:
                l = l.strip()
                if not l or l.startswith('#'): continue
                if ':' in l:
                    u, p = l.split(':', 1); u, p = u.strip(), p.strip()
                    if not u or not p or is_placeholder(u, p): continue
                    if u not in seen:
                        seen.add(u); accs.append({"uid": u, "password": p})
    except Exception as e:
        print(f"⚠️ load_file {fn}: {e}")
    return accs

def load_accounts(s):
    if s in {"BR","US","SAC","NA"}: pk = "BR"
    elif s in {"IND","BD","RU"}: pk = s
    else: pk = "IND"

    if REDIS_OK:
        try:
            data = redis.hgetall(redis_key(pk)) or {}
            accs = [{"uid": str(k), "password": str(v)} for k, v in data.items() if k and v]
            if accs: return accs
        except Exception as e:
            print(f"⚠️ redis load: {e}")

    a = load_file(ACCOUNT_FILES[pk])
    if a: return a
    for k, path in ACCOUNT_FILES.items():
        if k == pk: continue
        a = load_file(path)
        if a: return a
    return []

def load_accounts_by_file():
    out = {}
    for srv, fn in ACCOUNT_FILES.items():
        if REDIS_OK:
            try:
                data = redis.hgetall(redis_key(srv)) or {}
                if data:
                    out[srv] = [{"uid": str(k), "password": str(v)} for k, v in data.items()]
                    continue
            except Exception:
                pass
        a = load_file(fn)
        if a: out[srv] = a
    return out

def save_tokens_for_server(server, uids):
    fname = TOKEN_FILES.get(server)
    if not fname: return False
    try:
        tokens = {}
        for uid in uids:
            c = TOKEN_CACHE.get(uid)
            if not c: continue
            tokens[uid] = {
                "token": c.get("token"),
                "expires_at": c["expires_at"].isoformat() if isinstance(c.get("expires_at"), datetime) else str(c.get("expires_at")),
                "login_url": c.get("login_url"),
                "region": c.get("region"),
                "fetched_at": c.get("fetched_at"),
            }
        with open(fname, "w") as f:
            json.dump({"server": server,
                       "updated_at": datetime.now(timezone.utc).isoformat(),
                       "total": len(tokens), "tokens": tokens}, f, indent=2)
        return True
    except Exception as e:
        print(f"⚠️ save {fname}: {e}"); return False

def load_tokens_from_file(server):
    fname = TOKEN_FILES.get(server)
    if not fname or not os.path.exists(fname): return 0
    try:
        with open(fname) as f: data = json.load(f)
        now = datetime.now(timezone.utc); loaded = 0
        for uid, c in (data.get("tokens") or {}).items():
            try:
                exp = datetime.fromisoformat(c["expires_at"])
                if (exp - now).total_seconds() > TOKEN_MIN_VALIDITY:
                    TOKEN_CACHE[uid] = {
                        "token": c["token"], "expires_at": exp,
                        "login_url": c.get("login_url"),
                        "region": c.get("region"),
                        "fetched_at": c.get("fetched_at", time.time()),
                    }
                    loaded += 1
            except Exception:
                continue
        return loaded
    except Exception:
        return 0

def load_all_tokens_from_files():
    return sum(load_tokens_from_file(s) for s in TOKEN_FILES.keys())

async def fetch_raw(session, uid, pwd):
    try:
        url = f"{JWT_API}?uid={uid}&password={pwd}"
        async with session.get(url, timeout=JWT_TIMEOUT) as r:
            if r.status != 200: return None
            d = await r.json()
            if not isinstance(d, dict) or not d.get("Success", True): return None
            return d
    except Exception as e:
        print(f"⚠️ fetch_raw {uid}: {type(e).__name__}: {e}")
        return None

def token_is_valid(uid):
    c = TOKEN_CACHE.get(uid)
    if not c: return False
    try:
        return (c["expires_at"] - datetime.now(timezone.utc)).total_seconds() > TOKEN_MIN_VALIDITY
    except Exception: return False

async def get_valid_token(session, uid, pwd, force=False):
    if not force and token_is_valid(uid):
        return TOKEN_CACHE[uid]["token"]
    d = await fetch_raw(session, uid, pwd)
    if not d: return None
    t = d.get("Jwt_Token") or d.get("jwt_token") or d.get("token")
    if not t or t == "N/A": return None
    try:
        p = jwt.decode(t, options={"verify_signature": False})
        exp = datetime.fromtimestamp(p["exp"], tz=timezone.utc)
    except Exception:
        exp = datetime.now(timezone.utc) + timedelta(hours=8)
    TOKEN_CACHE[uid] = {
        "token": t, "expires_at": exp,
        "login_url": d.get("Login_url"), "region": d.get("Region"),
        "fetched_at": time.time(),
    }
    return t

def encrypt_msg(pt):
    c = AES.new(b'Yg&tc%DEuh6%Zc^8', AES.MODE_CBC, b'6oyZDr22E3ychjM%')
    return binascii.hexlify(c.encrypt(pad(pt, AES.block_size))).decode()

def make_pb(uid, region):
    m = like_pb2.like(); m.uid = int(uid); m.region = region
    return m.SerializeToString()

async def send_like(session, enc_uid, token, url):
    try:
        h = {'User-Agent': UA, 'Accept': "*/*", 'Accept-Encoding': "deflate, gzip",
             'X-GA-SV': XGA_SV, 'Authorization': f"Bearer {token}", 'X-GA': "v1 1",
             'ReleaseVersion': REL_V, 'Content-Type': "application/x-www-form-urlencoded",
             'X-Unity-Version': UNITY_V}
        async with session.post(url, data=bytes.fromhex(enc_uid), headers=h, timeout=8) as r:
            return r.status
    except Exception:
        return 500

def region_of(login_url, region_field):
    if region_field:
        r = str(region_field).upper()
        if r in URLS: return r
    if not login_url: return None
    l = login_url.lower()
    if "client.ind" in l: return "IND"
    if "client.us" in l:  return "US"
    if "client.ru" in l:  return "RU"
    if "clientbp" in l or "client.bd" in l: return "BD"
    return None

def region_matches(acc_region, target):
    if not acc_region: return True
    if target in {"IND","BD","RU"}: return acc_region == target
    if target in {"BR","US","SAC","NA"}: return acc_region in {"BR","US","SAC","NA"}
    return True

async def prewarm_tokens(accs, server=None, force=False):
    need, have = [], []
    for a in accs:
        if not force and token_is_valid(a['uid']): have.append(a)
        else: need.append(a)
    valid = []
    if need:
        sem = asyncio.Semaphore(JWT_CONCURRENCY)
        conn = aiohttp.TCPConnector(limit=JWT_CONCURRENCY*2, force_close=False)
        tmo = aiohttp.ClientTimeout(total=JWT_TIMEOUT)
        async with aiohttp.ClientSession(connector=conn, timeout=tmo) as s:
            async def one(a):
                async with sem:
                    t = await get_valid_token(s, a['uid'], a['password'], force=force)
                    return a if t else None
            res = await asyncio.gather(*[one(a) for a in need], return_exceptions=True)
        valid = [r for r in res if isinstance(r, dict)]
    combined = have + valid
    if server is not None:
        combined = [a for a in combined if region_matches(
            region_of((TOKEN_CACHE.get(a['uid']) or {}).get("login_url"),
                      (TOKEN_CACHE.get(a['uid']) or {}).get("region")), server)]
    return combined

async def broadcast_likes(target, server, url, valid_accs):
    pb = make_pb(target, server); enc_uid = encrypt_msg(pb)
    done = liked_cache.get(target, set())
    fresh = [a for a in valid_accs if a['uid'] not in done]
    if not fresh: return {"success": 0, "failed": 0}
    random.shuffle(fresh)
    sem = asyncio.Semaphore(LIKE_CONCURRENCY)
    conn = aiohttp.TCPConnector(limit=LIKE_CONCURRENCY*2, force_close=False)
    tmo = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(connector=conn, timeout=tmo) as s:
        async def one(a):
            async with sem:
                c = TOKEN_CACHE.get(a['uid']) or {}
                ep = f"{c['login_url'].rstrip('/')}/LikeProfile" if c.get("login_url") else url
                tok = c.get("token")
                if not tok: return 500
                st = await send_like(s, enc_uid, tok, ep)
                if st == 200: liked_cache[target].add(a['uid'])
                return st
        res = await asyncio.gather(*[one(a) for a in fresh], return_exceptions=True)
    ok = sum(1 for r in res if r == 200)
    return {"success": ok, "failed": len(res) - ok}

def enc(uid):
    m = uid_generator_pb2.uid_generator()
    fs = list(m.DESCRIPTOR.fields)
    setattr(m, fs[0].name, int(uid))
    if len(fs) > 1: setattr(m, fs[1].name, 1)
    return encrypt_msg(m.SerializeToString())

def decode(b):
    try:
        i = like_count_pb2.Info(); i.ParseFromString(b); return i
    except Exception: return None

def get_info(enc_uid, server, token):
    url = URLS.get(server, URLS["IND"])
    h = {'User-Agent': UA, 'Accept': "*/*", 'Accept-Encoding': "deflate, gzip",
         'X-GA-SV': XGA_SV, 'Authorization': f"Bearer {token}", 'X-GA': "v1 1",
         'ReleaseVersion': REL_V, 'Content-Type': "application/x-www-form-urlencoded",
         'X-Unity-Version': UNITY_V}
    try:
        r = requests.post(f"{url}/GetPlayerPersonalShow",
                          data=bytes.fromhex(enc_uid), headers=h,
                          verify=False, timeout=FF_TIMEOUT)
        return decode(r.content)
    except Exception as e:
        print(f"⚠️ get_info: {type(e).__name__}: {e}")
        return None

# ================= HTML UI =================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NG RAXX | Like API</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:linear-gradient(135deg,#1a0b2e 0%,#0f0520 50%,#1e0942 100%);
color:#e9d5ff;min-height:100vh;padding:20px}
.container{max-width:900px;margin:0 auto}
.header{text-align:center;padding:30px 0}
.header h1{font-size:42px;font-weight:900;
background:linear-gradient(135deg,#a78bfa,#c084fc,#8b5cf6);
-webkit-background-clip:text;-webkit-text-fill-color:transparent;
background-clip:text;margin-bottom:8px;letter-spacing:-1px}
.header p{color:#a78bfa;font-size:14px}
.card{background:rgba(30,9,66,.6);backdrop-filter:blur(20px);
border:1px solid rgba(139,92,246,.3);border-radius:20px;padding:30px;
margin-bottom:20px;box-shadow:0 8px 32px rgba(139,92,246,.15)}
.card h2{font-size:20px;margin-bottom:20px;color:#c4b5fd}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}
.stat-card{background:linear-gradient(135deg,rgba(139,92,246,.15),rgba(168,139,250,.05));
border:1px solid rgba(139,92,246,.3);border-radius:12px;padding:16px;text-align:center}
.stat-label{font-size:11px;color:#a78bfa;text-transform:uppercase;letter-spacing:1px}
.stat-value{font-size:28px;font-weight:800;margin-top:6px;color:#e9d5ff}
.form-group{margin-bottom:16px}
.form-group label{display:block;margin-bottom:6px;font-size:13px;color:#a78bfa;font-weight:500}
input,select,textarea{width:100%;padding:12px 16px;background:rgba(10,6,24,.8);
border:1px solid rgba(139,92,246,.3);border-radius:10px;color:#e9d5ff;
font-size:14px;font-family:inherit;outline:none;transition:all .2s}
input:focus,select:focus,textarea:focus{border-color:#8b5cf6;
box-shadow:0 0 0 3px rgba(139,92,246,.15)}
textarea{min-height:140px;resize:vertical;font-family:'Courier New',monospace}
button{background:linear-gradient(135deg,#8b5cf6,#a855f7);color:#fff;border:none;
padding:14px 28px;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;
transition:all .2s;width:100%}
button:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(139,92,246,.4)}
button:active{transform:translateY(0)}
button:disabled{opacity:.5;cursor:not-allowed;transform:none}
.result{margin-top:16px;padding:16px;background:rgba(10,6,24,.8);
border:1px solid rgba(139,92,246,.3);border-radius:10px;
font-family:'Courier New',monospace;font-size:13px;white-space:pre-wrap;
word-break:break-all;display:none}
.result.show{display:block}
.result.success{border-color:rgba(34,197,94,.5);background:rgba(34,197,94,.1)}
.result.error{border-color:rgba(239,68,68,.5);background:rgba(239,68,68,.1)}
.login-box{max-width:400px;margin:100px auto}
.hidden{display:none!important}
.footer{text-align:center;padding:30px 0;color:#7c3aed;font-size:12px}
.badge{display:inline-block;padding:4px 10px;background:rgba(139,92,246,.2);
border:1px solid rgba(139,92,246,.4);border-radius:20px;font-size:11px;color:#c4b5fd}
.badge.warn{background:rgba(234,179,8,.15);border-color:rgba(234,179,8,.4);color:#fde68a}
.badge.ok{background:rgba(34,197,94,.15);border-color:rgba(34,197,94,.4);color:#86efac}
@media(max-width:600px){.header h1{font-size:32px}}
</style>
</head>
<body>
<div class="container">

<div id="loginScreen" class="hidden">
  <div class="header"><h1>NG RAXX</h1><p>Free Fire Like API • @NG_RAXXX</p></div>
  <div class="card login-box">
    <h2>🔐 Enter Access Key</h2>
    <div class="form-group">
      <label>Access Key</label>
      <input type="password" id="keyInput" placeholder="Enter your key..." autocomplete="off">
    </div>
    <button onclick="login()">Login</button>
    <div class="result" id="loginResult"></div>
  </div>
</div>

<div id="dashboard" class="hidden">
  <div class="header"><h1>NG RAXX</h1><p>Free Fire Like API • @NG_RAXXX</p></div>

  <div class="card">
    <h2>📊 Account Stats</h2>
    <div class="stats-grid" id="statsGrid">
      <div class="stat-card"><div class="stat-label">Loading</div><div class="stat-value">...</div></div>
    </div>
    <div style="text-align:right;margin-top:12px">
      <button style="width:auto;padding:8px 16px;font-size:12px" onclick="loadStats()">Refresh</button>
    </div>
  </div>

  <div class="card">
    <h2>🎯 Get Likes</h2>
    <div class="form-group">
      <label>Server</label>
      <select id="likeServer">
        <option value="IND">IND</option>
        <option value="BR">BR</option>
        <option value="BD">BD</option>
        <option value="RU">RU</option>
        <option value="US">US</option>
        <option value="SAC">SAC</option>
        <option value="NA">NA</option>
      </select>
    </div>
    <div class="form-group">
      <label>Player UID</label>
      <input type="text" id="likeUid" placeholder="Enter Free Fire UID..." inputmode="numeric">
    </div>
    <button id="likeBtn" onclick="getLikes()">Get Likes</button>
    <div class="result" id="likeResult"></div>
  </div>

  <div class="card">
    <h2>➕ Add Accounts</h2>
    <div class="form-group">
      <label>Server</label>
      <select id="addServer">
        <option value="IND">IND</option>
        <option value="BR">BR</option>
        <option value="BD">BD</option>
        <option value="RU">RU</option>
      </select>
    </div>
    <div class="form-group">
      <label>Accounts (one per line, format: UID:PASSWORD)</label>
      <textarea id="accountsInput" placeholder="123456789:password123&#10;987654321:password456&#10;..."></textarea>
    </div>
    <button id="addBtn" onclick="addAccounts()">Add All Accounts</button>
    <div class="result" id="addResult"></div>
  </div>

  <div class="footer">
    <div>🔥 NG RAXX | @NG_RAXXX</div>
    <div style="margin-top:8px">
      <span class="badge" id="redisBadge">Storage: ...</span>
      <button style="width:auto;padding:6px 12px;font-size:11px;margin-left:8px" onclick="logout()">Logout</button>
    </div>
  </div>
</div>

</div>
<script>
const KEY_STORAGE='ng_raxx_key';
function getKey(){return localStorage.getItem(KEY_STORAGE)||''}
function apiFetch(p,o={}){return fetch(p,{...o,headers:{'X-API-Key':getKey(),...(o.headers||{})}})}
function showResult(id,text,type=''){const e=document.getElementById(id);e.textContent=text;e.className='result show '+type}
function showScreen(w){document.getElementById('loginScreen').classList.toggle('hidden',w!=='login');
document.getElementById('dashboard').classList.toggle('hidden',w!=='dashboard')}

async function login(){
  const k=document.getElementById('keyInput').value.trim();
  if(!k)return;
  const r=await fetch('/stats',{headers:{'X-API-Key':k}});
  if(r.status===401){showResult('loginResult','❌ Invalid key','error');return}
  localStorage.setItem(KEY_STORAGE,k);
  showScreen('dashboard');loadStats();
}
function logout(){localStorage.removeItem(KEY_STORAGE);showScreen('login')}

async function loadStats(){
  try{
    const r=await apiFetch('/stats');
    if(r.status===401){logout();return}
    const d=await r.json();
    const g=document.getElementById('statsGrid');g.innerHTML='';
    const order=['IND','BR','BD','RU','US','SAC','NA'];
    for(const s of order){
      if(!(s in d))continue;
      const div=document.createElement('div');div.className='stat-card';
      div.innerHTML=`<div class="stat-label">${s}</div><div class="stat-value">${d[s]}</div>`;
      g.appendChild(div);
    }
    const t=document.createElement('div');t.className='stat-card';
    t.style.background='linear-gradient(135deg,rgba(168,85,247,.3),rgba(139,92,246,.15))';
    t.innerHTML=`<div class="stat-label">Total</div><div class="stat-value">${d.total}</div>`;
    g.appendChild(t);
    const badge=document.getElementById('redisBadge');
    if(d.storage==='upstash-redis'){
      badge.textContent='✅ Storage: Redis (persistent)';badge.className='badge ok';
    }else{
      badge.textContent='⚠️ Storage: /tmp (not persistent)';badge.className='badge warn';
    }
  }catch(e){console.error(e)}
}

async function getLikes(){
  const uid=document.getElementById('likeUid').value.trim();
  const server=document.getElementById('likeServer').value;
  if(!uid){showResult('likeResult','❌ Enter UID','error');return}
  const btn=document.getElementById('likeBtn');
  btn.disabled=true;btn.textContent='⏳ Sending likes...';
  showResult('likeResult','⏳ Please wait (up to 30s)...','');
  try{
    const r=await apiFetch(`/like?uid=${encodeURIComponent(uid)}&server_name=${server}`);
    if(r.status===401){logout();return}
    const d=await r.json();
    if(d.LikesGivenByAPI!==undefined||d.status===1){
      showResult('likeResult',
        `✅ SUCCESS\n━━━━━━━━━━━━━━━━━━━━\nPlayer     : ${d.PlayerNickname||'-'}\nUID        : ${d.UID||uid}\nServer     : ${d.Server||server}\nBefore     : ${d.LikesbeforeCommand||0}\nAfter      : ${d.LikesafterCommand||0}\nGiven      : +${d.LikesGivenByAPI||0}\nTokens Used: ${d.tokens_used||0}\nTime       : ${d['Elapsed sec']||'-'}s\n━━━━━━━━━━━━━━━━━━━━\n🔥 NG RAXX | @NG_RAXXX`,'success');
    }else{
      showResult('likeResult','❌ '+(d.error||'Failed'),'error');
    }
  }catch(e){showResult('likeResult','❌ Network error: '+e.message,'error')}
  finally{btn.disabled=false;btn.textContent='Get Likes'}
}

async function addAccounts(){
  const raw=document.getElementById('accountsInput').value.trim();
  const server=document.getElementById('addServer').value;
  if(!raw){showResult('addResult','❌ Paste accounts first','error');return}
  const btn=document.getElementById('addBtn');
  btn.disabled=true;btn.textContent='⏳ Adding...';
  showResult('addResult','⏳ Saving accounts...','');
  try{
    const r=await apiFetch('/add',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({accounts:raw,server})});
    if(r.status===401){logout();return}
    const d=await r.json();
    if(d.success){
      showResult('addResult',
        `✅ DONE\n━━━━━━━━━━━━━━━━━━━━\nAdded      : ${d.added}\nDuplicates : ${d.duplicates}\nInvalid    : ${d.invalid}\nTotal      : ${d.total}\nStorage    : ${d.storage}\n━━━━━━━━━━━━━━━━━━━━\n🔥 NG RAXX | @NG_RAXXX`,'success');
      document.getElementById('accountsInput').value='';
      loadStats();
    }else{
      showResult('addResult','❌ '+(d.error||'Failed'),'error');
    }
  }catch(e){showResult('addResult','❌ Network error: '+e.message,'error')}
  finally{btn.disabled=false;btn.textContent='Add All Accounts'}
}

if(getKey()){showScreen('dashboard');loadStats()}else{showScreen('login')}
document.getElementById('keyInput').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
</script>
</body>
</html>"""

# ================= ROUTES =================

@app.route('/')
def home():
    return Response(INDEX_HTML, mimetype='text/html')

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/health')
def health():
    out = {"vercel": IS_VERCEL, "data_dir": DATA_DIR, "redis_ok": REDIS_OK,
           "proto": {}, "network": {}, "files": {}}
    for m, err in _PROTO_IMPORT_ERROR.items():
        out["proto"][m] = f"FAIL: {err}"
    for m in ("like_pb2","like_count_pb2","uid_generator_pb2"):
        if m not in _PROTO_IMPORT_ERROR:
            out["proto"][m] = "ok"
    t0 = time.time()
    try:
        r = requests.get(f"{JWT_API}?uid=0&password=0", timeout=6, verify=False)
        out["network"]["jwt_api"] = f"OK {r.status_code} ({round(time.time()-t0,2)}s)"
    except Exception as e:
        out["network"]["jwt_api"] = f"FAIL: {type(e).__name__}: {e}"
    t0 = time.time()
    try:
        r = requests.get("https://client.ind.freefiremobile.com", timeout=6, verify=False)
        out["network"]["ff_ind"] = f"OK {r.status_code} ({round(time.time()-t0,2)}s)"
    except Exception as e:
        out["network"]["ff_ind"] = f"FAIL: {type(e).__name__}: {e}"
    for srv, path in ACCOUNT_FILES.items():
        out["files"][f"account_{srv.lower()}"] = {
            "path": path, "exists": os.path.exists(path),
            "lines": len(open(path).read().splitlines()) if os.path.exists(path) else 0
        }
    return jsonify(out)

@app.route('/stats')
def stats():
    auth = require_auth()
    if auth: return auth
    out = {}
    total = 0
    for srv in ACCOUNT_FILES:
        if REDIS_OK:
            try:
                n = redis.hlen(redis_key(srv)) or 0
            except Exception:
                n = len(load_file(ACCOUNT_FILES[srv]))
        else:
            n = len(load_file(ACCOUNT_FILES[srv]))
        out[srv] = n
        total += n
    out["total"] = total
    out["storage"] = "upstash-redis" if REDIS_OK else "/tmp"
    return jsonify(out)

@app.route('/add', methods=['GET', 'POST'])
def add_account():
    auth = require_auth()
    if auth: return auth

    server = request.args.get("server", "IND").upper()
    accounts_raw = ""

    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        accounts_raw = body.get("accounts", "")
        server = body.get("server", server).upper()
    else:
        uid = request.args.get("uid", "").strip()
        pwd = request.args.get("pass", "").strip()
        if uid and pwd:
            accounts_raw = f"{uid}:{pwd}"

    if server not in ACCOUNT_FILES:
        return jsonify({"error": f"invalid server. use: {list(ACCOUNT_FILES.keys())}"}), 400
    if not accounts_raw:
        return jsonify({"error": "no accounts provided"}), 400

    added = 0
    duplicates = 0
    invalid = 0

    if REDIS_OK:
        try:
            existing_map = redis.hgetall(redis_key(server)) or {}
            existing_uids = set(existing_map.keys())
        except Exception:
            existing_uids = set()
    else:
        existing_uids = {a['uid'] for a in load_file(ACCOUNT_FILES[server])}

    new_lines = []
    for line in accounts_raw.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'): continue
        if ':' not in line:
            invalid += 1; continue
        u, p = line.split(':', 1); u, p = u.strip(), p.strip()
        if not u or not p or not u.isdigit() or is_placeholder(u, p):
            invalid += 1; continue
        if u in existing_uids:
            duplicates += 1; continue
        existing_uids.add(u)
        new_lines.append(f"{u}:{p}")
        added += 1

    if new_lines:
        try:
            if REDIS_OK:
                for line in new_lines:
                    u, p = line.split(':', 1)
                    redis.hset(redis_key(server), u, p)
            # mirror to file for local fallback / debugging
            with open(ACCOUNT_FILES[server], "a") as f:
                for line in new_lines:
                    f.write(line + "\n")
        except Exception as e:
            return jsonify({"error": f"save failed: {e}"}), 500

    return jsonify({
        "success": True,
        "added": added,
        "duplicates": duplicates,
        "invalid": invalid,
        "total": len(existing_uids),
        "storage": "upstash-redis" if REDIS_OK else "/tmp",
        "Credit": f"🔥 {CREDIT} | {CHANNEL}"
    })

@app.route('/like')
def like():
    auth = require_auth()
    if auth: return auth

    t_start = time.time()
    try:
        uid = request.args.get("uid")
        server = request.args.get("server_name", "").upper()
        ip = request.remote_addr or "unknown"

        if not uid or not server:
            return jsonify({"error": "UID and server_name required"}), 400
        if server not in URLS:
            return jsonify({"error": f"Invalid server. Use: {list(URLS.keys())}"}), 400
        if _PROTO_IMPORT_ERROR:
            return jsonify({"error": "protobuf modules failed to import",
                            "detail": _PROTO_IMPORT_ERROR}), 500

        accs = load_accounts(server)
        if not accs:
            return jsonify({"error": f"No accounts for {server}"}), 500

        if IS_VERCEL and len(accs) > MAX_ACCS_VERCEL:
            accs = accs[:MAX_ACCS_VERCEL]

        mid = midnight()
        cnt, lr = tracker[ip]
        if lr < mid: tracker[ip] = [0, time.time()]; cnt = 0
        if cnt >= KEY_LIMIT:
            return jsonify({"error": "Daily limit reached",
                            "remains": f"(0/{KEY_LIMIT})"}), 429

        valid_accs = run_async(prewarm_tokens(accs, server=server, force=False))
        if not valid_accs:
            return jsonify({"error": f"No valid {server} accounts",
                            "hint": "GET /health → network.jwt_api"}), 500

        first_uid = valid_accs[0]['uid']
        tok = (TOKEN_CACHE.get(first_uid) or {}).get('token')
        if not tok:
            return jsonify({"error": "Token missing after prewarm"}), 500

        enc_uid = enc(uid)
        b = get_info(enc_uid, server, tok)
        if b is None:
            return jsonify({"error": "Invalid UID or server mismatch", "status": 0}), 200
        try:
            bd = json.loads(MessageToJson(b))
            bl = int(bd['AccountInfo'].get('Likes', 0))
        except Exception as e:
            return jsonify({"error": f"Parse failed: {e}", "status": 0}), 200

        like_url = f"{URLS[server]}/LikeProfile"
        stats_r = run_async(broadcast_likes(uid, server, like_url, valid_accs))

        a = get_info(enc_uid, server, tok)
        if a is None:
            return jsonify({"error": "Verify failed", "status": 0}), 200
        ad = json.loads(MessageToJson(a))
        al = int(ad['AccountInfo']['Likes'])
        pid = int(ad['AccountInfo']['UID'])
        pn = str(ad['AccountInfo']['PlayerNickname'])
        given = al - bl
        st = 1 if given != 0 else 2
        if given > 0: tracker[ip][0] += 1; cnt += 1
        return jsonify({
            "LikesGivenByAPI": given,
            "LikesafterCommand": al,
            "LikesbeforeCommand": bl,
            "PlayerNickname": pn,
            "Server": server, "UID": pid, "status": st,
            "Elapsed sec": round(time.time() - t_start, 2),
            "tokens_used": stats_r.get("success", 0),
            "remains": f"({KEY_LIMIT - cnt}/{KEY_LIMIT})",
            "Credit": f"🔥 {CREDIT} | {CHANNEL}"
        })
    except Exception as e:
        tb = traceback.format_exc()
        print("❌ /like crashed:", tb)
        return jsonify({"error": f"{type(e).__name__}: {e}",
                        "traceback_tail": tb.splitlines()[-8:]}), 500

@app.route('/reset-limit')
def reset_limit():
    auth = require_auth()
    if auth: return auth
    ip = request.remote_addr or "unknown"
    tracker[ip] = [0, time.time()]
    return jsonify({"message": "reset", "ip": ip,
                    "remains": f"({KEY_LIMIT}/{KEY_LIMIT})",
                    "Credit": f"🔥 {CREDIT} | {CHANNEL}"})

@app.route('/tokens-info')
def tokens_info():
    auth = require_auth()
    if auth: return auth
    info = {}
    for uid, c in TOKEN_CACHE.items():
        try: left = int((c["expires_at"] - datetime.now(timezone.utc)).total_seconds())
        except Exception: left = -1
        info[uid] = {"region": c.get("region"),
                     "login_url": c.get("login_url"),
                     "expires_in_sec": left,
                     "valid": token_is_valid(uid)}
    return jsonify({"count": len(info), "data_dir": DATA_DIR, "tokens": info})

# ===== LOCAL DEV =====
if __name__ == '__main__':
    print(f"🔥 {CREDIT} | {CHANNEL}")
    print(f"📁 {DATA_DIR} | VERCEL={IS_VERCEL} | REDIS={REDIS_OK}")
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)