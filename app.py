import os, io, re, sys, csv, json, time, base64, secrets, sqlite3
from xml.sax.saxutils import escape as xml_escape
from calendar import Calendar, monthrange
from datetime import datetime, date, timedelta, timezone
from flask import (Flask, g, request, session, redirect, url_for, render_template,
                   flash, abort, send_file, jsonify, Response)
from jinja2 import DictLoader
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit

try:                                    # read variables from a local .env file (ignored when not installed / on Render)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
USE_PG = bool(DATABASE_URL)
if USE_PG:
    import psycopg2, psycopg2.extras
DB_PATH = os.environ.get("RFU_DB", os.path.join(BASE, "rfu.db"))
FACILITIES = ["Audio Visual Room (AVR)", "Administration Building Lobby", "Covered Court", "Classroom"]
OLD_DEFAULTS = [("Sound System", 2), ("Table", 30), ("Chair", 200), ("Microphone", 6), ("Projector", 3)]  # removed automatically once
BG_IMG = "image.jpg"          # your background picture is read from the images folder (see "YOUR BACKGROUND PICTURE" below)
from logos import LOGOS   # the long base64 logo strings live in logos.py (push it to GitHub together with app.py)

# =====================================================================================================================
# YOUR BACKGROUND PICTURE
#   1. Put your picture in the "images" folder (next to this file) - on your computer AND in your GitHub repository.
#   2. Write the file name below, for example  BACKGROUND = "campus.jpg"
#   Upper / lower case does not matter, and PNG / JPG / WEBP of any size work.  Render redeploys by itself after you commit to GitHub.
#   (The two header logos are built in. To use other logos, put files named like LOGO_LEFT / LOGO_RIGHT in the same folder.)
# =====================================================================================================================
IMAGE_FOLDER = "images"
BACKGROUND = "image.jpg"
LOGO_LEFT = ""
LOGO_RIGHT = ""

def _image_dirs():
    base = os.path.dirname(os.path.abspath(__file__))
    out = []
    for d in (os.path.join(base, IMAGE_FOLDER), os.path.join(base, "static"), base, os.path.join(os.getcwd(), IMAGE_FOLDER), os.getcwd()):
        if d not in out and os.path.isdir(d): out.append(d)
    return out

def _find_image(name):
    """Finds the picture by file name in images/, static/ or next to this file. Ignores upper/lower case, and a different extension."""
    name = (name or "").strip()
    if not name: return None
    want = name.lower(); stem = os.path.splitext(want)[0]; exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
    for pass_ in (1, 2):
        for d in _image_dirs():
            try: files = sorted(os.listdir(d))
            except OSError: continue
            for f in files:
                if (pass_ == 1 and f.lower() == want) or (pass_ == 2 and os.path.splitext(f.lower())[0] == stem and f.lower().endswith(exts)):
                    return os.path.join(d, f)
    return None

def _load_image(name, kind):
    """Returns base64 text (PNG for logos, JPEG for the background), or None when the picture is not there."""
    path = _find_image(name)
    if not path: return None
    try:
        from PIL import Image as _I
        im = _I.open(path); bf = io.BytesIO()
        if kind == "logo": im = im.convert("RGBA"); im.thumbnail((400, 400)); im.save(bf, "PNG", optimize=True)
        else: im = im.convert("RGB"); im.thumbnail((2200, 2200)); im.save(bf, "JPEG", quality=84, optimize=True, progressive=True)
        return base64.b64encode(bf.getvalue()).decode()
    except Exception as err:
        print("Could not use picture %s: %s" % (path, err))
        try: return base64.b64encode(open(path, "rb").read()).decode()
        except Exception: return None

def _placeholder_bg():
    try:
        from PIL import Image as _I
        im = _I.new("RGB", (8, 256)); px = im.load()
        for y in range(256):
            k = y / 255.0; c = (int(176 - 60 * k), int(205 - 40 * k), int(190 - 50 * k))
            for x in range(8): px[x, y] = c
        bf = io.BytesIO(); im.resize((512, 512)).save(bf, "JPEG", quality=80); return base64.b64encode(bf.getvalue()).decode()
    except Exception:
        return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="

LOGOS["1"] = _load_image(LOGO_LEFT, "logo") or LOGOS["1"]
LOGOS["2"] = _load_image(LOGO_RIGHT, "logo") or LOGOS["2"]
BG_IMG = _load_image(BACKGROUND, "bg") or _placeholder_bg()
_bg_path = _find_image(BACKGROUND)
if _bg_path: print("Background picture: using", _bg_path)
else:
    print('Background picture "%s" was NOT found. Folders searched: %s' % (BACKGROUND, ", ".join(_image_dirs())))
    for _d in _image_dirs()[:2]:
        try: print("  files in %s: %s" % (_d, ", ".join(sorted(os.listdir(_d))[:30])))
        except OSError: pass


app = Flask(__name__)
_kf = os.path.join(BASE, "rfu_secret.key")
if os.environ.get("SECRET_KEY"):
    app.secret_key = os.environ["SECRET_KEY"]       # set this on Render so logins survive redeploys / multiple workers
else:
    if not os.path.exists(_kf):
        open(_kf, "w").write(secrets.token_hex(32))
    app.secret_key = open(_kf).read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# ------------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, fullname TEXT NOT NULL,
  student_no TEXT NOT NULL, pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'student', must_change INTEGER DEFAULT 0,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS inventory(name TEXT PRIMARY KEY COLLATE NOCASE, stock INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, rfu_no TEXT, user_id INTEGER NOT NULL, requester TEXT, dept TEXT,
  event TEXT, event_date TEXT, t1 TEXT, t2 TEXT, attendees TEXT, facilities TEXT DEFAULT '[]', head TEXT,
  status TEXT DEFAULT 'Pending', remarks TEXT DEFAULT '', created_at TEXT, returned_at TEXT, return_date TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, request_id INTEGER, kind TEXT,
  message TEXT NOT NULL, created_at TEXT, read_at TEXT);
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  name TEXT NOT NULL, qty INTEGER NOT NULL);
"""

# PostgreSQL / Supabase version of the same schema (SERIAL ids, case-insensitive equipment names, RLS on)
SCHEMA_PG = ("CREATE EXTENSION IF NOT EXISTS citext;\n"
             + SCHEMA.replace("id INTEGER PRIMARY KEY", "id SERIAL PRIMARY KEY").replace("name TEXT PRIMARY KEY COLLATE NOCASE", "name CITEXT PRIMARY KEY")
             + "".join("ALTER TABLE %s ENABLE ROW LEVEL SECURITY;\n" % t for t in ("users", "inventory", "requests", "meta", "items", "notifications")))
assert "SERIAL" in SCHEMA_PG and "CITEXT" in SCHEMA_PG and "NOCASE" not in SCHEMA_PG

_NOCASE = re.compile(r"([A-Za-z_][\w.]*)\s*=\s*\?\s+COLLATE\s+NOCASE", re.I)
def pg_sql(sql):
    """Translate the app's SQLite-style SQL to PostgreSQL."""
    sql = _NOCASE.sub(r"LOWER(\1) = LOWER(?)", sql)
    return sql.replace("%", "%%").replace("?", "%s")

class _Res:
    def __init__(self, cur, lastrowid=None): self.cur, self.lastrowid = cur, lastrowid
    def fetchall(self): return self.cur.fetchall()
    def fetchone(self): return self.cur.fetchone()

class PGConn:
    """Thin wrapper so the rest of the app can keep using db().execute(sql, params)."""
    def __init__(self):
        kw = {} if ("sslmode" in DATABASE_URL or "localhost" in DATABASE_URL or "127.0.0.1" in DATABASE_URL) else {"sslmode": "require"}
        self.c = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor, connect_timeout=10, **kw)
    def execute(self, sql, params=()):
        cur = self.c.cursor(); s_ = pg_sql(sql)
        ins = bool(re.match(r"\s*INSERT\s+INTO\s+(users|requests|items)\b", s_, re.I))
        if ins: s_ += " RETURNING id"
        try: cur.execute(s_, params)
        except Exception:
            self.c.rollback(); raise
        return _Res(cur, cur.fetchone()[0] if ins else None)
    def commit(self): self.c.commit()
    def close(self): self.c.close()

def db():
    if "db" not in g:
        if USE_PG:
            g.db = PGConn()
        else:
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d: d.close()

def q(sql, *a): return db().execute(sql, a).fetchall()
def q1(sql, *a): return db().execute(sql, a).fetchone()
def ex(sql, *a):
    c = db().execute(sql, a); db().commit(); return c

# The school is in the Philippines (UTC+8, no daylight saving). Render servers run on UTC, so use PH time explicitly.
PH = timezone(timedelta(hours=int(os.environ.get("TZ_HOURS", "8"))))
def now_dt(): return datetime.now(PH)
def today_ph(): return now_dt().date()
def now_s(): return now_dt().strftime("%Y-%m-%d %H:%M")

ICONS = {
 "menu": '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>',
 "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
 "check": '<polyline points="20 6 9 17 4 12"/>',
 "new": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><line x1="9" y1="15" x2="15" y2="15"/>',
 "list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
 "cal": '<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
 "pending": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
 "approved": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
 "returned": '<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>',
 "disapproved": '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
 "stock": '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
 "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
 "report": '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
 "bell": '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
 "lock": '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
 "out": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
 "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
 "eyeoff": '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>',
}

# ---- 12-hour (AM/PM) display helpers, used by the pages and the PDF
def t12(t):
    try: return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except Exception: return t or ""
def dfmt(d):
    try: return datetime.strptime((d or "")[:10], "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
    except Exception: return d or ""
def sched(r):
    """One-line schedule text for reports: 'Oct 10, 2026 9:00 AM - 5:00 PM' or 'Oct 10, 2026 9:00 AM to Oct 11, 2026 5:00 PM'."""
    rd = r["return_date"] or r["event_date"]
    if rd == r["event_date"]: return "%s %s - %s" % (dfmt(r["event_date"]), t12(r["t1"]), t12(r["t2"]))
    return "%s %s to %s %s" % (dfmt(r["event_date"]), t12(r["t1"]), dfmt(rd), t12(r["t2"]))
def dt12(s_):
    s_ = s_ or ""
    return (dfmt(s_[:10]) + " " + t12(s_[11:16])).strip()

def _migrate():
    """Older databases: add the new 'return_date' column (date the facility/equipment will be returned)."""
    if USE_PG:
        ex("ALTER TABLE requests ADD COLUMN IF NOT EXISTS return_date TEXT")
    elif "return_date" not in [c["name"] for c in q("PRAGMA table_info(requests)")]:
        ex("ALTER TABLE requests ADD COLUMN return_date TEXT")
    ex("UPDATE requests SET return_date=event_date WHERE return_date IS NULL")

def _remove_old_defaults():
    if not q1("SELECT 1 FROM meta WHERE key='defaults_removed'"):     # equipment is now added by the admin only
        for n, st in OLD_DEFAULTS:
            ex("DELETE FROM inventory WHERE name=? AND stock=? AND NOT EXISTS (SELECT 1 FROM items WHERE name=? COLLATE NOCASE)", n, st, n)
        ex("INSERT INTO meta VALUES('defaults_removed','1')")

def _ensure_admin():
    if not q1("SELECT 1 FROM users WHERE username='admin'"):
        ex("INSERT INTO users(username,fullname,student_no,pw_hash,role,must_change,created_at) VALUES('admin','GSO Administrator','-',?, 'admin',1,?)",
           generate_password_hash(os.environ.get("RFU_ADMIN_PASSWORD", "admin123")), now_s())

def init_db():
    with app.app_context():
        if USE_PG:       # one process creates the tables at a time (several gunicorn workers start together)
            cur = db().c.cursor(); cur.execute("SELECT pg_advisory_xact_lock(7242026)"); cur.execute(SCHEMA_PG); db().commit()
        else:
            db().executescript(SCHEMA)
        for step in (_migrate, _remove_old_defaults, _ensure_admin):
            try: step()
            except Exception as err: print("init_db note (another worker may have done this already):", err)

# ------------------------------------------------------------------ security helpers
FAILS = {}
def is_locked(key):
    c = FAILS.get(key)
    return bool(c and c[0] >= 5 and time.time() - c[1] < 300)
def fail(key):
    c = FAILS.get(key, (0, 0)); FAILS[key] = (c[0] + 1, time.time())

def csrf():
    if "_csrf" not in session: session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]

@app.before_request
def _guard():
    if request.method == "POST" and not secrets.compare_digest(request.form.get("_csrf", ""), session.get("_csrf", "-")):
        abort(400, "Invalid form token. Please go back, refresh the page and try again.")
    g.user = q1("SELECT * FROM users WHERE id=?", session["uid"]) if "uid" in session else None
    if g.user and g.user["must_change"] and request.endpoint not in ("account", "logout", "logo", "static", None):
        flash("Please change your password before continuing.", "err")
        return redirect(url_for("account"))

def login_required(f):
    from functools import wraps
    @wraps(f)
    def w(*a, **k):
        if not g.user: return redirect(url_for("login"))
        return f(*a, **k)
    return w

def admin_required(f):
    from functools import wraps
    @wraps(f)
    @login_required
    def w(*a, **k):
        if g.user["role"] != "admin": abort(403)
        return f(*a, **k)
    return w

def valid_pw(p, p2):
    if len(p) < 8: return "Password must be at least 8 characters."
    if p != p2: return "Passwords do not match."
    return None

# ------------------------------------------------------------------ availability logic
def committed(name, st, en, exclude=0):
    """Units of `name` unavailable between st and en ("YYYY-MM-DD HH:MM"): (a) pending/approved requests that overlap
    that period, (b) approved requests whose return date/time already passed but were NOT marked Returned yet."""
    r = q1("""SELECT COALESCE(SUM(i.qty),0) s FROM items i JOIN requests r ON r.id=i.request_id
              WHERE i.name=? COLLATE NOCASE AND r.id!=? AND r.returned_at IS NULL AND (
                (r.status IN ('Pending','Approved') AND (r.event_date||' '||r.t1)<? AND ?<(COALESCE(r.return_date,r.event_date)||' '||r.t2))
                OR (r.status='Approved' AND (COALESCE(r.return_date,r.event_date)||' '||r.t2)<=?))""",
           name, exclude, en, st, now_s())
    return r["s"]

def stock_of(name):
    r = q1("SELECT name,stock FROM inventory WHERE name=?", name)
    return (r["name"], r["stock"]) if r else (None, None)

def left(name, st, en, exclude=0):
    n, s = stock_of(name)
    return None if n is None else max(0, s - committed(n, st, en, exclude))

def booked_facilities(st, en, exclude=0):
    s = set()
    for r in q("""SELECT facilities FROM requests WHERE status IN ('Pending','Approved') AND id!=?
                  AND (event_date||' '||t1)<? AND ?<(COALESCE(return_date,event_date)||' '||t2)""", exclude, en, st):
        s.update(x.lower() for x in json.loads(r["facilities"]))
    return s

def parse_slot(d, rd, t1, t2):
    try:
        a = datetime.strptime(d + " " + t1, "%Y-%m-%d %H:%M"); b = datetime.strptime(rd + " " + t2, "%Y-%m-%d %H:%M")
        return b > a
    except Exception:
        return False

@app.route("/api/availability")
@login_required
def api_availability():
    d, rd, t1, t2 = (request.args.get(k, "") for k in ("d", "rd", "t1", "t2"))
    rd = rd or d
    if parse_slot(d, rd, t1, t2): st, en = d + " " + t1, rd + " " + t2
    else:      # no valid period chosen yet -> show what is booked / out of stock right now
        n = now_dt(); st, en = n.strftime("%Y-%m-%d %H:%M"), (n + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    out = {"booked": sorted(booked_facilities(st, en)), "left": {}}
    for r in q("SELECT name,stock FROM inventory"):
        out["left"][r["name"]] = left(r["name"], st, en)
    return jsonify(out)

# ------------------------------------------------------------------ auth routes
@app.route("/logo/<n>.png")
def logo(n):
    if n not in LOGOS: abort(404)
    return Response(base64.b64decode(LOGOS[n]), mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})

@app.route("/bg.jpg")
def bg():
    return Response(base64.b64decode(BG_IMG), mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=300"})

def bad(e, k, msg): e.setdefault(k, []).append(msg)
def form_vals(*keys): return {k: request.form.get(k, "") for k in keys}
def clear_bad(v, e):
    for k in e:
        if k in v: v[k] = ""          # only the wrong fields are cleared; everything else is kept
def check_pw(e, p, c):
    if len(p) < 8: bad(e, "password", "Password must be at least 8 characters.")
    elif p != c: bad(e, "confirm", "Passwords do not match. Please re-type it.")

@app.route("/login", methods=["GET", "POST"])
def login():
    v, e = {}, {}
    if request.method == "POST":
        v = form_vals("username", "password"); u = v["username"].strip().lower()
        key = u + "|" + (request.remote_addr or "")
        if is_locked(key): bad(e, "password", "Too many failed attempts. Please wait 5 minutes.")
        else:
            r = q1("SELECT * FROM users WHERE username=?", u)
            if not r: bad(e, "username", "No account found with that username."); fail(key)
            elif not check_password_hash(r["pw_hash"], v["password"]): bad(e, "password", "Incorrect password. Please try again."); fail(key)
            else:
                FAILS.pop(key, None); session.clear(); session["uid"] = r["id"]; csrf()
                return redirect(url_for("dashboard"))
        clear_bad(v, e)
    return render_template("login.html", v=v, e=e)

@app.route("/signup", methods=["GET", "POST"])
def signup():
    v, e = {}, {}
    if request.method == "POST":
        v = form_vals("fullname", "student_no", "username", "password", "confirm")
        for k, l in (("fullname", "Full name"), ("student_no", "Student number"), ("username", "Username"),
                     ("password", "Password"), ("confirm", "Please confirm your password")):
            if not v[k].strip(): bad(e, k, l + " is required.")
        u = v["username"].strip().lower()
        if u and not re.fullmatch(r"[a-z0-9_.]{3,30}", u): bad(e, "username", "Username must be 3-30 characters: letters, numbers, . or _ only.")
        elif u and q1("SELECT 1 FROM users WHERE username=?", u): bad(e, "username", "That username is already taken. Please choose another.")
        if v["password"] and v["confirm"]: check_pw(e, v["password"], v["confirm"])
        if not e:
            ex("INSERT INTO users(username,fullname,student_no,pw_hash,role,created_at) VALUES(?,?,?,?, 'student',?)",
               u, v["fullname"].strip(), v["student_no"].strip(), generate_password_hash(v["password"]), now_s())
            flash("Account created! You can now sign in.", "ok"); return redirect(url_for("login"))
        clear_bad(v, e)
    return render_template("signup.html", v=v, e=e)

@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    v, e = {}, {}
    if request.method == "POST":
        v = form_vals("username", "fullname", "student_no", "password", "confirm"); u = v["username"].strip().lower()
        key = "fp|" + (request.remote_addr or ""); r = q1("SELECT * FROM users WHERE username=?", u); wrong = False
        if is_locked(key): bad(e, "username", "Too many failed attempts. Please wait 5 minutes.")
        elif not u: bad(e, "username", "Username is required.")
        elif not r: bad(e, "username", "No account found with that username."); wrong = True
        elif r["role"] == "admin": bad(e, "username", "Admin passwords cannot be reset here. Run 'python app.py reset-admin' on the server.")
        else:
            if r["fullname"].strip().lower() != v["fullname"].strip().lower(): bad(e, "fullname", "Full name does not match our records."); wrong = True
            if r["student_no"].strip() != v["student_no"].strip(): bad(e, "student_no", "Student number does not match our records."); wrong = True
            if not v["password"]: bad(e, "password", "New password is required.")
            elif not v["confirm"]: bad(e, "confirm", "Please confirm your new password.")
            else: check_pw(e, v["password"], v["confirm"])
        if wrong: fail(key)
        if not e:
            FAILS.pop(key, None)
            ex("UPDATE users SET pw_hash=?, must_change=0 WHERE id=?", generate_password_hash(v["password"]), r["id"])
            flash("Your password has been changed. Please sign in.", "ok"); return redirect(url_for("login"))
        clear_bad(v, e)
    return render_template("forgot.html", v=v, e=e)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear(); return redirect(url_for("login"))

@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    v, e = {}, {}
    if request.method == "POST":
        v = form_vals("current", "password", "confirm")
        if not check_password_hash(g.user["pw_hash"], v["current"]): bad(e, "current", "Current password is incorrect.")
        if not v["password"]: bad(e, "password", "New password is required.")
        elif not v["confirm"]: bad(e, "confirm", "Please confirm your new password.")
        else:
            check_pw(e, v["password"], v["confirm"])
            if "password" not in e and v["password"] == v["current"]: bad(e, "password", "New password must be different from the current one.")
        if not e:
            ex("UPDATE users SET pw_hash=?, must_change=0 WHERE id=?", generate_password_hash(v["password"]), g.user["id"])
            flash("Password updated.", "ok"); return redirect(url_for("dashboard"))
        clear_bad(v, e)
    return render_template("account.html", v=v, e=e)

# ------------------------------------------------------------------ dashboards
def enrich(rows):
    out = []
    for r in rows:
        d = dict(r); d["fac"] = json.loads(r["facilities"] or "[]")
        d["its"] = [dict(i) for i in q("SELECT name,qty FROM items WHERE request_id=?", r["id"])]
        d["res"] = ", ".join(d["fac"] + ["%s x%d" % (i["name"], i["qty"]) for i in d["its"]]) or "-"
        d["badge"] = "Returned" if r["returned_at"] else r["status"]
        out.append(d)
    return out

def calendar_ctx():
    m = request.args.get("m") or today_ph().strftime("%Y-%m")
    try: y, mo = int(m[:4]), int(m[5:7]); date(y, mo, 1)
    except Exception: y, mo = today_ph().year, today_ph().month
    ym = "%04d-%02d" % (y, mo)
    first, last = ym + "-01", "%s-%02d" % (ym, monthrange(y, mo)[1])
    days, ev = {}, {}
    is_admin = bool(g.user and g.user["role"] == "admin")
    for r in enrich(q("""SELECT r.*, u.student_no AS u_sn, u.username AS u_user FROM requests r LEFT JOIN users u ON u.id=r.user_id
                         WHERE r.status IN ('Pending','Approved') AND r.event_date<=? AND COALESCE(r.return_date,r.event_date)>=?
                         ORDER BY r.event_date, r.t1""", last, first)):
        rd = r["return_date"] or r["event_date"]
        if is_admin:
            ev[r["id"]] = dict(no=r["rfu_no"], status=r["badge"], event=r["event"], requester=r["requester"], dept=r["dept"],
                               student=r.get("u_sn") or "", user=r.get("u_user") or "",
                               start=dfmt(r["event_date"]) + ", " + t12(r["t1"]), end=dfmt(rd) + ", " + t12(r["t2"]),
                               attendees=r["attendees"] or "", fac=", ".join(r["fac"]) or "-",
                               items=", ".join("%s x%d" % (i["name"], i["qty"]) for i in r["its"]) or "-", head=r["head"] or "",
                               remarks=r["remarks"] or "", filed=dt12(r["created_at"]), returned=dt12(r["returned_at"]) if r["returned_at"] else "",
                               pdf=url_for("request_pdf", rid=r["id"]), tab="returned" if r["returned_at"] else "approved" if r["status"] == "Approved" else "pending")
        for dd in range(int(max(r["event_date"], first)[8:10]), int(min(rd, last)[8:10]) + 1):
            key = "%s-%02d" % (ym, dd)
            lbl = ("%s %s" % (t12(r["t1"]), r["res"])) if key == r["event_date"] else ("Return %s: %s" % (t12(r["t2"]), r["res"])) if key == rd else ("Ongoing: " + r["res"])
            days.setdefault(dd, []).append(dict(r, lbl=lbl, rd=rd))
    py, pm = (y - 1, 12) if mo == 1 else (y, mo - 1); ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return dict(weeks=Calendar(6).monthdayscalendar(y, mo), days=days, title=date(y, mo, 1).strftime("%B %Y"),
                prev="%04d-%02d" % (py, pm), next="%04d-%02d" % (ny, nm), evdata=ev)

# ---------------------------------------------------------------- monthly report (admin)
def report_data(ym, by):
    """All requests received by the GSO in month `ym` (by date filed, or by event date) + summary numbers."""
    col = "r.event_date" if by == "event" else "r.created_at"
    rows = enrich(q("SELECT r.*, u.student_no AS u_sn FROM requests r LEFT JOIN users u ON u.id=r.user_id WHERE substr(%s,1,7)=? ORDER BY r.created_at, r.id" % col, ym))
    st = {"total": len(rows), "approved": 0, "disapproved": 0, "pending": 0, "returned": 0, "not_returned": 0}
    dept, fac, eq = {}, {}, {}
    for r in rows:
        if r["status"] == "Approved":
            st["approved"] += 1
            if r["returned_at"]: st["returned"] += 1
            elif r["its"]: st["not_returned"] += 1
        elif r["status"] == "Disapproved": st["disapproved"] += 1
        else: st["pending"] += 1
        k = (r["dept"] or "-").strip() or "-"; dept.setdefault(k.lower(), [k, 0])[1] += 1
        for f in r["fac"]: fac[f] = fac.get(f, 0) + 1
        for i in r["its"]: eq[i["name"]] = eq.get(i["name"], 0) + i["qty"]
    srt = lambda d: sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(rows=rows, st=st, depts=sorted(dept.values(), key=lambda x: (-x[1], x[0])), fac=srt(fac), eq=srt(eq))

def report_args():
    m = request.args.get("m") or today_ph().strftime("%Y-%m")
    try: y, mo = int(m[:4]), int(m[5:7]); date(y, mo, 1)
    except Exception: y, mo = today_ph().year, today_ph().month
    return y, mo, "%04d-%02d" % (y, mo), ("event" if request.args.get("by") == "event" else "filed")

def report_ctx():
    y, mo, ym, by = report_args()
    py, pm = (y - 1, 12) if mo == 1 else (y, mo - 1); ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
    months = [("%s" % r["ym"], date(int(r["ym"][:4]), int(r["ym"][5:7]), 1).strftime("%B %Y"), r["c"]) for r in
              q("SELECT substr(created_at,1,7) AS ym, COUNT(*) AS c FROM requests WHERE created_at IS NOT NULL GROUP BY substr(created_at,1,7) ORDER BY substr(created_at,1,7) DESC LIMIT 12")]
    return dict(rep=report_data(ym, by), ym=ym, by=by, rtitle=date(y, mo, 1).strftime("%B %Y"), rprev="%04d-%02d" % (py, pm), rnext="%04d-%02d" % (ny, nm), months=months)

@app.route("/admin/report.csv")
@admin_required
def report_csv():
    y, mo, ym, by = report_args(); rep = report_data(ym, by)
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["RFU No.", "Date filed", "Requester", "Student no.", "College/Department", "Event", "Start", "Return", "Facilities", "Equipment", "Attendees", "Status", "Remarks", "Returned on"])
    for r in rep["rows"]:
        rd = r["return_date"] or r["event_date"]
        w.writerow([r["rfu_no"], dt12(r["created_at"]), r["requester"], r.get("u_sn") or "", r["dept"], r["event"],
                    dfmt(r["event_date"]) + " " + t12(r["t1"]), dfmt(rd) + " " + t12(r["t2"]), "; ".join(r["fac"]),
                    "; ".join("%s x%d" % (i["name"], i["qty"]) for i in r["its"]), r["attendees"] or "", r["badge"], r["remarks"] or "",
                    dt12(r["returned_at"]) if r["returned_at"] else ""])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=GSO-report-%s.csv" % ym})

@app.route("/admin/report.pdf")
@admin_required
def report_pdf():
    y, mo, ym, by = report_args(); rep = report_data(ym, by); title = date(y, mo, 1).strftime("%B %Y"); st = rep["st"]
    ss = getSampleStyleSheet()
    sm = ParagraphStyle("sm", parent=ss["BodyText"], fontSize=8, leading=10)
    ctr = ParagraphStyle("ctr", parent=sm, alignment=1, fontSize=9, leading=12)
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=13, leading=16, spaceAfter=2)
    P = lambda t, style=sm: Paragraph(xml_escape(str(t)), style)
    lg = lambda n: Image(io.BytesIO(base64.b64decode(LOGOS[n])), 19 * mm, 19 * mm)
    GREEN = colors.HexColor("#14532d")
    head = Table([[lg("1"), [Paragraph("Southern Luzon State University - Judge Guillermo Eleazar", h1), Paragraph("General Services Office - Tagkawayan, Quezon", ctr),
                             Paragraph("<b>MONTHLY REPORT OF FACILITY / EQUIPMENT REQUESTS</b>", ctr), Paragraph("Period: <b>%s</b> (%s)" % (xml_escape(title), "by event date" if by == "event" else "by date filed"), ctr)], lg("2")]],
                 colWidths=[26 * mm, 217 * mm, 26 * mm]); head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (0, 0), "LEFT"), ("ALIGN", (2, 0), (2, 0), "RIGHT")]))
    labels = ["Total requests", "Approved", "Disapproved", "Pending", "Returned", "Approved, not yet returned"]
    vals = [st["total"], st["approved"], st["disapproved"], st["pending"], st["returned"], st["not_returned"]]
    summ = Table([labels, vals], colWidths=[269 * mm / 6] * 6)
    summ.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), GREEN), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                              ("FONTSIZE", (0, 0), (-1, 0), 8), ("FONTSIZE", (0, 1), (-1, 1), 16), ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"), ("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#b7d7c4")),
                              ("TOPPADDING", (0, 1), (-1, 1), 6), ("BOTTOMPADDING", (0, 1), (-1, 1), 6)]))
    def mini(ttl, pairs, unit):
        data = [[P(ttl, ParagraphStyle("t", parent=sm, textColor=colors.white, fontName="Helvetica-Bold")), P(unit, ParagraphStyle("t2", parent=sm, textColor=colors.white, fontName="Helvetica-Bold", alignment=2))]]
        data += [[P(a), P(b, ParagraphStyle("n", parent=sm, alignment=2))] for a, b in pairs[:8]] or [[P("-"), P("")]]
        t = Table(data, colWidths=[62 * mm, 22 * mm]); t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), GREEN), ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#c9d6cf")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])); return t
    trio = Table([[mini("College/Department", [(d[0], d[1]) for d in rep["depts"]], "Requests"), mini("Facility used", rep["fac"], "Times"), mini("Equipment borrowed", rep["eq"], "Qty")]], colWidths=[89.6 * mm] * 3)
    trio.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story = [head, Spacer(1, 4 * mm), summ, Spacer(1, 4 * mm), trio, Spacer(1, 5 * mm)]
    rows = [[P(x, ParagraphStyle("h", parent=sm, textColor=colors.white, fontName="Helvetica-Bold")) for x in ("RFU No.", "Date filed", "Requester / Department", "Event", "Schedule", "Facility / Equipment", "Status")]]
    for r in rep["rows"]:
        rows.append([P(r["rfu_no"]), P(dt12(r["created_at"])), P("%s - %s" % (r["requester"], r["dept"])), P(r["event"]), P(sched(r)), P(r["res"]), P(r["badge"] + (" (" + dt12(r["returned_at"]) + ")" if r["returned_at"] else ""))])
    if len(rows) == 1: story.append(Paragraph("No requests were received for this period.", ss["BodyText"]))
    else:
        t = Table(rows, colWidths=[19 * mm, 30 * mm, 44 * mm, 40 * mm, 52 * mm, 53 * mm, 31 * mm], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), GREEN), ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#c9d6cf")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f8f5")])])); story.append(t)
    def foot(c, d):
        c.setFont("Helvetica", 8); c.setFillColor(colors.grey)
        c.drawString(14 * mm, 8 * mm, "Generated %s - GSO Facility & Equipment Request System" % dt12(now_s())); c.drawRightString(landscape(A4)[0] - 14 * mm, 8 * mm, "Page %d" % d.page)
    buf = io.BytesIO(); SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm, topMargin=10 * mm, bottomMargin=14 * mm, title="GSO Monthly Report " + title).build(story, onFirstPage=foot, onLaterPages=foot)
    buf.seek(0); return send_file(buf, mimetype="application/pdf", download_name="GSO-report-%s.pdf" % ym)

# ---------------------------------------------------------------- notifications (students)
def notify(uid, rid, kind, msg):
    ex("INSERT INTO notifications(user_id,request_id,kind,message,created_at) VALUES(?,?,?,?,?)", uid, rid, kind, msg, now_s())

@app.route("/notifications/read", methods=["POST"])
@login_required
def notifications_read():
    ex("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", now_s(), g.user["id"])
    return ("", 204)

def student_ctx(tab):
    ctx = dict(tab=tab, today=today_ph().isoformat(), facilities=FACILITIES, v={}, e={}, old_items=[], old_fac=[], summary="",
               inv=[r["name"] for r in q("SELECT name FROM inventory ORDER BY name")])
    ctx["mine"] = enrich(q("SELECT * FROM requests WHERE user_id=? ORDER BY id DESC", g.user["id"]))
    ctx["notifs"] = [dict(x) for x in q("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20", g.user["id"])]
    ctx["unread"] = q1("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", g.user["id"])[0]
    if tab == "cal": ctx.update(calendar_ctx())
    return ctx

@app.route("/")
@login_required
def home(): return redirect(url_for("dashboard"))

@app.route("/dashboard")
@login_required
def dashboard():
    tab = request.args.get("tab", "")
    if g.user["role"] == "admin":
        tab = tab if tab in ("pending", "approved", "returned", "disapproved", "cal", "stock", "users", "report") else "pending"
        cnt = {k: q1(s)[0] for k, s in {
            "pending": "SELECT COUNT(*) FROM requests WHERE status='Pending'",
            "approved": "SELECT COUNT(*) FROM requests WHERE status='Approved' AND returned_at IS NULL",
            "returned": "SELECT COUNT(*) FROM requests WHERE status='Approved' AND returned_at IS NOT NULL",
            "disapproved": "SELECT COUNT(*) FROM requests WHERE status='Disapproved'"}.items()}
        ctx = dict(tab=tab, cnt=cnt)
        where = {"pending": "status='Pending'", "approved": "status='Approved' AND returned_at IS NULL",
                 "returned": "status='Approved' AND returned_at IS NOT NULL", "disapproved": "status='Disapproved'"}
        if tab in where: ctx["rows"] = enrich(q("SELECT * FROM requests WHERE %s ORDER BY id DESC" % where[tab]))
        elif tab == "cal": ctx.update(calendar_ctx())
        elif tab == "stock":
            ctx["inv"] = [dict(r, out=q1("""SELECT COALESCE(SUM(i.qty),0) FROM items i JOIN requests r ON r.id=i.request_id
                 WHERE i.name=? COLLATE NOCASE AND r.status='Approved' AND r.returned_at IS NULL AND (r.event_date||' '||r.t1)<=?""",
                 r["name"], now_s())[0]) for r in q("SELECT * FROM inventory ORDER BY name")]
        elif tab == "report": ctx.update(report_ctx())
        elif tab == "users": ctx["users"] = q("SELECT * FROM users ORDER BY role, fullname")
        return render_template("admin.html", **ctx)
    tab = tab if tab in ("new", "mine", "cal") else "new"
    return render_template("student.html", **student_ctx(tab))

# ------------------------------------------------------------------ student: new request
@app.route("/request", methods=["POST"])
@login_required
def new_request():
    f = request.form; s = lambda k: f.get(k, "").strip()
    v = {k: s(k) for k in ("requester", "dept", "event", "event_date", "return_date", "t1", "t2", "attendees", "head", "fac_other")}
    e = {}
    for k, label in (("requester", "Requester's name"), ("dept", "College/Department"), ("event", "Name/Type of event"),
                     ("event_date", "Date of the event"), ("t1", "Start time"), ("t2", "Return time")):
        if not v[k]: bad(e, k, label + " is required.")
    d, t1, t2 = v["event_date"], v["t1"], v["t2"]
    rd = v["return_date"] or d
    if d:
        try:
            datetime.strptime(d, "%Y-%m-%d")
            if d < today_ph().isoformat(): bad(e, "event_date", "The event date cannot be in the past. Choose today or a later date.")
        except ValueError: bad(e, "event_date", "Invalid date.")
    if v["return_date"]:
        try:
            datetime.strptime(rd, "%Y-%m-%d")
            if d and "event_date" not in e and rd < d: bad(e, "return_date", "The return date cannot be earlier than the event date.")
        except ValueError: bad(e, "return_date", "Invalid date.")
    for k in ("t1", "t2"):
        if v[k]:
            try: datetime.strptime(v[k], "%H:%M")
            except ValueError: bad(e, k, "Invalid time.")
    if t1 and t2 and "t1" not in e and "t2" not in e and "return_date" not in e and rd == d and t2 <= t1:
        bad(e, "t2", "Return time must be later than the start time when the return date is the same day.")
    if v["attendees"] and not v["attendees"].isdigit(): bad(e, "attendees", "Attendees must be a whole number.")
    slot_ok = bool(d and t1 and t2) and not any(k in e for k in ("event_date", "return_date", "t1", "t2"))
    st, en = (d + " " + t1, rd + " " + t2) if slot_ok else ("", "")
    fac = [x for x in f.getlist("fac") if x in FACILITIES]
    if v["fac_other"]: fac.append(v["fac_other"][:80])
    if slot_ok:
        busy = booked_facilities(st, en); ok_fac = []
        for x in fac:
            if x.lower() in busy:
                bad(e, "fac", '"%s" is already booked at that time, so it was unchecked. Choose another facility or time.' % x)
                if x == v["fac_other"][:80]: v["fac_other"] = ""
            else: ok_fac.append(x)
        fac = ok_fac
    rows, valid = [], {}                      # rows = what is shown again in the form, valid = what will be saved
    names_, others_, qtys_ = f.getlist("item_name"), f.getlist("item_other"), f.getlist("item_qty")
    others_ += [""] * (len(names_) - len(others_))
    for n, o, qy in zip(names_, others_, qtys_):
        other = n == "__other"; n = (o if other else n).strip()[:80]
        if not n:
            if other: bad(e, "eq", 'Type the equipment name in the "Other" row, or remove the row.'); rows.append(["__other", "", ""])
            continue
        canon, _ = stock_of(n)
        if canon is None and not other: bad(e, "eq", '"%s" is not in the equipment list, so it was removed.' % n); continue
        name = canon or n; sel, oth = (canon, "") if canon else ("__other", n)      # typed names that match the list count against its stock
        try: qy = int(qy)
        except ValueError: qy = 0
        if qy <= 0: bad(e, "eq", "%s: enter a quantity of at least 1." % name); rows.append([sel, "", oth]); continue
        if slot_ok and canon:
            avail = left(canon, st, en) - valid.get(canon, 0)
            if avail <= 0:
                bad(e, "eq", "%s is OUT OF STOCK for those dates and times (borrowed or reserved and not yet returned), so it was removed." % canon); continue
            if qy > avail:
                bad(e, "eq", "Only %d %s available for that date and time. Please re-enter the quantity." % (avail, canon)); rows.append([sel, "", oth]); continue
        valid[name] = valid.get(name, 0) + qy; rows.append([sel, qy, oth])
    if not e and not fac and not valid: bad(e, "fac", "Please choose a facility or equipment.")
    if e:
        clear_bad(v, e)
        ctx = student_ctx("new")
        ctx.update(v=v, e=e, old_items=rows, old_fac=fac,
                   summary="Please fix the highlighted items. Only the incorrect entries were cleared - everything else was kept.")
        return render_template("student.html", **ctx)
    c = ex("""INSERT INTO requests(user_id,requester,dept,event,event_date,t1,t2,attendees,facilities,head,created_at,return_date)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", g.user["id"], v["requester"][:120], v["dept"][:120], v["event"][:160], d, t1, t2,
           v["attendees"], json.dumps(fac), v["head"][:120], now_s(), rd)
    rid = c.lastrowid
    ex("UPDATE requests SET rfu_no=? WHERE id=?", "%d-%04d" % (today_ph().year, rid), rid)
    for n, qy in valid.items(): ex("INSERT INTO items(request_id,name,qty) VALUES(?,?,?)", rid, n, qy)
    flash("Request submitted! It is now pending admin approval.", "ok")
    return redirect(url_for("dashboard", tab="mine"))

# ------------------------------------------------------------------ admin actions
@app.route("/admin/request/<int:rid>/<action>", methods=["POST"])
@admin_required
def admin_action(rid, action):
    r = q1("SELECT * FROM requests WHERE id=?", rid)
    if not r: abort(404)
    if action == "approve":
        for i in q("SELECT name,qty FROM items WHERE request_id=?", rid):
            l = left(i["name"], r["event_date"] + " " + r["t1"], (r["return_date"] or r["event_date"]) + " " + r["t2"], exclude=rid)
            if l is not None and i["qty"] > l:
                flash("Cannot approve: only %d %s available for that time." % (l, i["name"]), "err")
                return redirect(url_for("dashboard", tab="pending"))
        ex("UPDATE requests SET status='Approved', remarks='', returned_at=NULL WHERE id=?", rid); flash("Request approved.", "ok")
        notify(r["user_id"], rid, "approved", "Your request %s (%s) was APPROVED. You may use the facility/equipment on the schedule you requested." % (r["rfu_no"], r["event"]))
    elif action == "disapprove" and r["returned_at"]:
        flash("This request is already marked as returned, so it can no longer be disapproved.", "err")
    elif action == "disapprove":
        why = request.form.get("remarks", "").strip()
        if not why: flash("Please enter a reason (remarks) for disapproval.", "err"); return redirect(request.referrer or url_for("dashboard"))
        ex("UPDATE requests SET status='Disapproved', remarks=?, returned_at=NULL WHERE id=?", why[:300], rid); flash("Request disapproved.", "ok")
        notify(r["user_id"], rid, "disapproved", "Your request %s (%s) was DISAPPROVED. Reason: %s" % (r["rfu_no"], r["event"], why[:300]))
    elif action == "returned" and r["status"] == "Approved":
        ex("UPDATE requests SET returned_at=? WHERE id=?", now_s(), rid); flash("Marked as returned. Stock is available again.", "ok")
    else: abort(400)
    return redirect(url_for("dashboard", tab=request.form.get("back", "pending")))

@app.route("/admin/stock", methods=["POST"])
@admin_required
def admin_stock():
    n = request.form.get("name", "").strip()[:80]
    try: s = max(0, int(request.form.get("stock", "0")))
    except ValueError: s = -1
    if not n or s < 0: flash("Enter a valid name and quantity.", "err")
    else:
        ex("INSERT INTO inventory(name,stock) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET stock=excluded.stock", n, s)
        flash("Stock saved.", "ok")
    return redirect(url_for("dashboard", tab="stock"))

@app.route("/admin/stock/delete", methods=["POST"])
@admin_required
def admin_stock_delete():
    n = request.form.get("name", "").strip()
    busy = q1("""SELECT COUNT(*) FROM items i JOIN requests r ON r.id=i.request_id WHERE i.name=? COLLATE NOCASE
                 AND r.returned_at IS NULL AND r.status IN ('Pending','Approved')""", n)[0]
    if busy: flash("Cannot delete %s: it is still in a pending request or borrowed and not yet returned. Resolve those requests first." % n, "err")
    else:
        ex("DELETE FROM inventory WHERE name=?", n); flash("%s was removed from the equipment list." % n, "ok")
    return redirect(url_for("dashboard", tab="stock"))

# ------------------------------------------------------------------ PDF
def make_pdf(r):
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4); H = 297 * mm
    its = q("SELECT name,qty FROM items WHERE request_id=?", r["id"]); fac = json.loads(r["facilities"] or "[]")
    def T(t, x, y, s=10, b=False, a="l", i=False):
        c.setFont("Helvetica-BoldOblique" if b and i else "Helvetica-Bold" if b else "Helvetica-Oblique" if i else "Helvetica", s)
        (c.drawCentredString if a == "c" else c.drawString)(x * mm, H - y * mm, str(t or ""))
    def L(x1, y, x2): c.line(x1 * mm, H - y * mm, x2 * mm, H - y * mm)
    def R(x, y, w, h): c.rect(x * mm, H - (y + h) * mm, w * mm, h * mm)
    def B(x, y, on):
        R(x, y - 3.5, 4, 4)
        if on: T("X", x + .8, y - .2, 9, True)
    for n, x in (("1", 28), ("2", 158)):
        c.drawImage(ImageReader(io.BytesIO(base64.b64decode(LOGOS[n]))), x * mm, H - 34 * mm, 24 * mm, 24 * mm, mask="auto")
    T("Southern Luzon State University", 105, 18, 13, True, "c"); T("Judge Guillermo Eleazar", 105, 23, 11, True, "c")
    T("GENERAL SERVICES OFFICE", 105, 28, 9, False, "c"); T("Tagkawayan, Quezon", 105, 32, 8, False, "c")
    R(140, 35, 55, 9); T("R.F.U. No:", 142, 39, 8); T(r["rfu_no"], 150, 42.5, 10, True)
    T("REQUEST FOR FACILITY/EQUIPMENT USE", 105, 52, 12, True, "c")
    y = 62
    T("Requester's Name:", 15, y); T(r["requester"], 52, y - .5); L(50, y + 1, 120); T("Date:", 130, y); T(dfmt(r["created_at"]), 142, y - .5); L(140, y + 1, 195); y += 8
    T("College/Department:", 15, y); T(r["dept"], 52, y - .5); L(50, y + 1, 120); y += 8
    T("Name/Type of Event:", 15, y); T(r["event"], 52, y - .5); L(50, y + 1, 195); y += 8
    T("Date of the Event:", 15, y); T(dfmt(r["event_date"]), 50, y - .5); L(48, y + 1, 100)
    T("Estimated Attendees:", 110, y); T(r["attendees"], 148, y - .5); L(146, y + 1, 195); y += 8
    T("Time Reserved:", 15, y); T(t12(r["t1"]), 45, y - .5); L(43, y + 1, 70); T("to", 74, y); T(t12(r["t2"]), 82, y - .5); L(80, y + 1, 108); y += 8
    T("Date to be Returned:", 15, y); T(dfmt(r["return_date"] or r["event_date"]), 55, y - .5); L(53, y + 1, 105); y += 9
    T("Facility to be used:", 15, y, 10, True); y += 7
    B(25, y, FACILITIES[0] in fac); T(FACILITIES[0], 31, y, 9); B(100, y, FACILITIES[1] in fac); T(FACILITIES[1], 106, y, 9); y += 7
    B(25, y, FACILITIES[2] in fac); T(FACILITIES[2], 31, y, 9); B(100, y, FACILITIES[3] in fac); T(FACILITIES[3], 106, y, 9); y += 7
    o = ", ".join(x for x in fac if x not in FACILITIES); B(25, y, bool(o)); T("others:", 31, y, 9); T(o, 46, y - .5, 9); L(44, y + 1, 120); y += 9
    # Equipment - laid out like the paper form: [ ] Sound System   Table ____   Chair ____   Other: ____
    from reportlab.pdfbase.pdfmetrics import stringWidth as _sw
    snd = tbl = chr_ = 0; other = []
    for i_ in its:
        nm = (i_["name"] or "").strip(); lo = nm.lower()
        if lo.startswith("sound"): snd += i_["qty"]
        elif lo in ("table", "tables"): tbl += i_["qty"]
        elif lo in ("chair", "chairs"): chr_ += i_["qty"]
        else: other.append("%s (%d)" % (nm, i_["qty"]))
    T("Equipment", 15, y, 10, True, "l", True)
    B(40, y, snd > 0); T("Sound System" + (" (%d)" % snd if snd > 1 else ""), 46, y, 9)
    T("Table", 84, y, 9); T(tbl or "", 97, y - .5, 9, True, "c"); L(94, y + 1, 112)
    T("Chair", 118, y, 9); T(chr_ or "", 132, y - .5, 9, True, "c"); L(129, y + 1, 147)
    otxt = ", ".join(other); inline = _sw(otxt, "Helvetica", 8) <= 30 * mm
    T("Other:", 152, y, 9)
    if inline: T(otxt, 164, y - .5, 8); L(163, y + 1, 195); y += 9
    else:
        L(163, y + 1, 195); T("see below", 170, y - .5, 8, False, "l", True); y += 6; ol = simpleSplit(otxt, "Helvetica", 9, 150 * mm)[:3]
        T("Other:", 15, y, 9, True, "l", True)
        for k, ln in enumerate(ol): T(ln, 40, y + 5 * k, 9)
        y += 5 * len(ol) + 3
    T("Requested by:", 15, y, 8); y += 12; T(r["head"], 60, y - 1, 10, True, "c"); L(30, y, 90)
    T("College Department/Department Head", 60, y + 4, 8, False, "c"); y += 12
    R(15, y, 180, 22); c.line(105 * mm, H - y * mm, 105 * mm, H - (y + 22) * mm); T("Recommending Approval:", 17, y + 5, 9)
    R(120, y + 1.5, 4, 4); T("X" if r["status"] == "Approved" else "", 120.8, y + 4.8, 9, True); T("Approved", 126, y + 5, 9)
    R(155, y + 1.5, 4, 4); T("X" if r["status"] == "Disapproved" else "", 155.8, y + 4.8, 9, True); T("Disapproved", 161, y + 5, 9)
    L(15, y + 15.2, 195); T("LUALHATI G. AGUILA", 60, y + 14, 9, True, "c"); T("Head, Business Affairs Office", 60, y + 19, 8, False, "c")
    T("RASELIETO B. GARCIA", 150, y + 14, 9, True, "c"); T("Head, General Services Office", 150, y + 19, 8, False, "c"); y += 30
    T("After Facility/Equipment Used:", 15, y, 9, True); y += 7; T("Findings/Observation/Recommendation:", 15, y, 9)
    note = "  |  ".join(x for x in (r["remarks"] or "", ("Returned: " + dt12(r["returned_at"])) if r["returned_at"] else "") if x)
    for k, ln in enumerate(simpleSplit(note, "Helvetica", 8, 175 * mm)[:2]): T(ln, 17, y + 6.5 + 8 * k, 8)
    L(15, y + 8, 195); L(15, y + 16, 195); y += 25
    T("Checked by:", 20, y); T("Noted:", 120, y); y += 12; L(20, y, 80); L(115, y, 195)
    T("GSO Supervisor", 50, y + 5, 9, False, "c"); T("RASELIETO B. GARCIA", 155, y - 1, 9, True, "c"); T("Head, General Services Office", 155, y + 5, 9, False, "c"); y += 14
    for k, ln in enumerate(["Note:", "  \u2022 Requester for the use of the facility is responsible for any damages.", "  \u2022 Please file three (3) days before the event.",
                            "  \u2022 Please attach any valid I.D. and contact number of requester for any purpose it may served."]):
        T(ln, 15, y + 4 * k, 8, True, "l", True)
    T("AFA-GSO-1.01F8, Rev. 0", 15, 283, 8); T("Effective 11/08/24", 15, 287.5, 8)
    c.showPage(); c.save(); buf.seek(0); return buf

@app.route("/request/<int:rid>/pdf")
@login_required
def request_pdf(rid):
    r = q1("SELECT * FROM requests WHERE id=?", rid)
    if not r or (g.user["role"] != "admin" and r["user_id"] != g.user["id"]): abort(404)
    return send_file(make_pdf(r), mimetype="application/pdf", download_name="RFU-%s.pdf" % r["rfu_no"])

# ------------------------------------------------------------------ templates
CSS = """
:root{--bg:#f3f5f9;--card:#fff;--tx:#1b2333;--mu:#5d6779;--pr:#14417b;--ac:#c9a227;--bd:#d9dfe9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,Segoe UI,Arial,sans-serif}
body.authbg{background:linear-gradient(rgba(255,255,255,.10),rgba(255,255,255,.10)),url('/bg.jpg') center/cover fixed no-repeat}
.authbg .card{background:rgba(255,255,255,.93)}
body.dash{background:url('/bg.jpg') center/cover fixed no-repeat}
.dash .card{background:rgba(255,255,255,.94)}.dash .tabs a{background:rgba(255,255,255,.92)}.dash .tabs a.on{background:var(--pr)}
.dash .wrap>p.mu{background:rgba(255,255,255,.92);padding:6px 10px;border-radius:6px}
header{background:#b7e4c7;color:#1b4332;border-bottom:4px solid #74c69d;padding:10px clamp(8px,2vw,22px);display:flex;flex-wrap:nowrap;justify-content:center;align-items:center;gap:clamp(6px,1.5vw,14px)}
header img{height:clamp(34px,8vw,62px);width:auto;display:block;flex:none}.ht{text-align:center;min-width:0;flex:0 1 auto}
header h1{font:600 clamp(11px,2.5vw,19px)/1.25 Georgia,serif;margin:0}header small{opacity:.85;display:block;font-size:clamp(9px,1.8vw,13px);line-height:1.3;margin-top:2px}
header a,header button.lk{color:#1b4332;background:none;border:0;padding:0;font:inherit;cursor:pointer;text-decoration:underline;margin:0;display:inline}
.wrap{max-width:980px;margin:22px auto;padding:0 16px}.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:22px;margin-bottom:18px}
h2{font:600 19px Georgia,serif;margin:0 0 14px;color:var(--pr)}label{display:block;font-size:13px;color:var(--mu);margin:10px 0 3px}
input,select{width:100%;padding:9px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--bg);color:var(--tx);font:inherit}
input[type=checkbox]{width:auto;margin-right:6px}.g{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}@media(max-width:600px){.g{grid-template-columns:1fr}}
button,.btn{background:var(--pr);color:#fff;border:0;padding:8px 14px;border-radius:6px;font:600 14px inherit;cursor:pointer;margin:8px 6px 0 0;text-decoration:none;display:inline-block}
button.s,.btn.s{background:transparent;color:var(--pr);border:1px solid var(--pr)}button.r{background:#a63232}button.gr{background:#2b7a47}button.sm{padding:4px 9px;font-size:12px;margin:2px 4px 2px 0}
a{color:var(--pr)}.ck label{display:inline-flex;align-items:center;margin:4px 16px 4px 0;color:var(--tx)}
.msg{padding:10px 14px;border-radius:6px;margin-bottom:14px}.msg.err{background:#f8d7d7;color:#8e2323}.msg.ok{background:#d5f0de;color:#1e6b3a}
.fe{color:#c0392b;font-size:12.5px;margin-top:3px}
.pw{position:relative}.pw input{padding-right:48px}button.eye{position:absolute;right:5px;top:50%;transform:translateY(-50%);margin:0;padding:0;width:34px;height:30px;display:grid;place-items:center;background:transparent;color:var(--pr);border:0;border-radius:8px}button.eye svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}button.eye:hover{background:rgba(20,65,123,.1)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--bd);vertical-align:top}th{color:var(--mu);font-weight:600}.tb{overflow-x:auto}
.b{padding:2px 9px;border-radius:12px;font-size:12px;font-weight:600;white-space:nowrap}.Pending{background:#fff1c9;color:#7a5a00}.Approved{background:#d5f0de;color:#1e6b3a}.Disapproved{background:#f8d7d7;color:#8e2323}.Returned{background:#dbe7fb;color:#14417b}
.tabs{display:flex;flex-wrap:wrap;margin-bottom:14px}.tabs a{margin:0 6px 6px 0;padding:8px 14px;border:1px solid var(--pr);border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;color:var(--pr)}.tabs a.on{background:var(--pr);color:#fff}
.auth{max-width:540px;width:100%;margin:clamp(20px,6vh,56px) auto;padding:clamp(22px,4vw,38px)}
.auth h2{font-size:26px;margin-bottom:18px}.auth label{font-size:14px;margin:14px 0 5px}.auth input{padding:12px 13px;font-size:16px}.auth .pw input{padding-right:54px}
.auth button:not(.eye),.auth .btn{padding:12px 24px;font-size:15px;margin-top:18px}.erow{display:flex;gap:8px;margin-bottom:6px}.erow select{flex:2}.erow input{flex:1}
.cal{width:100%;table-layout:fixed}.cal td,.cal th{border:1px solid var(--bd);height:74px;padding:3px;font-size:11px;overflow:hidden}.cal th{height:auto;text-align:center}
.cal i{display:block;font-style:normal;color:#fff;border-radius:3px;padding:0 4px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cal i.Approved{background:var(--pr)}.cal i.Pending{background:#c9a227}
.tabs .who a,.dash .tabs .who a{margin:0;padding:0;border:0;border-radius:0;font-weight:400;font-size:13px;background:none;text-decoration:underline;display:inline;color:var(--pr)}
.who{margin-left:auto;font-size:13px;padding:8px 2px;color:var(--tx)}.who button.lk{color:var(--pr);background:none;border:0;padding:0;margin:0;font:inherit;text-decoration:underline;cursor:pointer;display:inline}
.dash .who{background:rgba(255,255,255,.92);border-radius:6px;padding:8px 12px;margin-bottom:6px}
form.in{display:inline}.mu{color:var(--mu);font-size:12px}
/* ---------- animations: hover, click and page transitions ---------- */
button,.btn,a.nav{transition:transform .16s ease,box-shadow .16s ease,background-color .16s ease,color .16s ease,border-color .16s ease,filter .16s ease}
button:not(.eye):not(.menubtn):not(.bell):not(.x):not(.x2):hover:not(:disabled),.btn:hover{transform:translateY(-2px);box-shadow:0 7px 16px rgba(20,65,123,.3);filter:brightness(1.08)}
button.s:hover:not(:disabled),.btn.s:hover{background:rgba(20,65,123,.09);box-shadow:0 5px 12px rgba(20,65,123,.18)}
button:not(.eye):not(.menubtn):not(.bell):not(.x):not(.x2):active:not(:disabled),.btn:active{transform:translateY(0) scale(.95);box-shadow:none;filter:brightness(.94)}
.rip{position:absolute;border-radius:50%;background:rgba(255,255,255,.55);transform:scale(0);animation:rip .6s ease-out;pointer-events:none}button.s .rip,.btn.s .rip,.side .rip{background:rgba(20,65,123,.25)}.side a.nav .rip{background:rgba(255,255,255,.35)}
@keyframes rip{to{transform:scale(2.8);opacity:0}}
input,select{transition:border-color .16s ease,box-shadow .16s ease,transform .16s ease,background-color .16s ease}
input:hover:not(:disabled),select:hover:not(:disabled){border-color:#7fa6d6;box-shadow:0 2px 9px rgba(20,65,123,.15)}
input:focus,select:focus{outline:none;border-color:var(--pr);box-shadow:0 0 0 3px rgba(20,65,123,.22);transform:translateY(-1px)}
input[type=checkbox]{transition:transform .15s ease}input[type=checkbox]:hover{transform:scale(1.2)}
.ck label{transition:transform .15s ease}.ck label:hover{transform:translateX(3px)}
a{transition:opacity .15s ease,color .15s ease,background-color .15s ease}a:hover{opacity:.8}
.tb tr{transition:background-color .15s ease}.tb tr:hover td{background:rgba(20,65,123,.06)}
.wrap>.card,.wrap>.msg,.wrap>p,.wrap>form{animation:pageIn .42s cubic-bezier(.22,.8,.3,1) both}.wrap>.card:nth-of-type(2){animation-delay:.06s}.wrap>.card:nth-of-type(3){animation-delay:.12s}
@keyframes pageIn{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
body.leaving .wrap>.card,body.leaving .wrap>.msg,body.leaving .wrap>p,body.leaving .wrap>form{opacity:0;transform:translateY(-10px);animation:none;transition:opacity .18s ease,transform .18s ease}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
.tp{display:flex;gap:6px}.tp select{flex:1;min-width:0;padding:9px 6px}
/* ---------- side menu (new style): dark-green drawer, icons, gold active pill ---------- */
.menubtn{position:fixed;left:0;top:112px;z-index:40;width:40px;height:58px;margin:0;padding:0;border:0;border-radius:0 18px 18px 0;background:linear-gradient(135deg,#2d6a4f,#14532d);color:#fff;display:grid;place-items:center;box-shadow:3px 4px 12px rgba(20,83,45,.45);transition:width .2s ease,box-shadow .2s ease}
.menubtn:hover{width:50px;box-shadow:4px 6px 16px rgba(20,83,45,.6)}.menubtn svg,.side svg,.bell svg,.toast svg,.ni svg,.sideuser .x svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round;flex:none}
.sideBg{position:fixed;inset:0;background:rgba(6,30,18,.5);backdrop-filter:blur(2px);z-index:50;opacity:0;pointer-events:none;transition:opacity .25s ease}.sideBg.open{opacity:1;pointer-events:auto}
.side{position:fixed;top:0;left:0;bottom:0;width:min(300px,86vw);background:linear-gradient(185deg,#14532d 0%,#1b4332 55%,#0f3d2a 100%);color:#e8f5ec;z-index:60;transform:translateX(-106%);transition:transform .3s cubic-bezier(.22,.8,.3,1);display:flex;flex-direction:column;box-shadow:6px 0 28px rgba(0,0,0,.4);overflow-y:auto;border-radius:0 24px 24px 0}
.side.open{transform:none}.sideuser{display:flex;align-items:center;gap:12px;padding:22px 18px 16px;border-bottom:1px solid rgba(255,255,255,.14)}
.av{width:46px;height:46px;border-radius:50%;background:linear-gradient(135deg,#e0bb4a,#c9a227);color:#1b3a1b;display:grid;place-items:center;font:700 20px Georgia,serif;flex:none}
.who2{display:flex;flex-direction:column;min-width:0;flex:1}.who2 b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:15px}.who2 span{font-size:12px;color:#95d5b2;text-transform:uppercase;letter-spacing:1px}
.side .x{background:rgba(255,255,255,.1);color:#fff;border:0;border-radius:50%;width:32px;height:32px;padding:0;margin:0;display:grid;place-items:center}.side .x:hover{background:rgba(255,255,255,.22);transform:rotate(90deg)}
.navlabel{padding:16px 22px 6px;font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:#95d5b2}
.side a.nav{position:relative;overflow:hidden;display:flex;align-items:center;gap:12px;margin:3px 12px;padding:11px 14px;border-radius:14px;color:#d8f3dc;font-weight:600;text-decoration:none}
.side a.nav:hover{background:rgba(255,255,255,.12);transform:translateX(5px);opacity:1}.side a.nav.on{background:linear-gradient(90deg,#c9a227,#e6c453);color:#173a1f;box-shadow:0 4px 12px rgba(0,0,0,.25)}
.side .cnt{margin-left:auto;background:rgba(255,255,255,.2);border-radius:20px;padding:0 9px;font-size:12px;font-weight:700}.side a.nav.on .cnt{background:rgba(0,0,0,.14)}
.side.open a.nav,.side.open .navlabel{animation:slideIn .38s ease both;animation-delay:calc(var(--i,1)*35ms)}
@keyframes slideIn{from{opacity:0;transform:translateX(-18px)}to{opacity:1;transform:none}}
.side .foot{margin-top:auto;padding:14px 12px 20px;border-top:1px solid rgba(255,255,255,.14)}
.side .signout{display:flex;align-items:center;justify-content:center;gap:10px;width:calc(100% - 0px);margin:8px 0 0;padding:11px;background:rgba(255,255,255,.1);color:#fff;border:1px solid rgba(255,255,255,.25);border-radius:14px}.side .signout:hover{background:#a63232;border-color:#a63232}
@media(max-width:700px){.dash .wrap{padding-left:46px;padding-right:12px}}
/* ---------- notification bell + panel + toast (students) ---------- */
.bell{position:fixed;right:14px;top:100px;z-index:40;width:46px;height:46px;margin:0;padding:0;border-radius:50%;border:0;background:#fff;color:#14532d;display:grid;place-items:center;box-shadow:0 4px 14px rgba(0,0,0,.25);transition:transform .18s ease,box-shadow .18s ease}
.bell:hover{transform:scale(1.1);box-shadow:0 8px 20px rgba(0,0,0,.3)}.bell:active{transform:scale(.94)}.bell.ring svg{animation:ring 1.4s ease infinite;transform-origin:50% 0}
@keyframes ring{0%,60%,100%{transform:rotate(0)}10%{transform:rotate(16deg)}20%{transform:rotate(-14deg)}30%{transform:rotate(10deg)}40%{transform:rotate(-8deg)}50%{transform:rotate(4deg)}}
.badge{position:absolute;top:1px;right:1px;min-width:19px;height:19px;border-radius:10px;background:#d62828;color:#fff;font:700 11px/19px system-ui;text-align:center;padding:0 5px;box-shadow:0 0 0 2px #fff}
.npanel{position:fixed;right:14px;top:154px;z-index:45;width:min(380px,calc(100vw - 28px));max-height:62vh;overflow:auto;background:#fff;border-radius:16px;box-shadow:0 14px 40px rgba(0,0,0,.35);opacity:0;transform:translateY(-10px) scale(.97);pointer-events:none;transition:opacity .2s ease,transform .2s ease}
.npanel.open{opacity:1;transform:none;pointer-events:auto}.nh{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;background:linear-gradient(135deg,#14532d,#2d6a4f);color:#fff;border-radius:16px 16px 0 0;position:sticky;top:0}
.nh .x2{background:none;border:0;color:#fff;font-size:24px;line-height:1;padding:0 4px;margin:0}
.ni{display:flex;gap:12px;padding:13px 18px;border-bottom:1px solid #edf1ee;text-decoration:none;color:var(--tx);font-size:14px;transition:background-color .15s ease,transform .15s ease}.ni:hover{background:#f1f8f3;transform:translateX(3px);opacity:1}
.ni.new{background:#f4fbf6;border-left:4px solid #2b7a47}.ni .dot{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;flex:none;color:#fff}.ni.approved .dot{background:#2b7a47}.ni.disapproved .dot{background:#b3261e}
.ni small{display:block;color:var(--mu);font-size:11.5px;margin-top:3px}.npanel .empty{padding:26px 18px;color:var(--mu);font-size:13.5px;text-align:center}
.toast{position:fixed;right:14px;bottom:18px;z-index:70;display:flex;align-items:center;gap:10px;background:#14532d;color:#fff;padding:12px 18px;border-radius:14px;box-shadow:0 10px 28px rgba(0,0,0,.35);cursor:pointer;animation:toastIn .45s cubic-bezier(.2,.9,.3,1.2) both;transition:opacity .4s ease,transform .4s ease}
.toast.hide{opacity:0;transform:translateY(20px);pointer-events:none}@keyframes toastIn{from{opacity:0;transform:translateY(30px) scale(.9)}to{opacity:1;transform:none}}
/* ---------- calendar event details (admin) ---------- */
.cal i.clk{cursor:pointer}.cal i.clk:hover{transform:scale(1.05);filter:brightness(1.15);box-shadow:0 3px 8px rgba(0,0,0,.3)}
.modalbg{position:fixed;inset:0;background:rgba(6,30,18,.55);backdrop-filter:blur(3px);z-index:80;display:none;align-items:center;justify-content:center;padding:14px}.modalbg.open{display:flex;animation:fade .2s ease}
@keyframes fade{from{opacity:0}to{opacity:1}}@keyframes pop{from{opacity:0;transform:scale(.92) translateY(10px)}to{opacity:1;transform:none}}
.modal{background:#fff;border-radius:18px;max-width:580px;width:100%;max-height:90vh;overflow:auto;box-shadow:0 24px 60px rgba(0,0,0,.4);animation:pop .28s cubic-bezier(.2,.9,.3,1.1)}
.mh{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:18px 22px;background:linear-gradient(135deg,#14532d,#2d6a4f);color:#fff;border-radius:18px 18px 0 0}.mh h3{margin:2px 0 0;font:600 20px Georgia,serif}.mh .mu2{font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#b7e4c7}
.mh .x{background:rgba(255,255,255,.15);color:#fff;border:0;border-radius:50%;width:34px;height:34px;padding:0;margin:0;display:grid;place-items:center;flex:none}.mh .x svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2.4;stroke-linecap:round}.mh .x:hover{background:rgba(255,255,255,.3);transform:rotate(90deg)}
.mb{padding:18px 22px 22px}.mb dl{display:grid;grid-template-columns:150px 1fr;gap:9px 14px;margin:0 0 14px}.mb dt{color:var(--mu);font-size:13px}.mb dd{margin:0;font-weight:500;word-break:break-word}
@media(max-width:480px){.mb dl{grid-template-columns:1fr;gap:2px}.mb dt{margin-top:8px}}
/* ---------- monthly report ---------- */
.rbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:10px}.rf{display:flex;flex-wrap:wrap;gap:8px;align-items:center;flex:1;min-width:220px}.rf input,.rf select{width:auto;flex:1;min-width:130px}.rf button,.rbar .btn{margin:0}
.chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0 12px}.chips a{padding:4px 12px;border-radius:20px;border:1px solid #b7d7c4;background:#f2f8f4;color:#14532d;text-decoration:none;font-size:12.5px;font-weight:600}.chips a.on,.chips a:hover{background:#14532d;color:#fff;opacity:1}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin:14px 0}.stat{background:#f2f8f4;border:1px solid #cfe6d8;border-radius:14px;padding:12px 14px;transition:transform .18s ease,box-shadow .18s ease}
.stat:hover{transform:translateY(-4px);box-shadow:0 10px 20px rgba(20,83,45,.18)}.stat b{display:block;font:700 28px Georgia,serif;color:#14532d}.stat span{font-size:12px;color:var(--mu)}
.g3{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-bottom:12px}.g3 h4{margin:0 0 6px;font-size:13px;color:#14532d}
"""
T = {}
T["base.html"] = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Facility &amp; Equipment Request - SLSU</title><style>""" + CSS + """</style></head><body class="{{ 'authbg' if request.endpoint in ('login', 'signup', 'forgot', 'account') else 'dash' if request.endpoint in ('dashboard', 'new_request') }}">
<header><img src="{{ url_for('logo', n='1') }}" alt="SLSU">
<div class="ht"><h1>Southern Luzon State University - Judge Guillermo Eleazar</h1><small>General Services Office - Facility &amp; Equipment Request</small></div>
<img src="{{ url_for('logo', n='2') }}" alt="SLSU-JGE"></header>
<div class="wrap">{% for c, m in get_flashed_messages(with_categories=true) %}<div class="msg {{ c }}">{{ m }}</div>{% endfor %}{% block body %}{% endblock %}</div>
<script>var EYE={{ ('<svg viewBox="0 0 24 24">' ~ ICONS['eye'] ~ '</svg>')|tojson }},EYEOFF={{ ('<svg viewBox="0 0 24 24">' ~ ICONS['eyeoff'] ~ '</svg>')|tojson }};
document.querySelectorAll('input[type=password]').forEach(function(i){var w=document.createElement('div');w.className='pw';i.parentNode.insertBefore(w,i);w.appendChild(i);
var b=document.createElement('button');b.type='button';b.className='eye';b.innerHTML=EYE;b.setAttribute('aria-label','Show password');b.onclick=function(){var h=i.type==='password';i.type=h?'text':'password';b.innerHTML=h?EYEOFF:EYE;b.setAttribute('aria-label',h?'Hide password':'Show password')};w.appendChild(b)});
function menu(o){var s=document.getElementById('side'),b=document.getElementById('sideBg');if(!s)return;if(o===undefined)o=!s.classList.contains('open');s.classList.toggle('open',o);b.classList.toggle('open',o)}
document.addEventListener('keydown',function(e){if(e.key==='Escape')menu(false)});
document.addEventListener('click',function(e){var b=e.target.closest('button:not(.bell):not(.x):not(.x2):not(.menubtn),.btn,a.nav');if(!b||b.disabled)return;
 var cs=getComputedStyle(b);if(cs.position==='static')b.style.position='relative';b.style.overflow='hidden';
 var r=b.getBoundingClientRect(),z=Math.max(r.width,r.height),p=document.createElement('span');p.className='rip';
 p.style.cssText='width:'+z+'px;height:'+z+'px;left:'+(e.clientX-r.left-z/2)+'px;top:'+(e.clientY-r.top-z/2)+'px';b.appendChild(p);setTimeout(function(){p.remove()},650)});
document.addEventListener('click',function(e){var a=e.target.closest('a[href]');if(!a||a.target||a.hasAttribute('download')||e.defaultPrevented||e.button||e.ctrlKey||e.metaKey||e.shiftKey||e.altKey)return;
 var u=new URL(a.href,location.href);if(u.origin!==location.origin||/(pdf|csv)$/.test(u.pathname)||u.href===location.href||u.hash&&u.pathname===location.pathname&&u.search===location.search)return;
 e.preventDefault();document.body.classList.add('leaving');setTimeout(function(){location.href=a.href},200)});
document.addEventListener('submit',function(e){if(!e.defaultPrevented)document.body.classList.add('leaving')});
window.addEventListener('pageshow',function(){document.body.classList.remove('leaving')})</script>
</body></html>"""
T["macros.html"] = """
{% macro fe(e, k) %}{% for x in e.get(k, []) %}<div class="fe">{{ x }}</div>{% endfor %}{% endmacro %}
{% macro ico(n) %}<svg viewBox="0 0 24 24" aria-hidden="true">{{ ICONS[n]|safe }}</svg>{% endmacro %}
{% macro side(items, tab) %}<button type="button" class="menubtn" onclick="menu(true)" aria-label="Open menu">{{ ico('menu') }}</button><div id="sideBg" class="sideBg" onclick="menu(false)"></div>
<nav id="side" class="side"><div class="sideuser"><div class="av">{{ (g.user.fullname or '?')[:1]|upper }}</div><div class="who2"><b>{{ g.user.fullname }}</b><span>{{ g.user.role }}</span></div><button type="button" class="x" onclick="menu(false)" aria-label="Close menu">{{ ico('x') }}</button></div>
{% for k, label, n, ic in items %}{% if k == '__label' %}<div class="navlabel" style="--i:{{ loop.index }}">{{ label }}</div>{% else %}<a class="nav {{ 'on' if tab == k }}" style="--i:{{ loop.index }}" href="{{ url_for('dashboard', tab=k) }}">{{ ico(ic) }}<span>{{ label }}</span>{% if n is not none %}<span class="cnt">{{ n }}</span>{% endif %}</a>{% endif %}{% endfor %}
<div class="foot"><a class="nav" href="{{ url_for('account') }}">{{ ico('lock') }}<span>Change password</span></a><form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="_csrf" value="{{ csrf() }}"><button class="signout">{{ ico('out') }}<span>Sign out</span></button></form></div></nav>{% endmacro %}
{% macro bell(notifs, unread) %}<button type="button" class="bell {{ 'ring' if unread }}" onclick="toggleN()" aria-label="Notifications">{{ ico('bell') }}{% if unread %}<span class="badge" id="nBadge">{{ unread }}</span>{% endif %}</button>
<div id="npanel" class="npanel"><div class="nh"><b>Notifications</b><button type="button" class="x2" onclick="toggleN(false)" aria-label="Close">&times;</button></div>
{% for n in notifs %}<a class="ni {{ 'new' if not n.read_at }} {{ n.kind }}" href="{{ url_for('dashboard', tab='mine') }}"><span class="dot">{{ ico('check' if n.kind == 'approved' else 'x') }}</span><span class="nt">{{ n.message }}<small>{{ n.created_at|dt12 }}</small></span></a>{% else %}<div class="empty">No notifications yet. You will be notified here when your request is approved or disapproved.</div>{% endfor %}</div>
{% if unread %}<div id="toast" class="toast" onclick="toggleN(true)">{{ ico('bell') }}<span>You have {{ unread }} new notification{{ 's' if unread != 1 }}</span></div>{% endif %}
<script>var NTOK={{ csrf()|tojson }};
function toggleN(o){var p=document.getElementById('npanel');if(o===undefined)o=!p.classList.contains('open');p.classList.toggle('open',o);var t=document.getElementById('toast');if(t)t.remove();
 var b=document.getElementById('nBadge');if(o&&b){b.remove();document.querySelector('.bell').classList.remove('ring');fetch('/notifications/read',{method:'POST',body:new URLSearchParams({_csrf:NTOK}),credentials:'same-origin'})}}
document.addEventListener('keydown',function(e){if(e.key==='Escape')toggleN(false)});
setTimeout(function(){var t=document.getElementById('toast');if(t)t.classList.add('hide')},6500)</script>{% endmacro %}
{% macro when(r) %}{% set rd = r.return_date or r.event_date %}{% if rd == r.event_date %}{{ r.event_date|dfmt }}<br><span class="mu">{{ r.t1|t12 }} - {{ r.t2|t12 }}</span>{% else %}<b>From:</b> {{ r.event_date|dfmt }} <span class="mu">{{ r.t1|t12 }}</span><br><b>Return:</b> {{ rd|dfmt }} <span class="mu">{{ r.t2|t12 }}</span>{% endif %}{% endmacro %}
{% macro token() %}<input type="hidden" name="_csrf" value="{{ csrf() }}">{% endmacro %}
{% macro table(rows, admin=False, tab='', title='') %}<div class="card">{% if title %}<h2>{{ title }}</h2>{% endif %}<div class="tb"><table><tr><th>RFU No.</th>{% if admin %}<th>Requester</th>{% endif %}<th>Event</th><th>Date &amp; time</th><th>Resources</th><th>Status</th><th>Actions</th></tr>
{% for r in rows %}<tr><td>{{ r.rfu_no }}</td>{% if admin %}<td>{{ r.requester }}<br><span class="mu">{{ r.dept }}</span></td>{% endif %}
<td>{{ r.event }}</td><td>{{ when(r) }}</td><td><span class="mu" style="font-size:13px">{{ r.res }}</span></td>
<td><span class="b {{ r.badge }}">{{ r.badge }}</span>{% if r.status == 'Approved' and not r.returned_at and r.its %}<br><span class="mu">Not yet returned</span>{% endif %}
{% if r.returned_at %}<br><span class="mu">Returned {{ r.returned_at|dt12 }}</span>{% endif %}{% if r.remarks %}<br><span class="mu">Remarks: {{ r.remarks }}</span>{% endif %}</td>
<td><a href="{{ url_for('request_pdf', rid=r.id) }}">PDF</a>
{% if admin %}<br>{% if r.status != 'Approved' %}<form class="in" method="post" action="{{ url_for('admin_action', rid=r.id, action='approve') }}">{{ token() }}<input type="hidden" name="back" value="{{ tab }}"><button class="sm gr">Approve</button></form>{% endif %}
{% if r.status == 'Approved' and not r.returned_at %}<form class="in" method="post" action="{{ url_for('admin_action', rid=r.id, action='returned') }}">{{ token() }}<input type="hidden" name="back" value="{{ tab }}"><button class="sm">{{ 'Mark Returned' if r.its else 'Mark Completed' }}</button></form>{% endif %}
{% if r.status != 'Disapproved' and not r.returned_at %}<form method="post" action="{{ url_for('admin_action', rid=r.id, action='disapprove') }}">{{ token() }}<input type="hidden" name="back" value="{{ tab }}"><input name="remarks" placeholder="Reason (required)" required style="padding:4px;font-size:12px;margin-top:4px"><button class="sm r">Disapprove</button></form>{% endif %}{% endif %}</td></tr>
{% else %}<tr><td colspan="7">No requests.</td></tr>{% endfor %}</table></div></div>{% endmacro %}
{% macro calendar(weeks, days, title, prev, next, tab, detail=False, evdata=none) %}<div class="card"><h2>Booking Calendar</h2>
<div style="display:flex;justify-content:space-between;align-items:center"><a class="btn s" href="{{ url_for('dashboard', tab=tab, m=prev) }}">&lsaquo;</a><b>{{ title }}</b><a class="btn s" href="{{ url_for('dashboard', tab=tab, m=next) }}">&rsaquo;</a></div>
<table class="cal" style="margin-top:8px"><tr>{% for d in ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'] %}<th>{{ d }}</th>{% endfor %}</tr>
{% for w in weeks %}<tr>{% for d in w %}<td>{% if d %}{{ d }}{% for r in days.get(d, []) %}<i class="{{ r.status }}{{ ' clk' if detail }}" {% if detail %}onclick="showEv({{ r.id }})" onkeydown="if(event.key==='Enter')showEv({{ r.id }})" tabindex="0" role="button"{% endif %} title="{{ r.event }}: {{ r.event_date|dfmt }} {{ r.t1|t12 }} to {{ r.rd|dfmt }} {{ r.t2|t12 }} - {{ r.res }}">{{ r.lbl }}</i>{% endfor %}{% endif %}</td>{% endfor %}</tr>{% endfor %}</table>
<p class="mu">Navy = Approved &middot; Gold = Pending. {{ 'Click an event to see its full details.' if detail else 'Hover an entry for details.' }}</p></div>
{% if detail %}<div id="evBg" class="modalbg" onclick="if(event.target===this)hideEv()"><div class="modal" role="dialog" aria-modal="true"><div class="mh"><div><div class="mu2" id="evNo"></div><h3 id="evTitle"></h3></div><button type="button" class="x" onclick="hideEv()" aria-label="Close"><svg viewBox="0 0 24 24">{{ ICONS['x']|safe }}</svg></button></div><div class="mb"><dl id="evBody"></dl><div id="evAct"></div></div></div></div>
<script id="evdata" type="application/json">{{ evdata|tojson }}</script>
<script>(function(){var D=JSON.parse(document.getElementById('evdata').textContent),bg=document.getElementById('evBg');
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
window.showEv=function(id){var d=D[id];if(!d)return;document.getElementById('evNo').textContent='RFU No. '+d.no;document.getElementById('evTitle').textContent=d.event;
 var rows=[['Status',d.status],['Requester',d.requester],['College/Department',d.dept],['Student no.',d.student],['Username',d.user],['Start',d.start],['Return',d.end],['Est. attendees',d.attendees],['Facility',d.fac],['Equipment',d.items],['Requested by (head)',d.head],['Date filed',d.filed],['Remarks',d.remarks],['Returned on',d.returned]],h='';
 rows.forEach(function(r){if(r[1]===''||r[1]==null)return;h+='<dt>'+esc(r[0])+'</dt><dd>'+(r[0]==='Status'?'<span class="b '+esc(d.status)+'">'+esc(d.status)+'</span>':esc(r[1]))+'</dd>'});
 document.getElementById('evBody').innerHTML=h;
 document.getElementById('evAct').innerHTML='<a class="btn" href="'+esc(d.pdf)+'">Open PDF form</a><a class="btn s" href="/dashboard?tab='+esc(d.tab)+'">View in list</a>';
 bg.classList.add('open')};
window.hideEv=function(){bg.classList.remove('open')};document.addEventListener('keydown',function(e){if(e.key==='Escape')hideEv()})})()</script>{% endif %}{% endmacro %}"""
T["login.html"] = """{% extends 'base.html' %}{% block body %}{% import 'macros.html' as m %}<form class="card auth" method="post"><h2>Sign In</h2>{{ m.token() }}
<label>Username</label><input name="username" value="{{ v.get('username', '') }}" autofocus>{{ m.fe(e, 'username') }}
<label>Password</label><input name="password" type="password" value="{{ v.get('password', '') }}">{{ m.fe(e, 'password') }}
<div style="text-align:right;margin-top:4px"><a href="{{ url_for('forgot') }}" style="font-size:12px">Forgot password?</a></div>
<button>Sign In</button><p><a href="{{ url_for('signup') }}">Create an account</a></p></form>{% endblock %}"""
T["signup.html"] = """{% extends 'base.html' %}{% block body %}{% import 'macros.html' as m %}<form class="card auth" method="post"><h2>Sign Up</h2>{{ m.token() }}
<label>Full name</label><input name="fullname" value="{{ v.get('fullname', '') }}">{{ m.fe(e, 'fullname') }}
<label>Student number</label><input name="student_no" value="{{ v.get('student_no', '') }}">{{ m.fe(e, 'student_no') }}
<label>Username</label><input name="username" value="{{ v.get('username', '') }}">{{ m.fe(e, 'username') }}
<label>Password (min. 8 characters)</label><input name="password" type="password" value="{{ v.get('password', '') }}">{{ m.fe(e, 'password') }}
<label>Confirm password</label><input name="confirm" type="password" value="{{ v.get('confirm', '') }}">{{ m.fe(e, 'confirm') }}
<button>Create Account</button><a class="btn s" href="{{ url_for('login') }}">Back</a></form>{% endblock %}"""
T["forgot.html"] = """{% extends 'base.html' %}{% block body %}{% import 'macros.html' as m %}<form class="card auth" method="post"><h2>Forgot Password</h2>{{ m.token() }}
<p class="mu">Enter the details you registered with to verify your identity. Admin accounts must be reset on the server.</p>
<label>Username</label><input name="username" value="{{ v.get('username', '') }}">{{ m.fe(e, 'username') }}
<label>Full name</label><input name="fullname" value="{{ v.get('fullname', '') }}">{{ m.fe(e, 'fullname') }}
<label>Student number</label><input name="student_no" value="{{ v.get('student_no', '') }}">{{ m.fe(e, 'student_no') }}
<label>New password (min. 8 characters)</label><input name="password" type="password" value="{{ v.get('password', '') }}">{{ m.fe(e, 'password') }}
<label>Confirm new password</label><input name="confirm" type="password" value="{{ v.get('confirm', '') }}">{{ m.fe(e, 'confirm') }}
<button>Reset Password</button><a class="btn s" href="{{ url_for('login') }}">Back</a></form>{% endblock %}"""
T["account.html"] = """{% extends 'base.html' %}{% block body %}{% import 'macros.html' as m %}<form class="card auth" method="post"><h2>Change Password</h2>{{ m.token() }}
<label>Current password</label><input name="current" type="password" value="{{ v.get('current', '') }}">{{ m.fe(e, 'current') }}
<label>New password (min. 8 characters)</label><input name="password" type="password" value="{{ v.get('password', '') }}">{{ m.fe(e, 'password') }}
<label>Confirm new password</label><input name="confirm" type="password" value="{{ v.get('confirm', '') }}">{{ m.fe(e, 'confirm') }}
<button>Update Password</button>{% if not g.user.must_change %}<a class="btn s" href="{{ url_for('dashboard') }}">Cancel</a>{% endif %}</form>
{% if g.user.must_change %}<form class="in" method="post" action="{{ url_for('logout') }}" style="display:block;text-align:center"><input type="hidden" name="_csrf" value="{{ csrf() }}"><button class="s">Sign out</button></form>{% endif %}{% endblock %}"""
T["student.html"] = """{% extends 'base.html' %}{% block body %}{% import 'macros.html' as m %}
{{ m.side([('new','New Request',none,'new'),('mine','My Requests',mine|length,'list'),('cal','Calendar',none,'cal')], tab) }}
{{ m.bell(notifs, unread) }}
{% if tab == 'new' %}{% if summary %}<div class="msg err">{{ summary }}</div>{% endif %}
<form class="card" method="post" action="{{ url_for('new_request') }}"><h2>Request for Facility/Equipment Use</h2>{{ m.token() }}<div class="g">
<div><label>Requester's name *</label><input name="requester" value="{{ v.requester if 'requester' in v else g.user.fullname }}">{{ m.fe(e, 'requester') }}</div><div><label>Date</label><input value="{{ today }}" disabled></div>
<div><label>College/Department *</label><input name="dept" value="{{ v.get('dept', '') }}">{{ m.fe(e, 'dept') }}</div><div><label>Name/Type of event *</label><input name="event" value="{{ v.get('event', '') }}">{{ m.fe(e, 'event') }}</div>
<div><label>Date of the event (start) *</label><input id="ed" name="event_date" type="date" min="{{ today }}" value="{{ v.get('event_date', '') }}">{{ m.fe(e, 'event_date') }}</div>
<div><label>Return date (when the items/facility will be returned) *</label><input id="rd" name="return_date" type="date" min="{{ today }}" value="{{ v.get('return_date', '') }}">{{ m.fe(e, 'return_date') }}</div>
<div><label>Start time (AM/PM) *</label><input id="t1" name="t1" type="time" value="{{ v.get('t1', '') }}">{{ m.fe(e, 't1') }}</div><div><label>Return time (AM/PM) *</label><input id="t2" name="t2" type="time" value="{{ v.get('t2', '') }}">{{ m.fe(e, 't2') }}</div>
<div><label>Estimated attendees (optional)</label><input name="attendees" type="number" min="0" value="{{ v.get('attendees', '') }}">{{ m.fe(e, 'attendees') }}</div></div>
<label>Facility to be used</label><div class="ck">{% for f in facilities %}<label><input type="checkbox" class="fc" name="fac" value="{{ f }}"><span>{{ f }}</span></label>{% endfor %}</div>
<input name="fac_other" placeholder="Others (specify)" value="{{ v.get('fac_other', '') }}">{{ m.fe(e, 'fac') }}
<label>Equipment</label>{% if not inv %}<p class="mu" style="margin:0 0 6px">The admin has not listed any equipment yet. Choose "Other" and type the equipment you need.</p>{% endif %}<div id="eq"></div><button type="button" class="s" onclick="addEq()">+ Add equipment</button>{{ m.fe(e, 'eq') }}
<label>Requested by (College Department/Department Head) - optional</label><input name="head" value="{{ v.get('head', '') }}">
<p class="mu">Note: The requester is responsible for any damages.<br> Please attach a valid I.D. and contact number when the form is printed.<br> Facilities and equipment that are booked, or borrowed and not yet returned, are marked BOOKED / OUT OF STOCK.<br> Pick the event dates and times to see availability for that period.</p>
<button>Submit Request</button></form>
<script>
const INV={{ inv|tojson }},OLD_ITEMS={{ old_items|tojson }},OLD_FAC={{ old_fac|tojson }},HAS_ERR={{ 'true' if e else 'false' }};
let AV={left:{},booked:[]},READY=false;const $=s=>document.querySelector(s);
function addEq(name,qty,other){const d=document.createElement('div');d.className='erow';
 d.innerHTML='<select name="item_name" onchange="paint()">'+INV.map(n=>'<option value="'+n.replace(/"/g,'&quot;')+'"></option>').join('')+'<option value="__other">Other (type equipment name)</option></select><input name="item_qty" type="number" min="1" value="1" onchange="paint()"><input name="item_other" placeholder="Equipment name" style="flex:2;display:none"><button type="button" class="r" style="margin:0" onclick="this.parentNode.remove()">x</button>';
 $('#eq').appendChild(d);const s=d.querySelector('select');
 if(name!==undefined){s.value=[...s.options].some(o=>o.value===name)?name:'__other';d.querySelector('[name=item_qty]').value=qty;d.querySelector('[name=item_other]').value=other||''}
 else if(!INV.length)s.value='__other';paint()}
async function refresh(){try{AV=await (await fetch('/api/availability?d='+$('#ed').value+'&rd='+$('#rd').value+'&t1='+$('#t1').value+'&t2='+$('#t2').value)).json()}catch(e){}READY=true;paint()}
function paint(){if(!READY)return;
 document.querySelectorAll('.fc').forEach(c=>{const b=AV.booked.includes(c.value.toLowerCase());c.disabled=b;if(b)c.checked=false;c.nextElementSibling.textContent=c.value+(b?' - BOOKED':'')});
 document.querySelectorAll('.erow').forEach(r=>{const s=r.querySelector('select'),q=r.querySelector('[name=item_qty]');
  [...s.options].forEach(o=>{if(o.value==='__other')return;const l=AV.left[o.value];o.disabled=!(l>0);o.textContent=o.value+(l>0?' ('+l+' available)':' - OUT OF STOCK')});
  if(s.selectedOptions[0].disabled){const f=[...s.options].find(o=>!o.disabled);if(f)s.value=f.value}
  const oth=s.value==='__other';r.querySelector('[name=item_other]').style.display=oth?'block':'none';
  if(oth)q.removeAttribute('max');else{const l=AV.left[s.value]||0;q.max=l;if(q.value!==''&&+q.value>l)q.value=Math.max(l,1)}})}
function mkTime(inp){inp.type='hidden';const w=document.createElement('div');w.className='tp';
 const H=document.createElement('select'),M=document.createElement('select'),P=document.createElement('select');
 H.innerHTML='<option value="">Hour</option>'+[...Array(12)].map((_,i)=>'<option>'+(i+1)+'</option>').join('');
 M.innerHTML='<option value="">Min</option>'+[...Array(12)].map((_,i)=>'<option>'+('0'+i*5).slice(-2)+'</option>').join('');
 P.innerHTML='<option>AM</option><option>PM</option>';[H,M,P].forEach(x=>w.appendChild(x));inp.parentNode.insertBefore(w,inp.nextSibling);
 const v=inp.value;if(/^[0-9][0-9]:[0-9][0-9]$/.test(v)){let h=+v.slice(0,2);const m=v.slice(3);P.value=h>=12?'PM':'AM';h=h%12||12;H.value=String(h);
  if(![...M.options].some(o=>o.value===m)){const o=document.createElement('option');o.textContent=m;M.appendChild(o)}M.value=m}
 const sync=()=>{let val='';if(H.value&&M.value!==''){let h=+H.value%12;if(P.value==='PM')h+=12;val=('0'+h).slice(-2)+':'+M.value}
  if(inp.value!==val){inp.value=val;inp.dispatchEvent(new Event('change',{bubbles:true}))}};[H,M,P].forEach(x=>x.onchange=sync)}
document.querySelectorAll('input[type=time]').forEach(mkTime);
$('#ed').addEventListener('change',()=>{const r=$('#rd');r.min=$('#ed').value;if(!r.value||r.value<$('#ed').value)r.value=$('#ed').value});
(async()=>{await refresh();document.querySelectorAll('.fc').forEach(c=>{if(OLD_FAC.includes(c.value)&&!c.disabled)c.checked=true});
 if(HAS_ERR&&OLD_ITEMS.length)OLD_ITEMS.forEach(x=>addEq(x[0],x[1],x[2]));else addEq();
 ['ed','rd','t1','t2'].forEach(i=>$('#'+i).addEventListener('change',refresh))})();
</script>
{% elif tab == 'mine' %}{{ m.table(mine, False, '', 'My Requests') }}
{% else %}{{ m.calendar(weeks, days, title, prev, next, 'cal') }}{% endif %}{% endblock %}"""
T["admin.html"] = """{% extends 'base.html' %}{% block body %}{% import 'macros.html' as m %}
{{ m.side([('__label','Requests',none,''),('pending','Pending',cnt.pending,'pending'),('approved','Approved',cnt.approved,'approved'),('returned','Returned',cnt.returned,'returned'),('disapproved','Disapproved',cnt.disapproved,'disapproved'),('__label','Manage',none,''),('cal','Calendar',none,'cal'),('stock','Equipment Stock',none,'stock'),('users','Users',none,'users'),('report','Monthly Report',none,'report')], tab) }}
{% if tab in ('pending','approved','returned','disapproved') %}{% if tab == 'approved' %}<p class="mu">Approved requests stay here until you click <b>Mark Returned</b>. Unreturned items stay out of stock, even after the event date.</p>{% endif %}{{ m.table(rows, True, tab, {'pending': 'Pending Requests', 'approved': 'Approved - Not Yet Returned', 'returned': 'Returned', 'disapproved': 'Disapproved'}[tab]) }}
{% elif tab == 'cal' %}{{ m.calendar(weeks, days, title, prev, next, 'cal', true, evdata) }}
{% elif tab == 'stock' %}<div class="card"><h2>Equipment Stock</h2><p class="mu">You decide which equipment students can borrow and how many you own. Students cannot borrow more than what is left for their chosen date and time. "Out now" = approved and not yet returned.</p>
{% if inv %}<div class="tb"><table><tr><th>Equipment</th><th>Out now (not returned)</th><th>Available now</th><th>Total stock</th></tr>{% for i in inv %}
<tr><td>{{ i.name }}</td><td>{{ i.out }}</td><td>{{ [i.stock - i.out, 0]|max }}</td><td style="white-space:nowrap"><form class="in" method="post" action="{{ url_for('admin_stock') }}">{{ m.token() }}<input type="hidden" name="name" value="{{ i.name }}"><input name="stock" type="number" min="0" value="{{ i.stock }}" style="max-width:90px;display:inline-block"><button class="sm">Save</button></form>
<form class="in" method="post" action="{{ url_for('admin_stock_delete') }}" onsubmit="return confirm('Remove {{ i.name }} from the equipment list?')">{{ m.token() }}<input type="hidden" name="name" value="{{ i.name }}"><button class="sm r">Delete</button></form></td></tr>{% endfor %}</table></div>{% else %}<p>No equipment yet. Add the first one below.</p>{% endif %}
<form method="post" action="{{ url_for('admin_stock') }}" class="erow" style="margin-top:14px">{{ m.token() }}<input name="name" placeholder="New equipment name" required><input name="stock" type="number" min="0" placeholder="Quantity" required><button style="margin:0">Add</button></form></div>
{% elif tab == 'report' %}<div class="card"><h2>Monthly Report - {{ rtitle }}</h2>
<p class="mu" style="margin:-6px 0 12px">Requests / inquiries received by the GSO. Pick a month to review past inquiries, then download the report.</p>
<div class="rbar"><a class="btn s" href="{{ url_for('dashboard', tab='report', m=rprev, by=by) }}">&lsaquo; Previous</a>
<form method="get" action="{{ url_for('dashboard') }}" class="rf"><input type="hidden" name="tab" value="report"><input type="month" name="m" value="{{ ym }}"><select name="by"><option value="filed" {{ 'selected' if by == 'filed' }}>By date filed</option><option value="event" {{ 'selected' if by == 'event' }}>By event date</option></select><button>Show</button></form>
<a class="btn s" href="{{ url_for('dashboard', tab='report', m=rnext, by=by) }}">Next &rsaquo;</a></div>
{% if months %}<div class="chips"><span class="mu">Past months:</span>{% for mm, label, c in months %}<a class="{{ 'on' if mm == ym }}" href="{{ url_for('dashboard', tab='report', m=mm, by=by) }}">{{ label }} ({{ c }})</a>{% endfor %}</div>{% endif %}
<div class="stats"><div class="stat"><b>{{ rep.st.total }}</b><span>Total requests</span></div><div class="stat"><b>{{ rep.st.approved }}</b><span>Approved</span></div><div class="stat"><b>{{ rep.st.disapproved }}</b><span>Disapproved</span></div><div class="stat"><b>{{ rep.st.pending }}</b><span>Pending</span></div><div class="stat"><b>{{ rep.st.returned }}</b><span>Returned</span></div><div class="stat"><b>{{ rep.st.not_returned }}</b><span>Approved, not yet returned</span></div></div>
<div class="g3"><div><h4>College/Department</h4><table>{% for d in rep.depts %}<tr><td>{{ d[0] }}</td><td style="text-align:right">{{ d[1] }}</td></tr>{% else %}<tr><td class="mu">-</td></tr>{% endfor %}</table></div>
<div><h4>Facility used</h4><table>{% for f, c in rep.fac %}<tr><td>{{ f }}</td><td style="text-align:right">{{ c }}</td></tr>{% else %}<tr><td class="mu">-</td></tr>{% endfor %}</table></div>
<div><h4>Equipment borrowed (qty)</h4><table>{% for f, c in rep.eq %}<tr><td>{{ f }}</td><td style="text-align:right">{{ c }}</td></tr>{% else %}<tr><td class="mu">-</td></tr>{% endfor %}</table></div></div>
<div style="margin:6px 0 14px"><a class="btn" href="{{ url_for('report_pdf', m=ym, by=by) }}">Download PDF</a><a class="btn s" href="{{ url_for('report_csv', m=ym, by=by) }}">Download CSV (Excel)</a></div>
<div class="tb"><table><tr><th>RFU No.</th><th>Date filed</th><th>Requester</th><th>Event</th><th>Schedule</th><th>Resources</th><th>Status</th></tr>
{% for r in rep.rows %}<tr><td>{{ r.rfu_no }}</td><td>{{ r.created_at|dt12 }}</td><td>{{ r.requester }}<br><span class="mu">{{ r.dept }}</span></td><td>{{ r.event }}</td><td>{{ m.when(r) }}</td><td><span class="mu" style="font-size:13px">{{ r.res }}</span></td><td><span class="b {{ r.badge }}">{{ r.badge }}</span></td></tr>
{% else %}<tr><td colspan="7">No requests for this period.</td></tr>{% endfor %}</table></div></div>
{% else %}<div class="card"><h2>Users</h2><div class="tb"><table><tr><th>Name</th><th>Student no.</th><th>Username</th><th>Role</th></tr>{% for u in users %}
<tr><td>{{ u.fullname }}</td><td>{{ u.student_no }}</td><td>{{ u.username }}</td><td>{{ u.role }}</td></tr>{% endfor %}</table></div></div>{% endif %}{% endblock %}"""
app.jinja_loader = DictLoader(T)
app.jinja_env.globals["csrf"] = csrf
app.jinja_env.filters.update(t12=t12, dfmt=dfmt, dt12=dt12)
app.jinja_env.globals["ICONS"] = ICONS

init_db()

# =====================================================================================================================
# ADDED MODULE - ONLINE DAMAGE-REPORT SYSTEM (everything below is new; nothing above this line was changed)
#   Public (no login):  small "Report broken equipment" button on the sign-in page ->
#                       /report      report form        /report/done   confirmation        /report/track   check status
#   GSO admin:          /gso         separate Damage Reports dashboard (same admin account as the facility dashboard)
#   Optional e-mail alerts: set GSO_NOTIFY_EMAIL (+ SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM, APP_URL)
# =====================================================================================================================
import smtplib, threading
from email.message import EmailMessage

RP_STATUSES = ("New", "In Progress", "Resolved", "Rejected")
RP_WHERE = {"new": "status NOT IN ('Resolved','Rejected')", "progress": "status='In Progress'", "resolved": "status='Resolved'",
            "rejected": "status='Rejected'", "all": "1=1"}
RP_TITLES = {"new": "Not yet resolved", "progress": "In progress", "resolved": "Resolved reports", "rejected": "Rejected / closed reports",
             "all": "All reports", "summary": "Summary"}
RP_REJECT = ["Duplicate report", "Spam / fake report", "Not a damage report", "Not under GSO"]
RP_PROBLEMS = ["Broken / damaged", "Not working", "Missing parts", "Leaking / electrical / safety hazard", "Other"]
RP_COMMON = ["Chair", "Table", "Projector", "Microphone", "Sound system", "Aircon", "Electric fan", "Computer", "Light / bulb",
             "Door / lock", "Window", "Faucet / plumbing", "Whiteboard", "Extension cord"]
RP_MAX_PHOTO = 4 * 1024 * 1024
RP_COLS = ("id,ticket,token,reporter,contact,dept,item,location,problem,urgent,description,photo_type,status,created_at,"
           "updated_at,resolved_at,(photo IS NOT NULL) AS has_photo")
RP_SCHEMA = """
CREATE TABLE IF NOT EXISTS damage_reports(id INTEGER PRIMARY KEY, ticket TEXT, token TEXT UNIQUE NOT NULL, reporter TEXT NOT NULL,
  contact TEXT, dept TEXT, item TEXT NOT NULL, location TEXT NOT NULL, problem TEXT, urgent INTEGER DEFAULT 0, description TEXT NOT NULL,
  photo TEXT, photo_type TEXT, status TEXT DEFAULT 'New', created_at TEXT, updated_at TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS damage_updates(id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES damage_reports(id) ON DELETE CASCADE,
  status TEXT, note TEXT, public INTEGER DEFAULT 1, by_name TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS survey_responses(id INTEGER PRIMARY KEY, created_at TEXT, client_type TEXT, service TEXT, s1 INTEGER, s2 INTEGER, s3 INTEGER,
  s4 INTEGER, s5 INTEGER, s6 INTEGER, s7 INTEGER, recommend TEXT, comment TEXT);
CREATE TABLE IF NOT EXISTS survey_csm(id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, created_at TEXT, client_type TEXT, sex TEXT, age INTEGER, region TEXT, service TEXT,
  cc1 INTEGER, cc2 INTEGER, cc3 INTEGER, sqd0 INTEGER, sqd1 INTEGER, sqd2 INTEGER, sqd3 INTEGER, sqd4 INTEGER, sqd5 INTEGER, sqd6 INTEGER, sqd7 INTEGER, sqd8 INTEGER,
  comment TEXT, email TEXT);
"""

def rp_init():
    with app.app_context():
        try:
            if USE_PG:
                cur = db().c.cursor(); cur.execute("SELECT pg_advisory_xact_lock(7242027)")
                cur.execute(RP_SCHEMA.replace("id INTEGER PRIMARY KEY", "id SERIAL PRIMARY KEY")
                            + "".join("ALTER TABLE %s ENABLE ROW LEVEL SECURITY;\n" % t for t in ("damage_reports", "damage_updates", "survey_responses", "survey_csm")))
                db().commit()
            else:
                db().executescript(RP_SCHEMA)
        except Exception as err:
            if USE_PG:
                try: db().c.rollback()
                except Exception: pass
            print("rp_init note (another worker may have done this already):", err)

ICONS.update({
 "alert": '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
 "star": '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
 "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
})

# ---- small helpers -------------------------------------------------------------------------------------------------
RP_HITS = {}
def rp_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")

def rp_limited(bucket, limit, window=600):
    """Soft anti-spam limit per visitor address (the form has no login)."""
    if len(RP_HITS) > 5000: RP_HITS.clear()
    key, now = (bucket, rp_ip()), time.time()
    hits = [t for t in RP_HITS.get(key, []) if now - t < window]
    if len(hits) >= limit:
        RP_HITS[key] = hits; return True
    hits.append(now); RP_HITS[key] = hits; return False

def rp_photo(file):
    """-> (base64 text, mime type, error). Only real JPG / PNG / WebP files are accepted (checked by file signature)."""
    if not file or not file.filename: return None, None, None
    raw = file.read(RP_MAX_PHOTO + 1)
    if not raw: return None, None, None
    if len(raw) > RP_MAX_PHOTO: return None, None, "The photo is too large (maximum 4 MB). Choose a smaller photo."
    if raw[:3] == b"\xff\xd8\xff": mt = "image/jpeg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n": mt = "image/png"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP": mt = "image/webp"
    else: return None, None, "The photo must be a JPG, PNG or WebP image."
    return base64.b64encode(raw).decode(), mt, None

def rp_find(code):
    code = (code or "").strip().upper()
    ticket, _, tok = code.rpartition("-")
    if code.count("-") < 3 or not tok: return None
    return q1("SELECT %s FROM damage_reports WHERE ticket=? AND token=?" % RP_COLS, ticket, tok)

def rp_code(r): return "%s-%s" % (r["ticket"], r["token"])
def rp_counts(): return {k: q1("SELECT COUNT(*) FROM damage_reports WHERE " + w)[0] for k, w in RP_WHERE.items()}

_RP_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
def rp_mail(to, subject, body):
    """Optional e-mail (only when SMTP_HOST is set). Runs in the background and never breaks the page if it fails."""
    host = os.environ.get("SMTP_HOST", "").strip()
    to = [t for t in to if t and _RP_EMAIL.match(t)]
    if not host or not to: return
    def run():
        try:
            m = EmailMessage(); m["Subject"] = re.sub(r"\s+", " ", subject)[:200]; m["To"] = ", ".join(to)
            m["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "gso@localhost"; m.set_content(body)
            port = int(os.environ.get("SMTP_PORT", "587"))
            s = smtplib.SMTP_SSL(host, port, timeout=15) if port == 465 else smtplib.SMTP(host, port, timeout=15)
            with s:
                if port != 465: s.starttls()
                if os.environ.get("SMTP_USER"): s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
                s.send_message(m)
        except Exception as err:
            print("rp_mail failed:", err)
    threading.Thread(target=run, daemon=True).start()

def rp_link(path): return (os.environ.get("APP_URL", "").rstrip("/") + path) if os.environ.get("APP_URL") else ""

def rp_csv(x):
    x = "" if x is None else str(x)
    return "'" + x if len(x) > 1 and x[:1] in ("=", "+", "-", "@") else x      # stops spreadsheet formula injection from public input

# ---- automatic spam / duplicate detection (used when a report is submitted) ---------------------------------------------
def rp_auto_spam(v):
    """Returns a short reason when the new report is obviously spam, else ''. Urgent reports are never auto-rejected."""
    c = " ".join((v["contact"] or "").lower().split())
    now = datetime.strptime(now_s(), "%Y-%m-%d %H:%M"); fmt = "%Y-%m-%d %H:%M"
    n = q1("SELECT COUNT(*) FROM damage_reports WHERE LOWER(contact)=? AND created_at>=?", c, (now - timedelta(hours=1)).strftime(fmt))[0]
    if n >= 3: return "more than 3 reports from the same contact within one hour"
    if q1("SELECT 1 FROM damage_reports WHERE LOWER(contact)=? AND created_at>=? AND LOWER(TRIM(description))=?", c, (now - timedelta(days=1)).strftime(fmt), " ".join((v["description"] or "").lower().split())):
        return "the same description was sent again by the same contact"
    return ""

def rp_open_dup(v):
    """An existing report that is not yet resolved for the same item in the same place (case and extra spaces ignored)."""
    key = (" ".join(v["item"].lower().split()), " ".join(v["location"].lower().split()))
    for x in q("SELECT id,ticket,status,created_at,item,location FROM damage_reports WHERE status NOT IN ('Resolved','Rejected') ORDER BY id DESC LIMIT 400"):
        if (" ".join(x["item"].lower().split()), " ".join(x["location"].lower().split())) == key: return x
    return None

# ---- public: report form -------------------------------------------------------------------------------------------
@app.route("/report", methods=["GET", "POST"])
def rp_form():
    v, e = {"problem": RP_PROBLEMS[0]}, {}
    dup = None; spam = ""
    if request.method == "POST":
        v = {k: request.form.get(k, "").strip() for k in ("reporter", "contact", "dept", "item", "location", "problem", "description", "urgent", "website")}
        if v["website"]: return redirect(url_for("home"))                       # hidden field: only bots fill it in
        for k, label in (("reporter", "Your name"), ("contact", "Contact number or e-mail"), ("item", "Equipment / item"),
                         ("location", "Location (building / room)"), ("description", "Description of the problem")):
            if not v[k]: bad(e, k, label + " is required.")
        if v["description"] and len(v["description"]) < 10: bad(e, "description", "Please describe the problem in a few more words.")
        if v["problem"] not in RP_PROBLEMS: v["problem"] = RP_PROBLEMS[0]
        photo, ptype, perr = rp_photo(request.files.get("photo"))
        if perr: bad(e, "photo", perr)
        if not e and not v["urgent"]: spam = rp_auto_spam(v)
        if e: bad(e, "form", "Please fix the highlighted items. If you attached a photo, please attach it again.")
        elif rp_limited("submit", 15): bad(e, "form", "Too many reports were sent from this network in a short time. Please wait a few minutes and try again.")
        elif not spam and not request.form.get("confirm_dup") and (dup := rp_open_dup(v)):
            pass                                                            # same item + place already reported: ask the visitor first (the form is shown again below)
        else:
            tok = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)); n = now_s()
            ex("""INSERT INTO damage_reports(token,reporter,contact,dept,item,location,problem,urgent,description,photo,photo_type,status,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,'New',?,?)""", tok, v["reporter"][:120], v["contact"][:120], v["dept"][:120], v["item"][:80],
               v["location"][:120], v["problem"], 1 if v["urgent"] else 0, v["description"][:1500], photo, ptype, n, n)
            rid = q1("SELECT id FROM damage_reports WHERE token=?", tok)["id"]
            ex("UPDATE damage_reports SET ticket=? WHERE id=?", "DR-%d-%04d" % (today_ph().year, rid), rid)
            ex("INSERT INTO damage_updates(report_id,status,note,public,by_name,created_at) VALUES(?,?,?,?,?,?)", rid, "New", "Report received by the GSO system.", 1, "System", n)
            rep = q1("SELECT %s FROM damage_reports WHERE id=?" % RP_COLS, rid)
            if spam:                                                         # obvious spam: keep it for the GSO to review (Rejected tab) but do not alert anyone
                ex("UPDATE damage_reports SET status='Rejected', updated_at=? WHERE id=?", n, rid)
                ex("INSERT INTO damage_updates(report_id,status,note,public,by_name,created_at) VALUES(?,?,?,?,?,?)", rid, "Rejected", "Closed: Spam / fake report (detected automatically: %s)" % spam, 1, "System (auto)", n)
                rep = q1("SELECT %s FROM damage_reports WHERE id=?" % RP_COLS, rid)
            rp_mail([] if spam else [x.strip() for x in os.environ.get("GSO_NOTIFY_EMAIL", "").split(",")],
                    "%sNew damage report %s - %s" % ("[URGENT] " if rep["urgent"] else "", rep["ticket"], rep["item"]),
                    "A new damage report was submitted.\n\nTicket: %s\nItem: %s\nLocation: %s\nProblem: %s\nUrgent: %s\nReported by: %s (%s)\n\n%s\n\n%s"
                    % (rep["ticket"], rep["item"], rep["location"], rep["problem"], "YES" if rep["urgent"] else "no", rep["reporter"], rep["contact"],
                       rep["description"], rp_link("/gso/report/%d" % rid)))
            return redirect(url_for("rp_done", c=rp_code(rep)))
    items = sorted({r["name"] for r in q("SELECT name FROM inventory")} | set(RP_COMMON), key=str.lower)
    return render_template("rp_form.html", v=v, e=e, dup=dup, items=items, problems=RP_PROBLEMS, page_sub="General Services Office - Report Broken Equipment")

@app.route("/report/same", methods=["POST"])
def rp_same():
    try: rid = int(request.form.get("rid", "0"))
    except ValueError: rid = 0
    r = q1("SELECT id,status FROM damage_reports WHERE id=?", rid)
    if r and r["status"] not in ("Resolved", "Rejected") and not rp_limited("same", 10):
        ex("INSERT INTO damage_updates(report_id,status,note,public,by_name,created_at) VALUES(?,?,?,?,?,?)", r["id"], r["status"],
           "Another person tried to report the same problem (also affected).", 0, "System", now_s())
    flash("Thank you! The GSO already has a report for this item, so no new report was needed. They have been told that more people are affected.", "ok")
    return redirect(url_for("home"))

@app.route("/report/done")
def rp_done():
    rep = rp_find(request.args.get("c", ""))
    if not rep: return redirect(url_for("rp_form"))
    return render_template("rp_done.html", rep=rep, code=rp_code(rep), page_sub="General Services Office - Report Broken Equipment")

@app.route("/report/track")
def rp_track():
    code, rep, ups, err = request.args.get("code", "").strip(), None, [], ""
    if code:
        if rp_limited("track", 30): err = "Too many attempts. Please wait a few minutes and try again."
        else:
            rep = rp_find(code)
            if not rep: err = "No report found with that tracking code. Please check the code and try again."
            else: ups = q("SELECT * FROM damage_updates WHERE report_id=? AND public=1 ORDER BY id DESC", rep["id"])
    return render_template("rp_track.html", code=code, rep=rep, ups=ups, err=err, page_sub="General Services Office - Report Broken Equipment")

# ---- GSO admin: separate Damage Reports dashboard ------------------------------------------------------------------
def rp_summary():
    ds = []
    for r in q("SELECT created_at, resolved_at FROM damage_reports WHERE status='Resolved' AND resolved_at IS NOT NULL"):
        try: ds.append((datetime.strptime(r["resolved_at"], "%Y-%m-%d %H:%M") - datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M")).total_seconds() / 86400)
        except Exception: pass
    top = lambda col: [(r[1], r[2]) for r in q("SELECT LOWER(%s) k, MIN(%s) n, COUNT(*) c FROM damage_reports WHERE status!='Rejected' GROUP BY LOWER(%s) ORDER BY c DESC LIMIT 8" % (col, col, col))]
    return dict(urgent_open=q1("SELECT COUNT(*) FROM damage_reports WHERE urgent=1 AND status NOT IN ('Resolved','Rejected')")[0],
                avg_days=round(sum(ds) / len(ds), 1) if ds else None, top_items=top("item"), top_locs=top("location"),
                months=[(r[0], r[1]) for r in q("SELECT SUBSTR(created_at,1,7) m, COUNT(*) c FROM damage_reports GROUP BY SUBSTR(created_at,1,7) ORDER BY m DESC LIMIT 6")])

def rp_norm(x): return " ".join((x or "").lower().split())

def rp_similar(r, earlier_only=False):
    """Other reports for the same item in the same place (not rejected). Helps the GSO spot repeated reports."""
    out = []
    for x in q("SELECT id,ticket,status,created_at,item,location FROM damage_reports WHERE id!=? AND status!='Rejected' ORDER BY id DESC LIMIT 400", r["id"]):
        if earlier_only and x["id"] > r["id"]: continue
        if rp_norm(x["item"]) == rp_norm(r["item"]) and rp_norm(x["location"]) == rp_norm(r["location"]): out.append(x)
    return out

def rp_flags(r):
    """Warnings shown to the GSO: possible duplicate, same description, many reports from the same contact in 24h."""
    fl = []
    sim = rp_similar(r)
    if sim: fl.append(("dup", "Possible duplicate: %d other report(s) for the same item and location." % len(sim)))
    since = (datetime.strptime(now_s(), "%Y-%m-%d %H:%M") - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    n = q1("SELECT COUNT(*) FROM damage_reports WHERE id!=? AND LOWER(contact)=? AND created_at>=?", r["id"], rp_norm(r["contact"]), since)[0]
    if n >= 2: fl.append(("spam", "The same contact filed %d other reports in the last 24 hours (possible spam)." % n))
    d = q1("SELECT ticket FROM damage_reports WHERE id!=? AND LOWER(TRIM(description))=? LIMIT 1", r["id"], rp_norm(r["description"]))
    if d: fl.append(("spam", "Same description as report %s." % d["ticket"]))
    if len((r["description"] or "").strip()) < 15: fl.append(("spam", "Very short description."))
    return sim, fl

@app.route("/gso")
@admin_required
def gso_dash():
    tab = request.args.get("tab", "new")
    if tab != "summary" and tab not in RP_WHERE: tab = "new"
    ctx = dict(tab=tab, cnt=rp_counts(), title=RP_TITLES[tab], qs="", page_sub="General Services Office - Damage Reports")
    if tab == "summary": ctx.update(rp_summary())
    else:
        qs = request.args.get("qs", "").strip()[:60]; sql = "SELECT %s FROM damage_reports WHERE %s" % (RP_COLS, RP_WHERE[tab]); args = []
        if qs:
            sql += " AND (LOWER(ticket) LIKE ? OR LOWER(item) LIKE ? OR LOWER(location) LIKE ? OR LOWER(reporter) LIKE ? OR LOWER(description) LIKE ?)"
            args = ["%" + qs.lower() + "%"] * 5
        sql += " ORDER BY urgent DESC, id DESC" if tab == "new" else " ORDER BY id DESC"
        rows = q(sql, *args)
        ctx.update(rows=rows, qs=qs, dups={x["id"]: 1 for x in rows if x["status"] != "Rejected" and rp_similar(x, True)})
    return render_template("gso_dash.html", **ctx)

@app.route("/gso/report/<int:rid>")
@admin_required
def gso_view(rid):
    r = q1("SELECT %s FROM damage_reports WHERE id=?" % RP_COLS, rid)
    if not r: abort(404)
    sim, flags = rp_flags(r) if r["status"] not in ("Resolved", "Rejected") else ([], [])
    if r["status"] not in ("Resolved", "Rejected"):
        more = q1("SELECT COUNT(*) FROM damage_updates WHERE report_id=? AND note LIKE 'Another person tried%'", rid)[0]
        if more: flags = [("info", "%d other person(s) tried to report this same problem, so more people are affected." % more)] + list(flags)
    return render_template("gso_view.html", r=r, ups=q("SELECT * FROM damage_updates WHERE report_id=? ORDER BY id DESC", rid), statuses=RP_STATUSES,
                           sim=sim, flags=flags, reasons=RP_REJECT, cnt=rp_counts(), tab="", page_sub="General Services Office - Damage Reports")

@app.route("/gso/report/<int:rid>/update", methods=["POST"])
@admin_required
def gso_update(rid):
    r = q1("SELECT %s FROM damage_reports WHERE id=?" % RP_COLS, rid)
    if not r: abort(404)
    st, note = request.form.get("status", r["status"]), request.form.get("note", "").strip()[:600]
    public = 1 if request.form.get("public") else 0
    if st not in RP_STATUSES: abort(400)
    if st == r["status"] and not note: flash("Nothing to update. Change the status or write a note.", "err"); return redirect(url_for("gso_view", rid=rid))
    if st == "Rejected" and not note: flash("Please write a reason in the note when rejecting a report.", "err"); return redirect(url_for("gso_view", rid=rid))
    n = now_s(); done = (r["resolved_at"] if r["status"] == "Resolved" and r["resolved_at"] else n) if st == "Resolved" else None
    ex("UPDATE damage_reports SET status=?, updated_at=?, resolved_at=? WHERE id=?", st, n, done, rid)
    ex("INSERT INTO damage_updates(report_id,status,note,public,by_name,created_at) VALUES(?,?,?,?,?,?)", rid, st, note, public, g.user["fullname"], n)
    if public:
        rp_mail([r["contact"]], "Update on your damage report %s" % r["ticket"],
                "Hello %s,\n\nYour report %s (%s) is now: %s\n%s\n\nTrack it any time with this code: %s\n%s\n\n- General Services Office"
                % (r["reporter"], r["ticket"], r["item"], st, ("Note from GSO: " + note) if note else "", rp_code(r), rp_link("/report/track?code=" + rp_code(r))))
    flash("Report %s updated." % r["ticket"], "ok")
    return redirect(url_for("gso_view", rid=rid))

@app.route("/gso/report/<int:rid>/resolve", methods=["POST"])
@admin_required
def gso_resolve(rid):
    r = q1("SELECT %s FROM damage_reports WHERE id=?" % RP_COLS, rid)
    if not r: abort(404)
    if r["status"] != "Resolved":
        n = now_s()
        ex("UPDATE damage_reports SET status='Resolved', updated_at=?, resolved_at=? WHERE id=?", n, n, rid)
        ex("INSERT INTO damage_updates(report_id,status,note,public,by_name,created_at) VALUES(?,?,?,?,?,?)", rid, "Resolved", "Marked as resolved", 1, g.user["fullname"], n)
    flash("Report %s was marked as resolved." % r["ticket"], "ok")
    return redirect(url_for("gso_view", rid=rid))

@app.route("/gso/report/<int:rid>/reject", methods=["POST"])
@admin_required
def gso_reject(rid):
    r = q1("SELECT %s FROM damage_reports WHERE id=?" % RP_COLS, rid)
    if not r: abort(404)
    why = request.form.get("reason", ""); why = why if why in RP_REJECT else RP_REJECT[0]
    extra = request.form.get("note", "").strip()[:300]
    if r["status"] != "Rejected":
        n = now_s()
        ex("UPDATE damage_reports SET status='Rejected', updated_at=?, resolved_at=NULL WHERE id=?", n, rid)
        ex("INSERT INTO damage_updates(report_id,status,note,public,by_name,created_at) VALUES(?,?,?,?,?,?)", rid, "Rejected", "Closed: " + why + ((" - " + extra) if extra else ""), 1, g.user["fullname"], n)
    flash("Report %s was rejected (%s)." % (r["ticket"], why), "ok")
    return redirect(url_for("gso_dash", tab="new"))

@app.route("/gso/report/<int:rid>/reopen", methods=["POST"])
@admin_required
def gso_reopen(rid):
    r = q1("SELECT %s FROM damage_reports WHERE id=?" % RP_COLS, rid)
    if not r: abort(404)
    if r["status"] in ("Resolved", "Rejected"):
        n = now_s()
        ex("UPDATE damage_reports SET status='New', updated_at=?, resolved_at=NULL WHERE id=?", n, rid)
        ex("INSERT INTO damage_updates(report_id,status,note,public,by_name,created_at) VALUES(?,?,?,?,?,?)", rid, "New", "Reopened", 1, g.user["fullname"], n)
    flash("Report %s was reopened." % r["ticket"], "ok")
    return redirect(url_for("gso_view", rid=rid))

@app.route("/gso/report/<int:rid>/delete", methods=["POST"])
@admin_required
def gso_delete(rid):
    r = q1("SELECT ticket FROM damage_reports WHERE id=?", rid)
    if not r: abort(404)
    ex("DELETE FROM damage_updates WHERE report_id=?", rid); ex("DELETE FROM damage_reports WHERE id=?", rid)
    flash("Report %s was deleted." % r["ticket"], "ok")
    return redirect(url_for("gso_dash", tab="all"))

@app.route("/gso/photo/<int:rid>")
@admin_required
def gso_photo(rid):
    r = q1("SELECT photo, photo_type FROM damage_reports WHERE id=?", rid)
    if not r or not r["photo"] or r["photo_type"] not in ("image/jpeg", "image/png", "image/webp"): abort(404)
    return Response(base64.b64decode(r["photo"]), mimetype=r["photo_type"], headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})

@app.route("/gso/api/count")
@admin_required
def gso_count():
    return jsonify(new=q1("SELECT COUNT(*) FROM damage_reports WHERE status NOT IN ('Resolved','Rejected')")[0])

@app.route("/gso/export.csv")
@admin_required
def gso_export():
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["Ticket", "Date filed", "Status", "Urgent", "Item", "Problem", "Location", "Description", "Reporter", "Contact", "College/Office", "Resolved on"])
    for r in q("SELECT %s FROM damage_reports ORDER BY id" % RP_COLS):
        w.writerow([rp_csv(x) for x in (r["ticket"], r["created_at"], r["status"], "Yes" if r["urgent"] else "", r["item"], r["problem"], r["location"],
                                        r["description"], r["reporter"], r["contact"], r["dept"], r["resolved_at"])])
    return Response("﻿" + out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=damage-reports.csv"})

# Compress pages (HTML/CSV/JSON) to make them lighter on phones. Registered BEFORE the other after_request helpers so it runs LAST.
import gzip
@app.after_request
def rp_gzip(resp):
    try:
        if (resp.status_code == 200 and not resp.direct_passthrough and "gzip" in request.headers.get("Accept-Encoding", "")
                and "Content-Encoding" not in resp.headers and resp.mimetype in ("text/html", "text/css", "text/csv", "application/json")):
            data = resp.get_data()
            if len(data) > 1024:
                resp.set_data(gzip.compress(data, 6)); resp.headers["Content-Encoding"] = "gzip"; resp.headers.add("Vary", "Accept-Encoding")
    except Exception:
        pass
    return resp

# "Report broken equipment?" prompt + "Click here" button, placed under the sign-in box (login page code is not edited).
@app.after_request
def rp_login_button(resp):
    try:
        if request.endpoint == "login" and resp.status_code == 200 and resp.mimetype == "text/html":
            btn = ('<div class="rp-lr"><div class="rp-lq">Report broken equipment?</div><a class="rp-lb" href="%s">Click here</a></div>' % url_for("rp_form"))
            resp.set_data(resp.get_data(as_text=True).replace("</form>", btn + "</form>", 1))
    except Exception:
        pass
    return resp

# Small "Damage Reports" shortcut shown ONLY to the admin on the facility dashboard (so new reports are noticed). The old page code is not edited.
@app.after_request
def rp_switch_pill(resp):
    try:
        if request.endpoint == "dashboard" and g.get("user") and g.user["role"] == "admin" and resp.status_code == 200 and resp.mimetype == "text/html":
            n = q1("SELECT COUNT(*) FROM damage_reports WHERE status NOT IN ('Resolved','Rejected')")[0]
            pill = ('<a href="%s" style="position:fixed;right:14px;bottom:18px;z-index:30;background:#b45309;color:#fff;padding:10px 16px;border-radius:24px;'
                    'font:600 14px system-ui,sans-serif;text-decoration:none;box-shadow:0 6px 18px rgba(0,0,0,.3)">Damage Reports%s &rarr;</a>'
                    % (url_for("gso_dash"), (' <span style="background:#fff;color:#b45309;border-radius:10px;padding:1px 8px;margin-left:4px">%d new</span>' % n) if n else ""))
            resp.set_data(resp.get_data(as_text=True).replace("</body>", pill + "</body>", 1))
    except Exception:
        pass
    return resp

# ---- templates + styling for the new pages --------------------------------------------------------------------------
_b = T["base.html"]          # lets the new pages set their own browser-tab title / header subtitle / background; old pages render exactly as before
_b = _b.replace("<title>Facility &amp; Equipment Request - SLSU</title>", "<title>{{ page_title|default('Facility &amp; Equipment Request - SLSU')|safe }}</title>")
_b = _b.replace("<small>General Services Office - Facility &amp; Equipment Request</small>", "<small>{{ page_sub|default('General Services Office - Facility &amp; Equipment Request')|safe }}</small>")
_b = _b.replace("'authbg' if request.endpoint in ('login',", "'authbg' if request.endpoint in ('rp_form', 'rp_done', 'rp_track', 'login',")
_b = _b.replace("'dash' if request.endpoint in (", "'dash' if (request.endpoint or '').startswith(('gso_', 'sched_')) or request.endpoint in (")
# Fixed background that never moves while scrolling (the old "background-attachment: fixed" is ignored by many phones/browsers,
# so the picture is drawn on a fixed layer behind the page instead). Applies to the login pages and all dashboards.
_b = _b.replace("</style></head>", """body.authbg,body.dash{background:var(--bg)!important}
body.authbg::before,body.dash::before{content:"";position:fixed;top:0;left:0;width:100%;height:100%;z-index:-1;pointer-events:none;background:linear-gradient(rgba(255,255,255,.10),rgba(255,255,255,.10)),url('/bg.jpg') center/cover no-repeat}
body.dash::before{background:url('/bg.jpg') center/cover no-repeat}
</style></head>""", 1)
T["base.html"] = _b

RP_CSS = """
.ic{width:1.05em;height:1.05em;stroke:currentColor;fill:none;stroke-width:3;stroke-linecap:round;stroke-linejoin:round;vertical-align:-.14em}.ic.ok{color:#1e8449}
.svo i svg{width:28px;height:28px;stroke:currentColor;fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;display:block}.svo input:checked+span i{color:#fff!important}
.svq{margin:14px 0 6px}.svq p{margin:0 0 6px;font-weight:600}.svr{display:grid;grid-template-columns:repeat(5,1fr);gap:6px}.svr.c3{grid-template-columns:repeat(3,1fr)}
.svo{display:block!important;margin:0!important;cursor:pointer;position:relative;font-weight:400!important}.svo input{position:absolute;opacity:0;width:0;height:0}
.svo span{display:flex;flex-direction:column;align-items:center;gap:1px;padding:8px 2px;border:1px solid var(--bd);border-radius:10px;background:var(--bg);text-align:center;transition:background .15s,border-color .15s}
.svo i{font-style:normal;font-size:22px;line-height:1}.svo b{font-size:13px}.svo small{font-size:10px;color:var(--mu);line-height:1.1;min-height:22px}
.svo input:checked+span{background:#0e7490;border-color:#0e7490;color:#fff}.svo input:checked+span small{color:#e0f7fa}.svo input:focus-visible+span{outline:2px solid #22d3ee}.svo span:hover{border-color:#22d3ee}
.svleg{font-size:12.5px;color:var(--mu);margin:0 0 4px}.svbar{height:10px;background:var(--bd);border-radius:6px;overflow:hidden;min-width:90px}.svbar i{display:block;height:100%;background:#0e7490}
@media(max-width:420px){.svo small{display:none}.svo span{padding:8px 0}}
.rp-dup{background:#b45309;color:#fff;border-radius:12px;padding:1px 8px;font-size:11px;font-weight:700;margin-left:4px;white-space:nowrap}
.rp-warn{background:#fff4e0;border:1px solid #e0a526;color:#5b3a00;border-radius:10px;padding:12px 14px;margin-bottom:18px;font-size:14px}.rp-warn ul{margin:6px 0;padding-left:20px}.rp-warn a{color:#8a4b00;font-weight:700}
html[data-theme=dark] .rp-warn{background:#3a2a0e;border-color:#8a6a1e;color:#f3d9a4}html[data-theme=dark] .rp-warn a{color:#ffd37a}
.rp-ok{border-radius:10px;padding:10px 12px;margin:12px 0 0;font-size:14px}.rp-ok.g{background:#d5f0de;color:#1e6b3a}.rp-ok.x{background:#e6e8ec;color:#4a5160}
html[data-theme=dark] .rp-ok.g{background:#173d27;color:#8ee0ac}html[data-theme=dark] .rp-ok.x{background:#2a323c;color:#c4cdd8}
.rp-choices{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}@media(max-width:700px){.rp-choices{grid-template-columns:1fr}}
.rp-choice{display:flex;flex-direction:column;gap:10px;padding:28px 24px;text-decoration:none;color:var(--tx);border-radius:16px;background:rgba(255,255,255,.95);border:1px solid var(--bd);box-shadow:0 4px 14px rgba(0,0,0,.08);transition:transform .2s ease,box-shadow .2s ease}
.rp-choice:hover{transform:translateY(-4px);box-shadow:0 14px 30px rgba(20,83,45,.22);opacity:1}
.rp-ic{width:54px;height:54px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(135deg,#2d6a4f,#14532d);color:#fff}.rp-ic.warn{background:linear-gradient(135deg,#e0a526,#b45309)}
.rp-ic svg{width:28px;height:28px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.rp-choice b{font:600 20px Georgia,serif;color:var(--pr)}.rp-choice .d{color:var(--mu);font-size:14px}.rp-go{margin-top:auto;font-weight:700;color:var(--pr);padding-top:6px}
.rp-narrow{max-width:640px;margin-left:auto;margin-right:auto}
textarea{width:100%;padding:9px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--bg);color:var(--tx);font:inherit;min-height:96px;resize:vertical}
.auth textarea{padding:12px 13px;font-size:16px}textarea:focus{outline:none;border-color:var(--pr);box-shadow:0 0 0 3px rgba(20,65,123,.22)}
.rp-urg{background:#a63232;color:#fff;border-radius:12px;padding:1px 8px;font-size:11px;font-weight:700;margin-left:4px;white-space:nowrap}
.b.New{background:#fde8c8;color:#8a4b00}.b.InProgress{background:#dbe7fb;color:#14417b}.b.Resolved{background:#d5f0de;color:#1e6b3a}.b.Rejected{background:#e6e8ec;color:#4a5160}
.rp-code{font:700 26px/1.2 ui-monospace,Consolas,monospace;letter-spacing:1px;background:#eef6f0;border:2px dashed #2d6a4f;border-radius:10px;padding:14px;text-align:center;margin:14px 0;word-break:break-all;user-select:all}
.rp-dl{display:grid;grid-template-columns:130px 1fr;gap:8px 14px;margin:0}.rp-dl dt{color:var(--mu);font-size:13px}.rp-dl dd{margin:0;word-break:break-word}
@media(max-width:560px){.rp-dl{grid-template-columns:1fr;gap:1px}.rp-dl dt{margin-top:8px}}
.rp-2{display:grid;grid-template-columns:1.2fr 1fr;gap:18px;align-items:start}@media(max-width:820px){.rp-2{grid-template-columns:1fr}}
.rp-tl{list-style:none;margin:0;padding:0}.rp-tl li{border-left:3px solid #95d5b2;padding:2px 0 12px 12px;margin-left:4px}.rp-tl small{color:var(--mu);display:block}
.rp-photo{max-width:100%;max-height:340px;border-radius:8px;border:1px solid var(--bd);display:block;margin-top:6px}
.rp-bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}.rp-bar input{flex:1;min-width:180px;width:auto}.rp-bar button,.rp-bar .btn{margin:0}
"""
app.jinja_env.globals["RPCSS"] = RP_CSS

T["rp_base.html"] = """{% extends 'base.html' %}{% block body %}<style>{{ RPCSS|safe }}</style>{% block rp %}{% endblock %}{% endblock %}"""

T["rp_macros.html"] = """
{% macro ico(n) %}<svg viewBox="0 0 24 24" aria-hidden="true">{{ ICONS[n]|safe }}</svg>{% endmacro %}
{% macro gside(tab, cnt) %}<button type="button" class="menubtn" onclick="menu(true)" aria-label="Open menu">{{ ico('menu') }}</button><div id="sideBg" class="sideBg" onclick="menu(false)"></div>
<nav id="side" class="side"><div class="sideuser"><div class="av">{{ (g.user.fullname or '?')[:1]|upper }}</div><div class="who2"><b>{{ g.user.fullname }}</b><span>Damage Reports</span></div><button type="button" class="x" onclick="menu(false)" aria-label="Close menu">{{ ico('x') }}</button></div>
<div class="navlabel" style="--i:1">Damage reports</div>
{% for k, label, n, ic in [('new','Not yet resolved',cnt['new'],'bell'),('resolved','Resolved',cnt['resolved'],'approved'),('rejected','Rejected / spam',cnt['rejected'],'disapproved'),('all','All reports',cnt['all'],'list'),('summary','Summary',none,'report'),('survey','Client survey',none,'star'),('backup','Backup & export',none,'stock')] %}
<a class="nav {{ 'on' if tab == k }}" style="--i:{{ loop.index + 1 }}" href="{{ url_for('gso_backup') if k == 'backup' else url_for('gso_survey') if k == 'survey' else url_for('gso_dash', tab=k) }}">{{ ico(ic) }}<span>{{ label }}</span>{% if n is not none %}<span class="cnt" {% if k == 'new' %}id="gsoNew"{% endif %}>{{ n }}</span>{% endif %}</a>{% endfor %}
<div class="navlabel" style="--i:8">Other system</div>
<a class="nav" style="--i:9" href="{{ url_for('dashboard') }}">{{ ico('stock') }}<span>Facility &amp; Equipment</span></a>
<div class="foot"><a class="nav" href="{{ url_for('account') }}">{{ ico('lock') }}<span>Change password</span></a><form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="_csrf" value="{{ csrf() }}"><button class="signout">{{ ico('out') }}<span>Sign out</span></button></form></div></nav>
<script>(function(){var last={{ cnt['new'] }};setInterval(function(){fetch('/gso/api/count',{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
 var el=document.getElementById('gsoNew');if(el)el.textContent=d['new'];
 if(d['new']>last&&!document.getElementById('gsoAlert')){var a=document.createElement('div');a.id='gsoAlert';a.className='msg ok';a.innerHTML='A new damage report was received. <a href="/gso?tab=new">View new reports</a>';var w=document.querySelector('.wrap');w.insertBefore(a,w.firstChild)}
 last=d['new']}).catch(function(){})},45000)})()</script>{% endmacro %}
{% macro badge(s) %}<span class="b {{ s|replace(' ', '') }}">{{ s }}</span>{% endmacro %}
"""

T["rp_form.html"] = """{% extends 'rp_base.html' %}{% block rp %}{% import 'macros.html' as m %}
<form class="card auth rp-narrow" method="post" enctype="multipart/form-data" onsubmit="var b=this.querySelector('button.sub');setTimeout(function(){b.disabled=true},0)">{{ m.token() }}
<h2 style="font-size:22px">Report Broken / Damaged Equipment</h2>
{% if dup %}<div class="rp-warn"><b>This may already be reported</b><p style="margin:6px 0">The GSO already has a report for <b>{{ v.get('item') }}</b> at <b>{{ v.get('location') }}</b>: <b>{{ dup.ticket }}</b> ({{ dup.status }}, filed {{ dup.created_at|dt12 }}).</p>
<p style="margin:6px 0">If it is the same problem, you do not need to report again. If yours is a different problem, press <b>Submit report</b> again (re-attach your photo if you had one).</p><button type="submit" form="sameForm" class="s">It is the same problem</button></div><input type="hidden" name="confirm_dup" value="1">{% endif %}
<p style="margin:0 0 8px;font-size:14px">Already reported? <a href="{{ url_for('rp_track') }}">Check my report</a> with your tracking code.</p>
<p class="mu" style="font-size:14px">Fields marked * are required. The GSO will receive your report right away.</p>
{% if e.get('form') %}<div class="msg err">{{ e['form'][0] }}</div>{% endif %}
<div style="position:absolute;left:-9999px" aria-hidden="true"><label>Website<input name="website" tabindex="-1" autocomplete="off"></label></div>
<label>Equipment / item *</label><input name="item" list="rp-items" maxlength="80" value="{{ v.get('item','') }}" placeholder="e.g. Projector, Chair, Aircon" required><datalist id="rp-items">{% for i in items %}<option value="{{ i }}">{% endfor %}</datalist>{{ m.fe(e,'item') }}
<label>Location (building / room) *</label><input name="location" maxlength="120" value="{{ v.get('location','') }}" placeholder="e.g. Admin Building, Room 203" required>{{ m.fe(e,'location') }}
<label>What is wrong? *</label><select name="problem">{% for p in problems %}<option {{ 'selected' if v.get('problem') == p }}>{{ p }}</option>{% endfor %}</select>
<label>Describe the problem *</label><textarea name="description" maxlength="1500" placeholder="What happened? Since when? Is it still usable?" required>{{ v.get('description','') }}</textarea>{{ m.fe(e,'description') }}
<div class="ck" style="margin-top:10px"><label><input type="checkbox" name="urgent" value="1" {{ 'checked' if v.get('urgent') }}> This is urgent / a safety hazard (exposed wires, leaking, could hurt someone)</label></div>
<label>Photo (optional)</label><input type="file" name="photo" id="rpPhoto" accept="image/*">{{ m.fe(e,'photo') }}<img id="rpPrev" class="rp-photo" alt="" style="display:none">
<label>Your name *</label><input name="reporter" maxlength="120" value="{{ v.get('reporter','') }}" required>{{ m.fe(e,'reporter') }}
<label>Contact number or e-mail *</label><input name="contact" maxlength="120" value="{{ v.get('contact','') }}" placeholder="So the GSO can reach you" required>{{ m.fe(e,'contact') }}
<label>College / Office (optional)</label><input name="dept" maxlength="120" value="{{ v.get('dept','') }}">
<button class="sub">Submit report</button><a class="btn s" href="{{ url_for('home') }}">Cancel</a></form>
<script>(function(){var f=document.getElementById('rpPhoto'),pv=document.getElementById('rpPrev');if(!f)return;
f.addEventListener('change',function(){var file=f.files[0];pv.style.display='none';if(!file||!/^image\\/(jpeg|png|webp)$/.test(file.type))return;
 var u=URL.createObjectURL(file),img=new Image();img.onload=function(){pv.src=u;pv.style.display='block';
  if(!window.DataTransfer)return;var s=Math.min(1,1280/Math.max(img.width,img.height)),c=document.createElement('canvas');c.width=Math.round(img.width*s);c.height=Math.round(img.height*s);
  c.getContext('2d').drawImage(img,0,0,c.width,c.height);c.toBlob(function(b){if(!b)return;try{var dt=new DataTransfer();dt.items.add(new File([b],'photo.jpg',{type:'image/jpeg'}));f.files=dt.files}catch(x){}},'image/jpeg',.82)};img.src=u})})()</script>{% endblock %}"""

T["rp_done.html"] = """{% extends 'rp_base.html' %}{% block rp %}
<div class="card rp-narrow" style="text-align:center"><h2 style="font-size:24px">Report submitted <svg class="ic ok" viewBox="0 0 24 24" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg></h2>
<p>Thank you! The General Services Office has received your report for <b>{{ rep.item }}</b>.</p>
<p class="mu" style="font-size:14px;margin-bottom:0">Save this tracking code. You will need it to check the status of your report later, even after a few hours or days:</p><div class="rp-code">{{ code }}</div>
<button type="button" id="rpCopy">Copy code</button><a class="btn s" href="{{ url_for('rp_track', code=code) }}">Check status</a><a class="btn s" href="{{ url_for('home') }}">Back to sign in</a></div>
<script>(function(){var b=document.getElementById('rpCopy'),c={{ code|tojson }};
function ok(){b.textContent='Copied!';setTimeout(function(){b.textContent='Copy code'},2000)}
function fb(){var t=document.createElement('textarea');t.value=c;t.style.position='fixed';t.style.opacity='0';document.body.appendChild(t);t.select();try{document.execCommand('copy');ok()}catch(e){prompt('Copy this code:',c)}t.remove()}
b.onclick=function(){if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(c).then(ok,fb);else fb()}})();</script>{% endblock %}"""

T["rp_track.html"] = """{% extends 'rp_base.html' %}{% block rp %}{% import 'rp_macros.html' as rm %}
<div class="card rp-narrow"><h2>Check my report</h2><form method="get" class="rp-bar"><input name="code" value="{{ code }}" placeholder="Tracking code, e.g. DR-2026-0007-K4X9Q2" required><button>Check</button></form>
{% if err %}<div class="msg err" style="margin:0">{{ err }}</div>{% endif %}
{% if rep %}<hr style="border:0;border-top:1px solid var(--bd);margin:14px 0"><p style="margin:0 0 6px"><b>{{ rep.ticket }}</b> &middot; {{ rm.badge(rep.status) }}{% if rep.urgent %}<span class="rp-urg">URGENT</span>{% endif %}</p>
<dl class="rp-dl"><dt>Item</dt><dd>{{ rep.item }}</dd><dt>Location</dt><dd>{{ rep.location }}</dd><dt>Problem</dt><dd>{{ rep.problem }}</dd><dt>Date filed</dt><dd>{{ rep.created_at|dt12 }}</dd><dt>Last update</dt><dd>{{ rep.updated_at|dt12 }}</dd></dl>
{% if rep.status == 'Resolved' %}<div class="rp-ok g"><b>Good news - this item has been fixed.</b></div>{% elif rep.status == 'Rejected' %}<div class="rp-ok x"><b>This report was closed by the GSO.</b> See the reason below.</div>{% else %}<div class="rp-ok x">The GSO has received your report. It has not been resolved yet. Check again later with the same code.</div>{% endif %}
<h4 style="margin:18px 0 8px">Progress</h4><ul class="rp-tl">{% for u in ups %}<li>{{ rm.badge(u.status) }} {% if u.note %}{{ u.note }}{% endif %}<small>{{ u.created_at|dt12 }}</small></li>{% endfor %}</ul>{% endif %}
<a class="btn s" href="{{ url_for('home') }}">Back to sign in</a></div>{% endblock %}"""

T["gso_dash.html"] = """{% extends 'rp_base.html' %}{% block rp %}{% import 'rp_macros.html' as rm %}{{ rm.gside(tab, cnt) }}
{% if tab == 'summary' %}<div class="card"><h2>Damage Reports - Summary</h2>
<div class="stats"><div class="stat"><b>{{ cnt['all'] }}</b><span>Total reports</span></div><div class="stat"><b>{{ cnt['new'] }}</b><span>Not yet resolved</span></div><div class="stat"><b>{{ cnt['resolved'] }}</b><span>Resolved</span></div><div class="stat"><b>{{ urgent_open }}</b><span>Urgent &amp; still open</span></div><div class="stat"><b>{{ avg_days if avg_days is not none else '-' }}</b><span>Avg. days to resolve</span></div></div>
<div class="rp-2"><div><h4>Most reported items</h4><table>{% for n, c in top_items %}<tr><td>{{ n }}</td><td style="text-align:right">{{ c }}</td></tr>{% else %}<tr><td class="mu">-</td></tr>{% endfor %}</table></div>
<div><h4>Locations with most reports</h4><table>{% for n, c in top_locs %}<tr><td>{{ n }}</td><td style="text-align:right">{{ c }}</td></tr>{% else %}<tr><td class="mu">-</td></tr>{% endfor %}</table></div></div>
<h4>Reports per month (last 6)</h4><table>{% for mo, c in months %}<tr><td>{{ mo }}</td><td style="text-align:right">{{ c }}</td></tr>{% else %}<tr><td class="mu">-</td></tr>{% endfor %}</table>
<a class="btn" href="{{ url_for('gso_export') }}">Download CSV (Excel)</a></div>
{% else %}<div class="card"><h2>{{ title }}</h2>
{% if tab == 'new' %}<p class="mu">Reports from the public form appear here (urgent ones first). Open a report and press <b>Mark as resolved</b> once the item is fixed, so the GSO has a record.</p>{% endif %}
<form method="get" class="rp-bar"><input type="hidden" name="tab" value="{{ tab }}"><input name="qs" value="{{ qs }}" placeholder="Search ticket, item, location, reporter"><button>Search</button>{% if qs %}<a class="btn s" href="{{ url_for('gso_dash', tab=tab) }}">Clear</a>{% endif %}</form>
<div class="tb"><table><tr><th>Ticket</th><th>Item / problem</th><th>Location</th><th>Reported by</th><th>Filed</th><th>Status</th><th></th></tr>
{% for x in rows %}<tr><td><a href="{{ url_for('gso_view', rid=x.id) }}">{{ x.ticket }}</a>{% if x.urgent %}<span class="rp-urg">URGENT</span>{% endif %}{% if dups and dups.get(x.id) %}<span class="rp-dup" title="Another report exists for the same item and place">DUPLICATE?</span>{% endif %}</td><td><b>{{ x.item }}</b><br><span class="mu">{{ x.problem }}</span></td><td>{{ x.location }}</td><td>{{ x.reporter }}<br><span class="mu">{{ x.contact }}</span></td><td>{{ x.created_at|dt12 }}</td><td>{{ rm.badge(x.status) }}</td><td><a class="btn sm" href="{{ url_for('gso_view', rid=x.id) }}">Open</a></td></tr>
{% else %}<tr><td colspan="7">No reports here.</td></tr>{% endfor %}</table></div></div>{% endif %}{% endblock %}"""

T["gso_backup.html"] = """{% extends 'rp_base.html' %}{% block rp %}{% import 'rp_macros.html' as rm %}{{ rm.gside(tab, cnt) }}
<div class="card"><h2>Backup &amp; Export</h2><p class="mu" style="font-size:14px">Download a copy of all the system's data (requests, equipment stock, users without passwords, notifications and damage reports with photos). Keep the file private - it contains names, student numbers and contact details.</p>
<a class="btn" href="{{ url_for('gso_backup_zip') }}">Download full backup (.zip)</a>
<div class="tb" style="margin-top:14px"><table><tr><th>Data</th><th>Records</th><th></th></tr>{% for n, label, c in info %}<tr><td>{{ label }}</td><td>{{ c }}</td><td><a class="btn s sm" href="{{ url_for('gso_backup_csv', name=n) }}">Download CSV</a></td></tr>{% endfor %}</table></div></div>{% endblock %}"""

T["gso_view.html"] = """{% extends 'rp_base.html' %}{% block rp %}{% import 'rp_macros.html' as rm %}{% import 'macros.html' as m %}{{ rm.gside(tab, cnt) }}
<p><a href="{{ url_for('gso_dash', tab='new') }}">&lsaquo; Back to reports</a></p>
<div class="rp-2"><div class="card"><h2>{{ r.ticket }} {% if r.urgent %}<span class="rp-urg">URGENT</span>{% endif %}</h2><p style="margin:-6px 0 12px">{{ rm.badge(r.status) }}</p>
<dl class="rp-dl"><dt>Item</dt><dd><b>{{ r.item }}</b></dd><dt>Problem type</dt><dd>{{ r.problem }}</dd><dt>Location</dt><dd>{{ r.location }}</dd><dt>Description</dt><dd style="white-space:pre-wrap">{{ r.description }}</dd>
<dt>Reported by</dt><dd>{{ r.reporter }}</dd><dt>Contact</dt><dd>{{ r.contact }}</dd><dt>College / Office</dt><dd>{{ r.dept or '-' }}</dd><dt>Date filed</dt><dd>{{ r.created_at|dt12 }}</dd>{% if r.resolved_at %}<dt>Resolved on</dt><dd>{{ r.resolved_at|dt12 }}</dd>{% endif %}
<dt>Tracking code</dt><dd style="font-family:monospace">{{ r.ticket }}-{{ r.token }}</dd></dl>
<p style="margin:14px 0 0"><a class="btn" href="{{ url_for('gso_pdf', rid=r.id) }}" target="_blank">Print report form (PDF)</a></p>
{% if r.has_photo %}<h4 style="margin:16px 0 4px">Photo</h4><a href="{{ url_for('gso_photo', rid=r.id) }}" target="_blank"><img class="rp-photo" src="{{ url_for('gso_photo', rid=r.id) }}" alt="Photo of the damaged item"></a>{% endif %}</div>
<div>{% if flags %}<div class="rp-warn"><b>Check before you act</b><ul>{% for k, t in flags %}<li>{{ t }}</li>{% endfor %}</ul>{% if sim %}<div>Similar reports: {% for x in sim[:6] %}<a href="{{ url_for('gso_view', rid=x.id) }}">{{ x.ticket }}</a> ({{ x.status }}){{ ', ' if not loop.last }}{% endfor %}</div>{% endif %}</div>{% endif %}
<div class="card"><h2>Status</h2><p style="margin:0 0 12px">{{ rm.badge(r.status) }}{% if r.resolved_at %} <span class="mu">on {{ r.resolved_at|dt12 }}</span>{% endif %}</p>
{% if r.status in ('Resolved', 'Rejected') %}<p class="mu" style="font-size:14px;margin-top:0">{{ 'This item has been fixed.' if r.status == 'Resolved' else 'This report was rejected and closed.' }} Made a mistake?</p><form method="post" action="{{ url_for('gso_reopen', rid=r.id) }}">{{ m.token() }}<button class="s">Reopen</button></form>
{% else %}<p class="mu" style="font-size:14px;margin-top:0">Press the button after the item is repaired or replaced. This is kept as a record.</p><form method="post" action="{{ url_for('gso_resolve', rid=r.id) }}">{{ m.token() }}<button class="gr"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg> Mark as resolved</button></form>
<hr style="border:0;border-top:1px solid var(--bd);margin:16px 0"><form method="post" action="{{ url_for('gso_reject', rid=r.id) }}" onsubmit="return confirm('Reject and close this report? The reporter will see the reason.')">{{ m.token() }}
<label>Not valid? Reject it as</label><select name="reason">{% for x in reasons %}<option {{ 'selected' if loop.first and sim }}>{{ x }}</option>{% endfor %}</select><input name="note" maxlength="300" placeholder="Optional note (the reporter can see it)"><button class="r">Reject report</button></form>{% endif %}</div>
<div class="card"><h2>History</h2><ul class="rp-tl">{% for u in ups %}<li>{{ rm.badge(u.status) }} {% if u.note %}{{ u.note }}{% endif %}<small>{{ u.created_at|dt12 }} &middot; {{ u.by_name }}{% if not u.public %} &middot; internal note{% endif %}</small></li>{% endfor %}</ul>
<form method="post" action="{{ url_for('gso_delete', rid=r.id) }}" onsubmit="return confirm('Delete this report permanently? Use this only for spam or duplicates.')">{{ m.token() }}<button class="sm r">Delete report</button></form></div></div></div>{% endblock %}"""

# =====================================================================================================================
# ADDED MODULE v2 - printable damage-report PDF, dark mode + lighter mobile pages, backup/export, late-return reminders, new calendar
# =====================================================================================================================
from markupsafe import escape as _esc
import zipfile

# ---- 1. Printable Damage Report form (PDF) ------------------------------------------------------------------------
def rp_make_pdf(r):
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4); H = 297 * mm
    ups = q("SELECT status,note,by_name,created_at FROM damage_updates WHERE report_id=? ORDER BY id", r["id"])
    def T(t, x, y, s=10, b=False, a="l", i=False):
        c.setFont("Helvetica-BoldOblique" if b and i else "Helvetica-Bold" if b else "Helvetica-Oblique" if i else "Helvetica", s)
        (c.drawCentredString if a == "c" else c.drawString)(x * mm, H - y * mm, str(t or ""))
    def L(x1, y, x2): c.line(x1 * mm, H - y * mm, x2 * mm, H - y * mm)
    def R(x, y, w, h): c.rect(x * mm, H - (y + h) * mm, w * mm, h * mm)
    def B(x, y, on):
        R(x, y - 3.5, 4, 4)
        if on: T("X", x + .8, y - .2, 9, True)
    def fit(t, w, s=10, b=False):
        t = str(t or ""); f = "Helvetica-Bold" if b else "Helvetica"
        while t and c.stringWidth(t, f, s) > w * mm: t = t[:-1]
        return t
    for n, x in (("1", 28), ("2", 158)):
        c.drawImage(ImageReader(io.BytesIO(base64.b64decode(LOGOS[n]))), x * mm, H - 34 * mm, 24 * mm, 24 * mm, mask="auto")
    T("Southern Luzon State University", 105, 18, 13, True, "c"); T("Judge Guillermo Eleazar", 105, 23, 11, True, "c")
    T("GENERAL SERVICES OFFICE", 105, 28, 9, False, "c"); T("Tagkawayan, Quezon", 105, 32, 8, False, "c")
    R(135, 35, 60, 9); T("Report No:", 137, 39, 8); T(r["ticket"], 152, 42.5, 10, True)
    T("DAMAGE / REPAIR REPORT FORM", 105, 52, 12, True, "c")
    y = 62
    T("Reported by:", 15, y); T(fit(r["reporter"], 76), 42, y - .5); L(40, y + 1, 120); T("Date filed:", 125, y); T(fit(dt12(r["created_at"]), 48, 9), 146, y - .5, 9); L(144, y + 1, 195); y += 8
    T("College/Office:", 15, y); T(fit(r["dept"], 76), 42, y - .5); L(40, y + 1, 120); T("Contact:", 125, y); T(fit(r["contact"], 54, 9), 140, y - .5, 9); L(138, y + 1, 195); y += 8
    T("Equipment/Item:", 15, y); T(fit(r["item"], 148), 45, y - .5); L(43, y + 1, 195); y += 8
    T("Location:", 15, y); T(fit(r["location"], 160), 33, y - .5); L(31, y + 1, 195); y += 8
    T("Type of problem:", 15, y); T(fit(r["problem"], 92, 9), 45, y - .5, 9); L(43, y + 1, 140); B(148, y, bool(r["urgent"])); T("URGENT / safety hazard", 154, y, 8); y += 9
    T("Description of the problem:", 15, y, 10, True); y += 6
    lines = simpleSplit(r["description"] or "", "Helvetica", 10, 178 * mm)[:6]
    for k in range(6):
        if k < len(lines): T(lines[k], 17, y + 5 * k)
        L(15, y + 1 + 5 * k, 195)
    y += 33
    T("Photo:", 15, y, 9, True); R(15, y + 2, 90, 42)
    ph = q1("SELECT photo FROM damage_reports WHERE id=?", r["id"])
    drawn = False
    if ph and ph["photo"]:
        try:
            c.drawImage(ImageReader(io.BytesIO(base64.b64decode(ph["photo"]))), 16 * mm, H - (y + 43) * mm, 88 * mm, 40 * mm, preserveAspectRatio=True, anchor="c"); drawn = True
        except Exception: pass
    if not drawn: T("No photo attached" if not (ph and ph["photo"]) else "(photo could not be printed)", 60, y + 24, 8, False, "c", True)
    T("Status (GSO use):", 112, y, 9, True)
    for k, s_ in enumerate(RP_STATUSES): B(114, y + 8 + 7 * k, r["status"] == s_); T(s_, 120, y + 8 + 7 * k, 9)
    if r["resolved_at"]: T("Resolved on: " + dt12(r["resolved_at"]), 112, y + 38, 8)
    y += 50
    T("Action taken / Findings / Remarks (GSO):", 15, y, 9, True); y += 7
    notes = [u for u in ups if u["note"] and u["by_name"] != "System"][-3:]
    out = []
    for u in notes: out += simpleSplit("%s [%s]: %s" % (dt12(u["created_at"]), u["status"], u["note"]), "Helvetica", 8, 176 * mm)
    for k in range(4):
        if k < len(out): T(out[k], 17, y + 7 * k - .5, 8)
        L(15, y + 7 * k + 1, 195)
    y += 36
    L(15, y, 70); L(80, y, 135); L(145, y, 195)
    T(fit(r["reporter"], 54, 9, True), 42.5, y - 1, 9, True, "c")
    T("Reported by", 42.5, y + 4, 8, False, "c"); T("Received by (GSO staff) / Date", 107.5, y + 4, 8, False, "c"); T("Repaired by / Date", 170, y + 4, 8, False, "c")
    y += 16
    L(70, y, 140); T("RASELIETO B. GARCIA", 105, y - 1, 9, True, "c"); T("Head, General Services Office", 105, y + 4, 8, False, "c")
    T("Printed: " + dt12(now_s()), 15, 288, 7, False, "l", True)
    c.showPage(); c.save(); buf.seek(0); return buf

@app.route("/gso/report/<int:rid>/pdf")
@admin_required
def gso_pdf(rid):
    r = q1("SELECT %s FROM damage_reports WHERE id=?" % RP_COLS, rid)
    if not r: abort(404)
    return send_file(rp_make_pdf(r), mimetype="application/pdf", download_name="DamageReport-%s.pdf" % r["ticket"])

# ---- 2. Dark mode + lighter pages on phones -----------------------------------------------------------------------
try:
    from PIL import Image as _PILImage
    for _k in ("1", "2"):                       # the header logos are shown at ~60px, so a 140px copy is plenty (original: 300px)
        _im = _PILImage.open(io.BytesIO(base64.b64decode(LOGOS[_k]))).convert("RGBA"); _im.thumbnail((140, 140))
        _bf = io.BytesIO(); _im.save(_bf, "PNG", optimize=True); LOGOS[_k + "s"] = base64.b64encode(_bf.getvalue()).decode()
except Exception:
    LOGOS["1s"], LOGOS["2s"] = LOGOS["1"], LOGOS["2"]

_BG_S = {}
@app.route("/bg-s.jpg")
def bg_small():
    if "d" not in _BG_S:
        try:
            im = _PILImage.open(io.BytesIO(base64.b64decode(BG_IMG))).convert("RGB"); im.thumbnail((640, 640))
            bf = io.BytesIO(); im.save(bf, "JPEG", quality=55, optimize=True, progressive=True); _BG_S["d"] = bf.getvalue()
        except Exception:
            _BG_S["d"] = base64.b64decode(BG_IMG)
    return Response(_BG_S["d"], mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=300"})

RP_DARK = """
:root{--bgimg:url('/bg.jpg')}@media(max-width:800px){:root{--bgimg:url('/bg-s.jpg')}}
body.authbg{--ov:rgba(255,255,255,.10)}body.dash{--ov:rgba(255,255,255,0)}
body.authbg::before,body.dash::before{background:linear-gradient(var(--ov),var(--ov)),var(--bgimg) center/cover no-repeat}
html[data-theme=dark] body.authbg{--ov:rgba(8,14,20,.72)}html[data-theme=dark] body.dash{--ov:rgba(8,14,20,.84)}
html[data-theme=dark]{color-scheme:dark;--bg:#0f1720;--card:#17212c;--tx:#e8eef5;--mu:#9fb0c0;--pr:#4a82cf;--bd:#2a3948}
html[data-theme=dark] header{background:#12342a;color:#d8f3dc;border-bottom-color:#2d6a4f}html[data-theme=dark] header a,html[data-theme=dark] header button.lk{color:#d8f3dc}
html[data-theme=dark] .authbg .card,html[data-theme=dark] .dash .card{background:rgba(23,33,44,.95)}
html[data-theme=dark] .dash .tabs a{background:#17212c}html[data-theme=dark] .dash .tabs a.on{background:var(--pr)}
html[data-theme=dark] .dash .wrap>p.mu,html[data-theme=dark] .dash .who{background:rgba(23,33,44,.92)}
html[data-theme=dark] .modal,html[data-theme=dark] .npanel{background:#17212c;color:var(--tx)}html[data-theme=dark] .ni{border-bottom-color:var(--bd)}html[data-theme=dark] .ni:hover{background:#1e2c3a}html[data-theme=dark] .ni.new{background:#18301f}
html[data-theme=dark] .bell{background:#17212c;color:#95d5b2}html[data-theme=dark] .badge{box-shadow:0 0 0 2px #17212c}
html[data-theme=dark] .stat,html[data-theme=dark] .chips a{background:#1e2c3a;border-color:var(--bd);color:var(--tx)}html[data-theme=dark] .stat b{color:#95d5b2}html[data-theme=dark] .chips a.on,html[data-theme=dark] .chips a:hover{background:#2d6a4f;color:#fff}
html[data-theme=dark] .g3 h4{color:#95d5b2}html[data-theme=dark] .tb tr:hover td{background:rgba(120,170,255,.08)}
html[data-theme=dark] .rp-code{background:#102a20;border-color:#2d6a4f}html[data-theme=dark] .sc td.off{background:rgba(255,255,255,.03)}
html[data-theme=dark] .msg.err{background:#4a1f1f;color:#ffd0d0}html[data-theme=dark] .msg.ok{background:#17402a;color:#c9f2d8}
html[data-theme=dark] .cal i.Pending{color:#1b2333}
#themeBtn{position:fixed;left:12px;bottom:14px;z-index:35;width:40px;height:40px;border-radius:50%;margin:0;padding:0;display:grid;place-items:center;background:#fff;color:#14532d;border:1px solid var(--bd);box-shadow:0 3px 10px rgba(0,0,0,.25)}
#themeBtn svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
html[data-theme=dark] #themeBtn{background:#17212c;color:#ffd166}
@media print{ #themeBtn{display:none}}
"""
RP_THEME_INIT = "<script>try{var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}</script>"
RP_THEME_BTN = ('<button type="button" id="themeBtn" aria-label="Switch dark / light mode" title="Dark / light mode" onclick="var d=document.documentElement,t=d.getAttribute(\'data-theme\')===\'dark\'?\'light\':\'dark\';'
                'd.setAttribute(\'data-theme\',t);try{localStorage.setItem(\'theme\',t)}catch(e){}">'
                '<svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></button>')
_b = T["base.html"]
_b = _b.replace("url_for('logo', n='1')", "url_for('logo', n='1s')").replace("url_for('logo', n='2')", "url_for('logo', n='2s')")
_b = _b.replace("</style></head>", RP_DARK + "</style>" + RP_THEME_INIT + "</head>", 1)
_b = _b.replace("</body></html>", RP_THEME_BTN + "</body></html>", 1)
T["base.html"] = _b

# ---- 4. Mobile optimisation for every page + background that never moves (additive; nothing above is changed) ---------
RP_MOBILE = r"""
/* background layer: sized to the LARGEST phone viewport so it does not resize/shift when the browser bar hides while scrolling */
body.authbg::before,body.dash::before{height:100vh;height:100lvh;min-height:100%;transform:translate3d(0,0,0);backface-visibility:hidden;will-change:transform}
html{-webkit-text-size-adjust:100%}
@media(max-width:700px){
 body{padding-bottom:88px}
 .wrap{margin:14px auto;padding-left:12px;padding-right:12px}.dash .wrap{padding-left:52px;padding-right:12px}
 .wrap .card{padding:16px 14px;border-radius:14px}.card h2{font-size:21px;line-height:1.25}.card h4{margin-top:14px}
 input,select,textarea{font-size:16px!important;min-height:44px;max-width:100%}
 input[type=checkbox],input[type=radio]{min-height:0}
 input[type=month],input[type=date],input[type=time]{display:block;width:100%;min-width:0;-webkit-appearance:none;appearance:none;text-align:left;line-height:1.2}
 button,.btn{min-height:44px;font-size:15px}.sm,.btn.sm{min-height:38px}
 .tabs{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}.tabs a{white-space:nowrap;flex:none}
 /* monthly report toolbar: Previous | Next on one row, then the form full width */
 .rbar{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
 .rbar>.btn{display:flex;align-items:center;justify-content:center;width:100%;margin:0;text-align:center;box-sizing:border-box}
 .rbar>.btn:first-child{grid-row:1;grid-column:1}.rbar>.btn:last-child{grid-row:1;grid-column:2}
 .rbar>.rf{grid-row:2;grid-column:1/-1;flex-direction:column;align-items:stretch;gap:10px;min-width:0}
 .rf input,.rf select,.rf button{width:100%;flex:none;margin:0;box-sizing:border-box}
 .chips{gap:8px}.chips a{padding:7px 14px}
 .stats{grid-template-columns:1fr 1fr;gap:10px}.stat{padding:12px}.stat b{font-size:26px}
 .g3{grid-template-columns:1fr}
 /* booking calendar toolbar */
 .sc-bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
 .sc-bar>.btn{flex:1 1 0;display:flex;align-items:center;justify-content:center;margin:0;text-align:center}
 .sc-bar>b{flex:1 1 100%;margin:4px 0 0!important}.sc-bar>.mu{flex:1 1 100%}
 form.sc-bar{flex-direction:column;align-items:stretch}form.sc-bar input,form.sc-bar select,form.sc-bar button{width:100%;flex:none;margin:0;box-sizing:border-box}
 /* public / GSO search bars */
 .rp-bar{display:grid;grid-template-columns:1fr auto;gap:10px}.rp-bar input{min-width:0;width:100%;grid-column:1/-1}.rp-bar button,.rp-bar .btn{width:100%;display:flex;align-items:center;justify-content:center;margin:0}
 .rp-bar button:only-of-type{grid-column:1/-1}
 /* tables become tidy cards (labels are added by the script below) */
 table.rpm,table.rpm tbody,table.rpm tr,table.rpm td{display:block;width:100%;box-sizing:border-box}
 table.rpm tr.rph{display:none}
 table.rpm tr{border:1px solid var(--bd);border-radius:12px;margin:0 0 12px;padding:6px 12px;background:rgba(255,255,255,.7)}
 table.rpm td{display:flex;gap:12px;justify-content:space-between;align-items:flex-start;padding:8px 0;border:0;border-bottom:1px dashed var(--bd);text-align:right;word-break:break-word}
 table.rpm td:last-child{border-bottom:0}
 table.rpm td::before{content:attr(data-label);flex:0 0 38%;max-width:38%;text-align:left;font-size:12px;font-weight:700;color:var(--mu);text-transform:uppercase;letter-spacing:.03em;padding-top:2px}
 table.rpm td[data-label=""]::before{display:none}
 table.rpm td .mu{display:block}
 table.rpm tr:hover td{background:none}
 table.rpm .rpc{flex:1 1 auto;min-width:0;text-align:right}
 table.rpm td.rpa{flex-direction:column;gap:8px}table.rpm td.rpa::before{flex:none;max-width:100%}
 table.rpm td.rpa .rpc{width:100%;text-align:left;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
 table.rpm td.rpa form,table.rpm td.rpa form.in{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0}
 table.rpm td.rpa input:not([type=hidden]):not([type=checkbox]){flex:1 1 110px;width:auto;max-width:none;min-width:90px;text-align:left}
 table.rpm td.rpa button{margin:0}
 table.rpm td .rpc br{display:none}table.rpm td .rpc .mu{display:block}
 .card>table:not(.sc):not(.tb){display:block;overflow-x:auto}
 .pill,.chips{max-width:100%}
 .side{width:min(300px,86vw)}.bell{top:auto;bottom:18px;right:14px}.npanel{top:auto;bottom:76px;right:8px;width:calc(100vw - 16px);max-height:65vh}
 .modal{max-height:92vh}.mb{padding:16px}
 .toast{left:12px;right:12px;bottom:76px}
 .rp-code{word-break:break-all}
 .rp-choices{gap:12px}
 .erow{flex-wrap:wrap;gap:8px;margin-bottom:10px}.erow select{flex:1 1 100%;order:1;min-width:0}.erow input{flex:1 1 90px;min-width:0;order:3;margin:0}
 .erow input[name=item_other]{flex:1 1 100%!important;order:2}.erow input[name=name]{flex:1 1 100%;order:1}.erow button{flex:0 0 52px;order:4;margin:0!important;padding:0}
 #eq+button,#eq~button.s{width:100%}
 header{padding:8px 8px}
}
@media(max-width:380px){.stats{grid-template-columns:1fr 1fr}.tabs a{padding:7px 10px;font-size:13px}}
"""
RP_MOBILE_JS = """<script>(function(){function go(){try{document.querySelectorAll('table').forEach(function(t){if(t.classList.contains('sc'))return;var r0=t.rows[0];if(!r0||!r0.cells.length)return;var h=[];for(var k=0;k<r0.cells.length;k++){if(r0.cells[k].tagName!=='TH')return;h.push((r0.cells[k].textContent||'').trim())}r0.classList.add('rph');for(var n=1;n<t.rows.length;n++){var i=0,cs=t.rows[n].cells;for(var m=0;m<cs.length;m++){var td=cs[m];td.setAttribute('data-label',h[i]||'');i+=td.colSpan||1;if(!td.querySelector('.rpc')){var w=document.createElement('div');w.className='rpc';while(td.firstChild)w.appendChild(td.firstChild);td.appendChild(w);if(w.querySelector('button,input:not([type=hidden]),select'))td.classList.add('rpa')}}}t.classList.add('rpm')})}catch(e){}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',go);else go()})();</script>"""
_b = T["base.html"]
_b = _b.replace("</style>" + RP_THEME_INIT, RP_MOBILE + "</style>" + RP_THEME_INIT, 1)
_b = _b.replace("</body></html>", RP_MOBILE_JS + "</body></html>", 1)
T["base.html"] = _b

# ---- 5. "Other" equipment: type the name right inside the Other box (additive; server still gets item_name=__other + item_other) ----
RP_EQ = """<script>(function(){var op=window.paint;if(typeof op!=='function')return;
window.paint=function(){op.apply(this,arguments);document.querySelectorAll('.erow').forEach(function(r){var s=r.querySelector('select[name=item_name]'),o=r.querySelector('[name=item_other]');if(!s||!o)return;
var oth=s.value==='__other',b=r.querySelector('.eb');r.classList.toggle('eo',oth);s.style.display=oth?'none':'';o.placeholder='Other equipment - type the name here';
var alt=[].some.call(s.options,function(x){return x.value!=='__other'&&!x.disabled});
if(oth&&alt){if(!b){b=document.createElement('button');b.type='button';b.className='eb';b.textContent='List';b.title='Choose from the list instead';
b.onclick=function(){var f=[].filter.call(s.options,function(x){return x.value!=='__other'&&!x.disabled})[0];if(f){s.value=f.value;o.value='';window.paint()}};r.insertBefore(b,r.querySelector('button.r'))}}else if(b)b.remove()})};
if(typeof READY!=='undefined'&&READY)window.paint()})();</script>"""
RP_EQ_CSS = """
.erow.eo input[name=item_other]{flex:2 1 160px!important;order:-1!important;border-color:var(--pr)}
html[data-theme=dark] table.rpm tr{background:rgba(30,44,58,.92);border-color:var(--bd)}
html[data-theme=dark] table.rpm td{border-bottom-color:var(--bd);color:var(--tx)}
html[data-theme=dark] table.rpm td::before{color:#9fb0c0}
html[data-theme=dark] table.rpm td .mu,html[data-theme=dark] table.rpm .rpc .mu{color:#9fb0c0}
html[data-theme=dark] table.rpm td a{color:#8ab4ff}
html[data-theme=dark] .erow .eb{background:#1e2c3a;color:#8ab4ff;border-color:#4a82cf}
html[data-theme=dark] input,html[data-theme=dark] select,html[data-theme=dark] textarea{background:#1e2c3a;color:var(--tx);border-color:var(--bd)}
html[data-theme=dark] .btn.s{background:transparent;color:#8ab4ff;border-color:#4a82cf}
html[data-theme=dark] .sc-bar b{color:var(--tx)}
html[data-theme=dark] a.btn:not(.s),html[data-theme=dark] button:not(.s):not(.r):not(.gr):not(.eye):not(.lk):not(.x):not(.eb){color:#fff}
.erow .eb{margin:0;padding:0 12px;min-height:40px;background:#fff;color:var(--pr);border:1px solid var(--pr);border-radius:6px;font-size:13px;font-weight:600;order:4}
@media(max-width:700px){.erow.eo input[name=item_other]{flex:1 1 100%!important;order:1!important}.erow.eo input[name=item_qty]{order:2}.erow .eb{flex:1 1 auto;order:3;min-height:44px}.erow.eo button.r{order:4}}
"""
_s = T["student.html"]
_i = _s.index("function addEq")
_j = _s.index("</script>", _i) + len("</script>")
_s = _s[:_j] + RP_EQ + _s[_j:]
T["student.html"] = _s
_b = T["base.html"]
_b = _b.replace("</style>" + RP_THEME_INIT, RP_EQ_CSS + "</style>" + RP_THEME_INIT, 1)
T["base.html"] = _b

# ---- 6. Transparent sign-in / sign-up / change-password boxes (letters and buttons stay readable) + report prompt ----------
RP_AUTH_CSS = """
/* glass panel: the photo shows through (blurred, lightly tinted); white letters with a soft shadow stay readable in light AND dark mode */
.authbg .card.auth:not(.rp-narrow){position:relative;background:rgba(8,22,15,.30)!important;border:1px solid rgba(255,255,255,.45)!important;border-radius:16px;box-shadow:0 12px 36px rgba(0,0,0,.35)!important;-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px)}
.authbg .card.auth:not(.rp-narrow) h2,.authbg .card.auth:not(.rp-narrow) p,.authbg .card.auth:not(.rp-narrow) a:not(.btn),.authbg .card.auth:not(.rp-narrow) label{color:#fff!important;font-weight:700;text-shadow:0 1px 3px rgba(0,0,0,.9),0 0 8px rgba(0,0,0,.55)}
.authbg .card.auth:not(.rp-narrow) h2{font-size:30px;font-weight:800}
.authbg .card.auth:not(.rp-narrow) input{background:rgba(255,255,255,.93)!important;color:#10202b!important;font-weight:600;border:1px solid rgba(255,255,255,.9);box-shadow:0 2px 8px rgba(0,0,0,.3);text-shadow:none}
.authbg .card.auth:not(.rp-narrow) .fe{color:#ffc2b8!important;font-weight:700;text-shadow:0 1px 3px rgba(0,0,0,.9)}
.authbg .card.auth:not(.rp-narrow) .msg{background:rgba(255,255,255,.93);color:#10202b;text-shadow:none;border-radius:8px}
.authbg .card.auth:not(.rp-narrow) button:not(.eye){box-shadow:0 4px 12px rgba(0,0,0,.45);border:1px solid rgba(255,255,255,.55)}
.authbg .card.auth:not(.rp-narrow) .btn.s{background:rgba(255,255,255,.93);color:#14417b!important;text-shadow:none;border-color:#fff}
.authbg .card.auth:not(.rp-narrow) .eye{color:#14417b;text-shadow:none}
html[data-theme=dark] .authbg .card.auth:not(.rp-narrow){background:rgba(8,16,26,.42)!important;border-color:rgba(255,255,255,.35)!important}
html[data-theme=dark] .authbg .card.auth:not(.rp-narrow) input{background:rgba(8,16,26,.6)!important;color:#fff!important;border-color:rgba(255,255,255,.5)}
html[data-theme=dark] .authbg .card.auth:not(.rp-narrow) .eye{color:#9bd0ff}
html[data-theme=dark] .authbg .card.auth:not(.rp-narrow) .btn.s{background:rgba(23,33,44,.92);color:#cfe3ff!important}
.rp-lr{position:absolute;right:20px;bottom:16px;text-align:right}
.rp-lq{color:#fff;font:700 13px/1.3 system-ui,sans-serif;margin-bottom:5px;text-shadow:0 1px 3px rgba(0,0,0,.9),0 0 8px rgba(0,0,0,.55)}
.rp-lb{display:inline-block;background:#b45309;color:#fff!important;padding:7px 20px;border-radius:20px;font:700 13px system-ui,sans-serif;text-decoration:none;text-shadow:none!important;border:1px solid rgba(255,255,255,.7);box-shadow:0 4px 12px rgba(0,0,0,.45)}
.rp-lb:hover{background:#c2610c;opacity:1}
.rp-lt{background:#14417b}.rp-lt:hover{background:#1c56a0}
@media(max-width:560px){.rp-lr{position:static;margin-top:16px}}
"""
_b = T["base.html"]
_b = _b.replace("</style>" + RP_THEME_INIT, RP_AUTH_CSS + "</style>" + RP_THEME_INIT, 1)
T["base.html"] = _b

# ---- 7. Floating-label inputs with an animated glowing border (every input on the site; CSS-only, labels are tagged by a small script) ----
RP_FL_CSS = """
input:not([type=checkbox]):not([type=radio]):not([type=file]):not([type=hidden]):not([type=submit]):not([type=button]),select,textarea{transition:border-color .25s ease,box-shadow .25s ease,background-color .25s ease}
input:focus,select:focus,textarea:focus{border-color:#22d3ee!important;box-shadow:0 0 0 3px rgba(34,211,238,.22),0 0 18px rgba(34,211,238,.5)!important;outline:none}
@supports selector(:has(*)){
 label.fl{display:block;width:max-content;max-width:calc(100% - 20px);height:16px;margin:0 0 -16px!important;padding:0;position:relative;z-index:3;pointer-events:none;overflow:hidden;text-overflow:ellipsis;box-sizing:border-box;white-space:nowrap;font-size:15px!important;font-weight:500!important;line-height:16px;color:#5b6b7b!important;text-shadow:none!important;background:none!important;transform:translate(14px,37px);transform-origin:left top;transition:transform .24s cubic-bezier(.2,.8,.3,1),font-size .24s ease,color .2s ease}
 label.fl + input,label.fl + select,label.fl + textarea,label.fl + .pw{margin-top:22px!important}
 .g>*,form>div,.wrap .card>div{min-width:0}
 label.fl + input,label.fl + select{height:46px}label.fl + .pw input{height:46px}
 label.fl:has(+ textarea){transform:translate(14px,34px)}
 label.fl.up,label.fl:has(+ input:focus),label.fl:has(+ select:focus),label.fl:has(+ textarea:focus),label.fl:has(+ .pw input:focus),
 label.fl:has(+ input:not(:placeholder-shown)),label.fl:has(+ textarea:not(:placeholder-shown)),label.fl:has(+ .pw input:not(:placeholder-shown)){transform:translate(2px,1px);font-size:12px!important;font-weight:700!important;color:#0e7490!important}
 label.fl:has(+ input:focus),label.fl:has(+ select:focus),label.fl:has(+ textarea:focus),label.fl:has(+ .pw input:focus){color:#0891b2!important}
 label.fl + input::placeholder,label.fl + textarea::placeholder{color:transparent}
 label.fl:has(+ input:focus) + input::placeholder,label.fl.up + input::placeholder,label.fl.up + textarea::placeholder,label.fl:has(+ textarea:focus) + textarea::placeholder{color:#8a97a5}
 html[data-theme=dark] label.fl{color:#9fb0c0!important}
 html[data-theme=dark] label.fl.up,html[data-theme=dark] label.fl:has(+ input:focus),html[data-theme=dark] label.fl:has(+ select:focus),html[data-theme=dark] label.fl:has(+ textarea:focus),html[data-theme=dark] label.fl:has(+ .pw input:focus),
 html[data-theme=dark] label.fl:has(+ input:not(:placeholder-shown)),html[data-theme=dark] label.fl:has(+ textarea:not(:placeholder-shown)),html[data-theme=dark] label.fl:has(+ .pw input:not(:placeholder-shown)){color:#67e8f9!important}
 /* sign-in style pages: letters are white with a soft shadow, resting label dark so it reads inside the white field */
 .authbg .card.auth:not(.rp-narrow) label.fl{color:#4b5b6b!important;text-shadow:none!important;font-weight:600!important}
 .authbg .card.auth:not(.rp-narrow) label.fl.up,.authbg .card.auth:not(.rp-narrow) label.fl:has(+ input:focus),.authbg .card.auth:not(.rp-narrow) label.fl:has(+ .pw input:focus),.authbg .card.auth:not(.rp-narrow) label.fl:has(+ input:not(:placeholder-shown)),.authbg .card.auth:not(.rp-narrow) label.fl:has(+ .pw input:not(:placeholder-shown)){color:#fff!important;text-shadow:0 1px 3px rgba(0,0,0,.95),0 0 8px rgba(0,0,0,.6)!important;font-weight:700!important}
 html[data-theme=dark] .authbg .card.auth:not(.rp-narrow) label.fl{color:#b8c7d6!important}
 html[data-theme=dark] .authbg .card.auth:not(.rp-narrow) label.fl.up,html[data-theme=dark] .authbg .card.auth:not(.rp-narrow) label.fl:has(+ input:focus),html[data-theme=dark] .authbg .card.auth:not(.rp-narrow) label.fl:has(+ .pw input:focus),html[data-theme=dark] .authbg .card.auth:not(.rp-narrow) label.fl:has(+ input:not(:placeholder-shown)),html[data-theme=dark] .authbg .card.auth:not(.rp-narrow) label.fl:has(+ .pw input:not(:placeholder-shown)){color:#67e8f9!important}
}
"""
RP_FL_JS = """<script>(function(){function pick(n){if(!n)return null;if(n.classList&&n.classList.contains('pw'))n=n.querySelector('input');if(!n||!/^(INPUT|SELECT|TEXTAREA)$/.test(n.tagName))return null;
var t=(n.type||'').toLowerCase();if(n.tagName==='INPUT'&&/^(hidden|checkbox|radio|file|submit|button|image|reset|range|color)$/.test(t))return null;return n}
function go(){try{document.querySelectorAll('label').forEach(function(l){if(l.classList.contains('fl')||l.closest('.ck,.erow,.tp,.modalbg,.side'))return;var f=pick(l.nextElementSibling);if(!f)return;
var t=(f.type||'').toLowerCase();if(f.tagName==='SELECT'||/^(date|month|time|datetime-local|week)$/.test(t)||(f.placeholder&&f.placeholder.trim()))l.classList.add('up');else if(!f.placeholder)f.placeholder=' ';l.classList.add('fl')})}catch(e){}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){setTimeout(go,0)});else setTimeout(go,0)})();</script>"""
_b = T["base.html"]
_b = _b.replace("</style>" + RP_THEME_INIT, RP_FL_CSS + "</style>" + RP_THEME_INIT, 1)
_b = _b.replace("</body></html>", RP_FL_JS + "</body></html>", 1)
T["base.html"] = _b


# ---- 9. No browser autofill on Sign In / Create account ------------------------------------------------------------------
RP_NOFILL_JS = """<script>(function(){function go(){try{var fs=document.querySelectorAll('form.auth');if(!fs.length||/forgot/.test(location.pathname))return;
fs.forEach(function(f){if(!f.querySelector('[name=username]'))return;f.setAttribute('autocomplete','off');
f.querySelectorAll('input').forEach(function(i){var t=(i.type||'').toLowerCase();if(/^(hidden|submit|button|checkbox)$/.test(t))return;
i.setAttribute('autocomplete',t==='password'?'new-password':'off');i.readOnly=true;
var un=function(){i.readOnly=false};i.addEventListener('focus',un);i.addEventListener('mousedown',un);i.addEventListener('touchstart',un,{passive:true});
['keydown','paste','input'].forEach(function(ev){i.addEventListener(ev,function(e){if(e.isTrusted)i.dataset.t='1'})});
function wipe(){if(!i.dataset.t&&i.value)i.value=''}[50,300,800,1600,3000].forEach(function(ms){setTimeout(wipe,ms)});window.addEventListener('pageshow',wipe)})})}catch(e){}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',go);else go()})();</script>"""
_b = T["base.html"]; _b = _b.replace("</body></html>", RP_NOFILL_JS + "</body></html>", 1); T["base.html"] = _b

# ---- 10. Filters apply by themselves (no Apply/Show button) + slightly smaller Prev/Today/Next buttons ---------------------
RP_AUTO_CSS = """
form.rp-auto button{display:none!important}
.sc-bar>.btn,.rbar>.btn{padding:5px 12px!important;font-size:13px!important;line-height:1.25;min-height:32px;margin:0}
@media(max-width:700px){.sc-bar>.btn,.rbar>.btn{min-height:38px!important}}
"""
RP_AUTO_JS = """<script>(function(){function go(){try{document.querySelectorAll('form.sc-bar,form.rf').forEach(function(f){f.classList.add('rp-auto');var tm=null;
f.querySelectorAll('select').forEach(function(s){s.addEventListener('change',function(){f.requestSubmit()})});
f.querySelectorAll('input[type=month],input[type=date]').forEach(function(i){i.addEventListener('change',function(){clearTimeout(tm);tm=setTimeout(function(){var v=i.value;if(!v||parseInt(v.slice(0,4),10)<2000)return;f.requestSubmit()},350)})})})}catch(e){}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',go);else go()})();</script>"""
_b = T["base.html"]
_b = _b.replace("</style>" + RP_THEME_INIT, RP_AUTO_CSS + "</style>" + RP_THEME_INIT, 1)
_b = _b.replace("</body></html>", RP_AUTO_JS + "</body></html>", 1); T["base.html"] = _b

# ---- 11. White cards on the dashboards + smaller Cancel / Back buttons --------------------------------------------------------
RP_SM_CSS = """
html:not([data-theme=dark]) .dash .card{background:#fff!important;-webkit-backdrop-filter:none;backdrop-filter:none}
.rpsm{padding:5px 13px!important;font-size:13px!important;min-height:32px!important;line-height:1.2!important;margin-top:0!important;vertical-align:middle;width:auto!important;display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box}
@media(min-width:561px){.card.auth:has(.rp-lr){padding-bottom:88px}}
.auth .rpsm{margin-top:24px!important;margin-left:8px;vertical-align:top}
@media(max-width:700px){.rpsm{min-height:36px!important}}
"""
RP_SM_JS = """<script>(function(){function go(){try{document.querySelectorAll('a.btn,button').forEach(function(b){if(b.classList.contains('s')&&/^\\s*(Cancel|Back)/i.test(b.textContent||''))b.classList.add('rpsm')})}catch(e){}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){setTimeout(go,0)});else setTimeout(go,0);
new MutationObserver(function(){go()}).observe(document.documentElement,{childList:true,subtree:true})})();</script>"""
_b = T["base.html"]
_b = _b.replace("</style>" + RP_THEME_INIT, RP_SM_CSS + "</style>" + RP_THEME_INIT, 1)
_b = _b.replace("</body></html>", RP_SM_JS + "</body></html>", 1); T["base.html"] = _b

# ---- 12. New time picker (hour / minute chips in a pop-up, AM|PM switch) - drives the old Hour/Min/AM selects, so nothing else changes ----
RP_TP_CSS = """
.tpk{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;height:46px;margin:22px 0 0!important;padding:0 14px!important;background:var(--bg);color:var(--tx);border:1px solid var(--bd)!important;border-radius:6px;font:inherit;font-size:15px;font-weight:600;text-align:left;box-shadow:none!important;transform:none!important;min-height:46px!important}
.tpk:hover:not(:disabled){background:var(--bg)!important;border-color:#22d3ee!important;transform:none!important}
.tpk .tv.ph{color:#8a97a5;font-weight:500}.tpk svg{width:20px;height:20px;stroke:#0e7490;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none}
label.tpl{display:block;font-size:12px!important;font-weight:700!important;color:#0e7490!important;margin-bottom:-18px;position:relative;z-index:1}
.tpp{position:absolute;z-index:9999;width:292px;max-width:calc(100vw - 16px);background:#fff;color:#1b2a38;border:1px solid #cdd7e1;border-radius:14px;box-shadow:0 16px 40px rgba(0,0,0,.28);padding:14px}
.tpp .am{display:flex;background:#eef2f6;border-radius:999px;padding:3px;margin-bottom:12px}
.tpp .am button,.tpp .g button{background:transparent;color:inherit;border:0;box-shadow:none!important;transform:none!important;margin:0;padding:0;min-height:0!important;font:inherit;cursor:pointer}
.tpp .am button{flex:1;padding:8px 0;border-radius:999px;font-weight:700;font-size:14px;color:#5b6b7b}
.tpp .am button.on{background:#14417b;color:#fff}
.tpp h6{margin:0 0 6px;font:700 11px system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;color:#6b7a89}
.tpp .g{display:grid;grid-template-columns:repeat(6,1fr);gap:6px;margin-bottom:12px}
.tpp .g button{height:38px;border-radius:10px;border:1px solid #d5dee7;background:#f6f8fa;font-weight:600;font-size:14px}
.tpp .g button:hover{background:#e3f4f8!important;border-color:#22d3ee;transform:none!important}
.tpp .g button.on{background:#0e7490!important;color:#fff!important;border-color:#0e7490}
.tpp .ft{display:flex;justify-content:space-between;gap:8px}.tpp .ft button{margin:0;min-height:34px!important;padding:6px 16px;font-size:13px;border-radius:8px}
.tpbg{display:none}
@media(max-width:700px){.tpp{position:fixed;left:0!important;right:0;bottom:0;top:auto!important;width:auto;max-width:none;border-radius:18px 18px 0 0;padding:16px 16px 20px}.tpp .g button{height:44px}
 .tpbg{display:block;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9998}}
html[data-theme=dark] .tpp{background:#17212c;color:#e6edf4;border-color:#33465a}html[data-theme=dark] .tpp .am{background:#0f1720}html[data-theme=dark] .tpp .am button{color:#9fb0c0}
html[data-theme=dark] .tpp .g button{background:#1f2c3a;border-color:#33465a;color:#e6edf4}html[data-theme=dark] .tpp h6{color:#9fb0c0}html[data-theme=dark] .tpp .g button:hover{background:#264055!important}
"""
RP_TP_JS = """<script>(function(){var open=null;
function close(){if(open){open.p.remove();if(open.bg)open.bg.remove();open=null}}
function build(tp){var s=tp.querySelectorAll('select');if(s.length<3||tp.dataset.k)return;tp.dataset.k='1';var H=s[0],M=s[1],P=s[2];
 tp.style.display='none';var lab=tp.parentNode.querySelector('label');if(lab)lab.classList.add('tpl');
 var b=document.createElement('button');b.type='button';b.className='tpk';b.setAttribute('aria-haspopup','dialog');
 b.innerHTML='<span class="tv"></span><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';tp.parentNode.insertBefore(b,tp);
 var tv=b.firstChild;function show(){var ok=H.value&&M.value!=='';tv.textContent=ok?H.value+':'+M.value+' '+P.value:'Select time';tv.className='tv'+(ok?'':' ph')}show();
 function fire(){tp.querySelector('select').dispatchEvent(new Event('change'))}
 function openP(){close();var bg=null,p=document.createElement('div');p.className='tpp';p.setAttribute('role','dialog');
  if(window.innerWidth<=700){bg=document.createElement('div');bg.className='tpbg';bg.onclick=close;document.body.appendChild(bg)}
  var h='<div class="am"><button type="button" data-p="AM">AM</button><button type="button" data-p="PM">PM</button></div><h6>Hour</h6><div class="g" data-k="h">';
  for(var i=1;i<=12;i++)h+='<button type="button" data-v="'+i+'">'+i+'</button>';
  h+='</div><h6>Minute</h6><div class="g" data-k="m">';
  [].forEach.call(M.options,function(o){if(o.value!=='')h+='<button type="button" data-v="'+o.value+'">'+o.value+'</button>'});
  h+='</div><div class="ft"><button type="button" class="s" data-a="clear">Clear</button><button type="button" data-a="done">Done</button></div>';p.innerHTML=h;document.body.appendChild(p);
  function mark(){p.querySelectorAll('.am button').forEach(function(x){x.classList.toggle('on',x.dataset.p===P.value)});
   p.querySelectorAll('[data-k=h] button').forEach(function(x){x.classList.toggle('on',x.dataset.v===H.value)});
   p.querySelectorAll('[data-k=m] button').forEach(function(x){x.classList.toggle('on',x.dataset.v===M.value)})}mark();
  p.addEventListener('click',function(e){var t=e.target.closest('button');if(!t)return;e.stopPropagation();
   if(t.dataset.p){P.value=t.dataset.p}
   else if(t.parentNode.dataset.k==='h'){H.value=t.dataset.v;if(M.value==='')M.value='00'}
   else if(t.parentNode.dataset.k==='m'){M.value=t.dataset.v;if(H.value==='')H.value='12'}
   else if(t.dataset.a==='clear'){H.value='';M.value=''}
   else if(t.dataset.a==='done'){fire();show();close();return}
   fire();show();mark();if(t.parentNode.dataset.k==='m'){close()}});
  var r=b.getBoundingClientRect();if(!bg){var top=r.bottom+window.scrollY+6,left=r.left+window.scrollX;left=Math.max(8,Math.min(left,window.scrollX+document.documentElement.clientWidth-300));p.style.top=top+'px';p.style.left=left+'px'}
  open={p:p,bg:bg}}
 b.addEventListener('click',function(e){e.stopPropagation();if(open)close();else openP()});
 M.addEventListener('change',show)}
function go(){document.querySelectorAll('.tp').forEach(build)}
document.addEventListener('click',function(e){if(open&&!open.p.contains(e.target))close()});document.addEventListener('keydown',function(e){if(e.key==='Escape')close()});
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){setTimeout(go,0)});else setTimeout(go,0)})();</script>"""
_b = T["base.html"]
_b = _b.replace("</style>" + RP_THEME_INIT, RP_TP_CSS + "</style>" + RP_THEME_INIT, 1)
_b = _b.replace("</body></html>", RP_TP_JS + "</body></html>", 1); T["base.html"] = _b



# ---- 13. Terms and Conditions page + "I agree" checkboxes (Create Account, Request form) + note on the damage-report form ----------
T["terms.html"] = """{% extends 'base.html' %}{% block body %}<style>.tc h4{margin:18px 0 6px;color:var(--pr)}.tc ol{margin:0;padding-left:22px}.tc li{margin:5px 0}.tc{line-height:1.55}</style>
<div class="card tc" style="max-width:760px;margin:0 auto 18px"><h2>Terms and Conditions</h2>
<p class="mu" style="font-size:14px">General Services Office (GSO) - Southern Luzon State University, Judge Guillermo Eleazar. Please read before using this system.</p>
<h4 id="requests">A. Facility and equipment requests</h4><ol>
<li>Submitting a request does not guarantee approval. The GSO must approve it first.</li>
<li>Give correct dates, times and quantities. A request with wrong or missing information may be declined.</li>
<li>The requester is responsible for all borrowed equipment and for the facility used, from pick-up until it is returned.</li>
<li>Return equipment and facilities on the date and time stated in the request. Late returns may affect your future requests.</li>
<li>If an item is damaged or lost while under your care, you are responsible for repairing or replacing it, following the school's policy.</li>
<li>Use the facility or equipment only for the purpose stated in the request. Do not lend it to other people.</li>
<li>The GSO may cancel or move an approved request for an emergency or an important school activity.</li>
<li>Please attach any valid I.D and contact number of requester for any purpose it may served.</li>
<li>Please file three (3) days before the event.</li></ol>
<h4 id="reports">B. Reporting broken or damaged equipment</h4><ol>
<li>Use the report form only for real damage or hazards. False or prank reports may be blocked.</li>
<li>Give the exact location and a clear description so the GSO can find and fix the problem.</li>
<li>Mark a report as urgent only for safety hazards, such as exposed wires or leaks.</li>
<li>Keep your tracking code. You need it to check your report. A report does not mean the repair will be immediate; it depends on the GSO's schedule and available budget.</li>
<li>Photos must show only school property. Do not upload pictures of other people.</li></ol>
<h4 id="privacy">C. Privacy and accounts</h4><ol>
<li>The system collects your name, student number, contact details and request or report information. The GSO uses them only to process and follow up your request or report.</li>
<li>Your information is not shared outside the school, except when required by an official process.</li>
<li>These terms may be updated. By continuing to use the system, you accept the changes.</li></ol>
<p style="margin-top:20px"><a class="btn s" href="javascript:history.length>1?history.back():location.href='/'">Back</a></p></div>{% endblock %}"""

_i = T["terms.html"].index("<h4 id=\"requests\">"); _j = T["terms.html"].index('<p style="margin-top:20px">')
from markupsafe import Markup as _Mk
app.jinja_env.globals["TERMS_BODY"] = _Mk(T["terms.html"][_i:_j])

RP_TC_MODAL = """<div id="tcBg" class="tcbg" hidden><div class="tcm" role="dialog" aria-modal="true" aria-labelledby="tcT"><h3 id="tcT">Terms and Conditions</h3>
<div id="tcBody" class="tcb tc">{{ TERMS_BODY }}</div><p id="tcHint" class="tch">Scroll down to the end to enable the button.</p>
<div class="tcf"><button type="button" class="s rpsm" id="tcNo">Cancel</button><button type="button" id="tcYes" disabled>I have read and I agree</button></div></div></div>
<style>.tcbg{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10000;display:flex;align-items:center;justify-content:center;padding:12px}.tcbg[hidden]{display:none}
.tcm{background:#fff;color:#1b2a38;border-radius:14px;max-width:640px;width:100%;max-height:90vh;display:flex;flex-direction:column;padding:18px 18px 14px;box-shadow:0 20px 50px rgba(0,0,0,.4)}
.tcm h3{margin:0 0 8px;font:600 21px Georgia,serif;color:#14417b}.tcb{overflow:auto;flex:1;border:1px solid #d5dee7;border-radius:8px;padding:4px 14px 12px;font-size:14.5px;max-height:58vh;-webkit-overflow-scrolling:touch}
.tcb h4{margin:14px 0 6px;color:#14417b}.tcb ol{margin:0;padding-left:20px}.tcb li{margin:5px 0}.tch{margin:8px 0 0;font-size:13px;color:#8a4b00}.tch.ok{color:#1e6b3a}
.tcf{display:flex;justify-content:space-between;gap:10px;margin-top:10px}.tcf button{margin:0!important}.tcf #tcYes:disabled{opacity:.45;cursor:not-allowed}
html[data-theme=dark] .tcm{background:#17212c;color:#e6edf4}html[data-theme=dark] .tcm h3,html[data-theme=dark] .tcb h4{color:#8ab4ff}html[data-theme=dark] .tcb{border-color:#33465a}html[data-theme=dark] .tch{color:#f0b866}html[data-theme=dark] .tch.ok{color:#7ee0a0}</style>
<script>(function(){var cb=document.getElementById('tcAgree');if(!cb)return;var bg=document.getElementById('tcBg'),bd=document.getElementById('tcBody'),yes=document.getElementById('tcYes'),hint=document.getElementById('tcHint'),f=cb.form;
function chk(){if(bd.scrollTop+bd.clientHeight>=bd.scrollHeight-6){yes.disabled=false;hint.textContent='Thank you. You can now agree.';hint.className='tch ok'}}
function show(){bg.hidden=false;document.body.style.overflow='hidden';bd.scrollTop=0;if(!cb.checked){yes.disabled=true;hint.textContent='Scroll down to the end to enable the button.';hint.className='tch'}setTimeout(chk,60)}
function hide(){bg.hidden=true;document.body.style.overflow=''}
bd.addEventListener('scroll',chk);document.getElementById('tcNo').onclick=hide;bg.addEventListener('click',function(e){if(e.target===bg)hide()});
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!bg.hidden)hide()});
yes.onclick=function(){cb.checked=true;hide()};
cb.addEventListener('click',function(e){if(cb.checked){e.preventDefault();cb.checked=false;show()}});
document.querySelectorAll('.tclink').forEach(function(a){a.addEventListener('click',function(e){e.preventDefault();show()})});
if(f)f.addEventListener('submit',function(e){if(!cb.checked){e.preventDefault();show();setTimeout(function(){f.querySelectorAll('button').forEach(function(x){x.disabled=false})},30)}})})();</script>"""

@app.route("/terms")
def terms_page():
    return render_template("terms.html", page_sub="General Services Office - Terms and Conditions")

_AGREE = ('<div class="ck" style="margin-top:12px"><label><input type="checkbox" name="agree" value="1" required> <span>I have read and agree to the '
          '<a href="/terms" target="_blank" rel="noopener">Terms and Conditions</a></span></label></div>')

_AGREE2 = ('<div class="ck" style="margin-top:12px"><label><input type="checkbox" id="tcAgree" name="agree" value="1"> <span>I have read and agree to the '
           '<a href="/terms" class="tclink">Terms and Conditions</a></span></label></div>')

@app.before_request
def rp_need_agree():
    if request.method == "POST" and request.endpoint in ("signup", "new_request", "rp_form") and not request.form.get("agree"):
        flash("Please tick the box to agree to the Terms and Conditions first.", "err")
        return redirect(url_for({"signup": "signup", "rp_form": "rp_form"}.get(request.endpoint, "dashboard")))

_s = T["signup.html"]; assert "<button>Create Account</button>" in _s
T["signup.html"] = _s.replace("<button>Create Account</button>", _AGREE2 + "<button>Create Account</button>", 1)
_s = T["signup.html"]; _k = _s.rindex("{% endblock %}"); T["signup.html"] = _s[:_k] + RP_TC_MODAL + _s[_k:]
_s = T["student.html"]; assert "<button>Submit Request</button></form>" in _s
T["student.html"] = _s.replace("<button>Submit Request</button></form>", _AGREE + "<button>Submit Request</button></form>", 1)
_s = T["rp_form.html"]; assert '<button class="sub">Submit report</button>' in _s
_s = _s.replace('<button class="sub">Submit report</button>', _AGREE2 + '<button class="sub">Submit report</button>', 1)
_k = _s.rindex("{% endblock %}"); T["rp_form.html"] = _s[:_k] + """{% if dup %}<form id="sameForm" method="post" action="{{ url_for('rp_same') }}">{{ m.token() }}<input type="hidden" name="rid" value="{{ dup.id }}"></form><script>document.getElementById('tcAgree').checked=true</script>{% endif %}""" + RP_TC_MODAL + _s[_k:]
T["base.html"] = T["base.html"].replace("'rp_form', 'rp_done', 'rp_track', 'login',", "'rp_form', 'rp_done', 'rp_track', 'terms_page', 'login',", 1)

@app.after_request
def rp_terms_link(resp):
    try:
        if request.endpoint == "login" and resp.status_code == 200 and resp.mimetype == "text/html":
            lk = '<div style="text-align:center;margin:14px 0 24px"><a href="/survey" style="color:#fff;font:600 13px system-ui,sans-serif;text-shadow:0 1px 3px rgba(0,0,0,.9),0 0 8px rgba(0,0,0,.6)">Client Satisfaction Survey</a><span style="color:#fff;margin:0 8px;text-shadow:0 1px 3px rgba(0,0,0,.9)">&middot;</span><a href="/terms" style="color:#fff;font:600 13px system-ui,sans-serif;text-shadow:0 1px 3px rgba(0,0,0,.9),0 0 8px rgba(0,0,0,.6)">Terms and Conditions</a></div>'
            resp.set_data(resp.get_data(as_text=True).replace("</body>", lk + "</body>", 1))
    except Exception:
        pass
    return resp



# ---- 14. Client Satisfaction Measurement (ARTA CSM form, SP-SLSU-8.0F1) - web form + PDF + results for the GSO ---------------------
CSM_CLIENT = [("Citizen", "Citizen"), ("Business", "Business"), ("Government", "Government (Employee or another agency)")]
CSM_SEX = ["Male", "Female"]
CSM_CC1 = {"1": "I know what a CC is, and I saw this office's CC.", "2": "I know what a CC is, but I did NOT see this office's CC.",
           "3": "I learned of the CC only when I saw this office's CC.", "4": "I do not know what a CC is, and I did not see one in this office. (Answer 'N/A' on CC2 and CC3)"}
CSM_CC2 = {"1": "Easy to see", "2": "Somewhat easy to see", "3": "Difficult to see", "4": "Not visible at all", "5": "N/A"}
CSM_CC3 = {"1": "Helped very much", "2": "Somewhat helped", "3": "Did not help", "4": "N/A"}
CSM_SQD = [("sqd0", "SQD0", "I am satisfied with the service that I availed."), ("sqd1", "SQD1", "I spent a reasonable amount of time for my transaction."),
           ("sqd2", "SQD2", "The office followed the transaction's requirements and steps based on the information provided."),
           ("sqd3", "SQD3", "The steps (including payment) I needed to do for my transaction were easy and simple."),
           ("sqd4", "SQD4", "I easily found information about my transaction from the office or its website."),
           ("sqd5", "SQD5", "I paid a reasonable amount of fees for my transactions."),
           ("sqd6", "SQD6", "I feel the office was fair to everyone, or “walang palakasan”, during my transaction."),
           ("sqd7", "SQD7", "I was treated courteously by the staff, and (if asked for help) the staff was helpful."),
           ("sqd8", "SQD8", "I got what I needed from the government office, or (if denied) denial of request was sufficiently explained to me.")]
CSM_LABELS = {1: "Strongly Disagree", 2: "Disagree", 3: "Neither Agree nor Disagree", 4: "Agree", 5: "Strongly Agree", 0: "N/A Not Applicable"}
CSM_NAMES = ["Services you availed"]
def _face(mouth):
    return _Mk('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"/><circle cx="8.8" cy="9.8" r=".7" fill="currentColor"/><circle cx="15.2" cy="9.8" r=".7" fill="currentColor"/><path d="%s"/></svg>' % mouth)
SV_SCALE = [(1, _face("M7.8 17.4c1.2-2.2 2.7-3.3 4.2-3.3s3 1.1 4.2 3.3"), "Strongly Disagree", "#c0392b"), (2, _face("M8.8 16.4c.9-1 2-1.5 3.2-1.5s2.3.5 3.2 1.5"), "Disagree", "#d9822b"),
            (3, _face("M8.8 15.6h6.4"), "Neither", "#b08d1a"), (4, _face("M8.8 14.6c.9 1 2 1.5 3.2 1.5s2.3-.5 3.2-1.5"), "Agree", "#3f9b5a"),
            (5, _face("M7.8 13.6h8.4c0 2.6-1.9 4.6-4.2 4.6s-4.2-2-4.2-4.6z"), "Strongly Agree", "#1e8449")]
CSM_INTRO = ("This Client Satisfaction Measurement (CSM) tracks the customer experience of government offices. Your feedback on your recently concluded transaction "
             "will help this office provide a better service. Personal information shared will be kept confidential and you always have the option to not answer this form.")
CSM_INS1 = ("INSTRUCTIONS: Check mark (✓) your answer to the Citizen's Charter (CC) questions. The Citizen's Charter is an official document that reflects the services "
            "of a government agency/office including its requirements, fees, and processing times among others.")
CSM_COLS = "client_type,sex,age,region,service,cc1,cc2,cc3," + ",".join(k for k, _, _ in CSM_SQD) + ",comment,email"
def csm_no(r):
    return "CSM-%05d" % r["id"]

T["survey.html"] = """{% extends 'rp_base.html' %}{% block rp %}{% import 'macros.html' as m %}
<style>
.csm{max-width:860px;margin:0 auto}.csm .hd{border:1.5px solid currentColor;padding:6px 10px;font-weight:700;font-size:12.5px;line-height:1.4;width:max-content;max-width:100%;margin-left:auto}
.csm .ti{display:flex;align-items:center;gap:10px;margin:12px 0 8px}.csm .ti img{width:46px;height:46px}.csm .ti b{display:block;text-decoration:underline;font-size:15px}.csm .ti span{font-weight:700;font-size:13px}
.csm .row{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;margin:8px 0}.csm .row>em{font-style:italic;min-width:80px}
.csm .ul{border:0;border-bottom:1.5px solid currentColor;border-radius:0;background:transparent;padding:2px 4px;margin:0;width:auto;min-width:0;font-size:14px}
.csm hr{border:0;border-top:1.5px solid currentColor;margin:12px 0;opacity:.8}
.csm .ck{display:inline-flex;align-items:flex-start;gap:7px;cursor:pointer;margin:0!important;font-weight:400!important;font-size:14px;line-height:1.3}
.csm .ck input{position:absolute;opacity:0;width:0;height:0}
.csm .ck i{flex:none;width:19px;height:19px;border:1.6px solid currentColor;display:inline-block;position:relative;background:var(--bg)}
.csm .ck input:checked+i::after{content:"";position:absolute;left:5px;top:0;width:6px;height:12px;border:solid #0e7490;border-width:0 3px 3px 0;transform:rotate(40deg)}
.csm .ck input:focus-visible+i{outline:2px solid #22d3ee}
.csm .q{display:grid;grid-template-columns:44px 1fr;gap:4px 6px;margin:12px 0}.csm .q>b{font-size:14px}
.csm .opts{display:grid;gap:5px 18px}.csm .opts.two{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.csm .ck small{font-size:inherit;opacity:.65;margin-right:3px}
.csm table.csmg{width:100%;border-collapse:collapse;table-layout:fixed;border:1.5px solid currentColor;margin:8px 0}
.csm table.csmg th,.csm table.csmg td{border:1px solid currentColor;padding:2px;text-align:center;vertical-align:middle}
.csm table.csmg th{font-size:10px;font-weight:500;line-height:1.1;vertical-align:top;padding:5px 1px}.csm table.csmg th svg{width:30px;height:30px;stroke:currentColor;fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;display:block;margin:0 auto 3px}
.csm table.csmg td.hh{border-bottom:1px solid currentColor}.csm table.csmg td.t{text-align:left;font-size:12.5px;padding:5px 6px;line-height:1.25}.csm table.csmg td.t b{font-weight:700}
.csm table.csmg td .ck{justify-content:center;width:100%;height:36px;align-items:center}.csm table.csmg td .ck i{width:22px;height:22px}
.csm table.csmg td .ck input:checked+i::after{left:6px;top:0;width:7px;height:14px}
.csm table.csmg td:has(input:checked){background:rgba(14,116,144,.14)}
.csm textarea.ul{width:100%;min-height:70px;border:0;background-image:repeating-linear-gradient(transparent,transparent 27px,currentColor 27px,currentColor 28px);line-height:28px;padding:0 4px;font-size:14px;border-bottom:0}
.csm .r3{gap:8px 28px}.csm .f{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px}
@media(max-width:560px){.csm .r3{flex-direction:column;align-items:stretch;gap:12px}.csm .f{width:100%}.csm .f .ul{flex:1 1 auto;min-width:0;width:auto!important}.csm .row .ul{min-width:0}.csm .row[style],.csm .row{gap:10px 14px}
.csm .row>em{min-width:0}.csm .row .ul[name=region],.csm .row .ul[name=service],.csm .row .ul[name=email]{flex:1 1 100%!important}}
@media(max-width:560px){.csm{padding:16px 14px}.csm .row{gap:2px 12px;margin:12px 0}.csm .row .ul,.csm .f .ul{height:auto!important;min-height:34px;margin:0!important;font-size:16px;padding:4px 4px!important}
.csm .row:has(.ul[name=region]),.csm .row:has(.ul[name=email]){display:block}.csm .row:has(.ul[name=region])>em,.csm .row:has(.ul[name=email])>em{display:block;margin-bottom:2px}
.csm .row .ul[name=region],.csm .row .ul[name=service],.csm .row .ul[name=email]{display:block;width:100%!important;flex:none!important}.csm .row:has(.ul[name=region])>em:nth-of-type(2){margin-top:14px}
.csm .ck{font-size:15px;min-height:34px;align-items:center}.csm .ck i{width:22px;height:22px}.csm .ck input:checked+i::after{left:6px;top:1px}.csm .f .ck{margin-right:12px!important}
.csm .row:has(input[name=client_type]){gap:10px 18px}.csm .row:has(input[name=client_type])>em{flex:1 1 100%}.csm button.sub{width:100%;margin:14px 0 8px}.csm .btn{display:block;text-align:center}.csm textarea.ul{font-size:16px}}
@media(max-width:560px){.csm table.csmg td.t{font-size:11.5px;padding:4px}.csm table.csmg th{font-size:8.5px}.csm table.csmg th svg{width:22px;height:22px}.csm table.csmg td .ck i{width:19px;height:19px}.csm .q{grid-template-columns:36px 1fr}}
</style>
<form class="card csm" method="post" onsubmit="var b=this.querySelector('button.sub');setTimeout(function(){b.disabled=true},0)">{{ m.token() }}
<div class="hd">ANTI-RED TAPE AUTHORITY<br>Client Satisfaction Measurement Form<br>PSA Approved No.: ARTA-2242-3<br>Expires on 31 July 2022</div>
<div class="ti"><img src="{{ url_for('logo', n='1') }}" alt=""><div><b>SOUTHERN LUZON STATE UNIVERSITY</b><span>HELP US SERVE YOU BETTER!</span></div></div>
<p style="font-size:13.5px;margin:6px 0">{{ intro }}</p>
<p style="margin:6px 0"><a class="btn s" href="{{ url_for('survey_blank_pdf') }}">Download blank form (PDF)</a></p>
{% if err %}<div class="msg err">{{ err }}</div>{% endif %}
<div style="position:absolute;left:-9999px" aria-hidden="true"><label>Website<input name="website" tabindex="-1" autocomplete="off"></label></div>
<div class="row"><em>Client type:</em>{% for k, t in clients %}<label class="ck"><input type="radio" name="client_type" value="{{ k }}" {{ 'checked' if v.get('client_type') == k }} required><i></i>{{ t }}</label>{% endfor %}</div>
<div class="row r3"><div class="f"><em>Date:</em><input class="ul" value="{{ today }}" disabled size="10"></div>
<div class="f"><em style="min-width:0">Sex:</em>{% for x in sexes %}<label class="ck"><input type="radio" name="sex" value="{{ x }}" {{ 'checked' if v.get('sex') == x }}><i></i>{{ x }}</label>{% endfor %}</div>
<div class="f"><em style="min-width:0">Age:</em><input class="ul" name="age" type="number" min="1" max="120" inputmode="numeric" value="{{ v.get('age', '') }}" style="width:70px"></div></div>
<div class="row"><em style="min-width:0">Region of residence:</em><input class="ul" name="region" maxlength="80" value="{{ v.get('region', '') }}" style="flex:1;min-width:140px">
<em style="min-width:0">Service Availed:</em><input class="ul" name="service" maxlength="120" value="{{ v.get('service', '') }}" required style="flex:2;min-width:160px"></div>
<hr><p style="font-size:13px;margin:6px 0">{{ ins1 }}</p>
<div class="q"><b>CC1</b><div><p style="margin:0 0 5px">Which of the following best describes your awareness of a CC?</p><div class="opts">{% for k, t in cc1 %}<label class="ck"><input type="radio" name="cc1" value="{{ k }}" {{ 'checked' if v.get('cc1') == k }} required><i></i><span><small>{{ k }}.</small> {{ t }}</span></label>{% endfor %}</div></div></div>
<div class="q"><b>CC2</b><div><p style="margin:0 0 5px">If aware of CC (answered 1-3 in CC1), would you say that the CC of this office was...?</p><div class="opts two">{% for k, t in cc2 %}<label class="ck"><input type="radio" name="cc2" value="{{ k }}" {{ 'checked' if v.get('cc2') == k }}><i></i><span><small>{{ k }}.</small> {{ t }}</span></label>{% endfor %}</div></div></div>
<div class="q"><b>CC3</b><div><p style="margin:0 0 5px">If aware of CC (answered codes 1-3 in CC1), how much did the CC help you in your transaction?</p><div class="opts two">{% for k, t in cc3 %}<label class="ck"><input type="radio" name="cc3" value="{{ k }}" {{ 'checked' if v.get('cc3') == k }}><i></i><span><small>{{ k }}.</small> {{ t }}</span></label>{% endfor %}</div></div></div>
<p style="font-size:13px;margin:10px 0 0"><b>INSTRUCTIONS:</b><br>For SQD 0-8, please put a check mark on the column that the best corresponds to your answer.</p>
<table class="csmg"><colgroup><col style="width:40%"><col span="6" style="width:10%"></colgroup>
<tr><td class="hh"></td>{% for n, face, lab, col in scale %}<th>{{ face }}{{ lab|replace('Neither', 'Neither Agree nor Disagree') }}</th>{% endfor %}<th style="padding-top:12px">N/A<br>Not Applicable</th></tr>
{% for k, code, text in sqd %}<tr><td class="t"><b>{{ code }}.</b> {{ text }}</td>{% for n in [1, 2, 3, 4, 5, 0] %}<td><label class="ck" aria-label="{{ code }} {{ n if n else 'N/A' }}"><input type="radio" name="{{ k }}" value="{{ n }}" {{ 'checked' if v.get(k) == n|string }} required><i></i></label></td>{% endfor %}</tr>{% endfor %}</table>
<p style="font-size:13px;margin:12px 0 0">Suggestions on how we can further improve our services (optional):</p>
<textarea class="ul" name="comment" maxlength="600">{{ v.get('comment', '') }}</textarea>
<div class="row"><em style="min-width:0">Email address (optional):</em><input class="ul" name="email" type="email" maxlength="120" value="{{ v.get('email', '') }}" style="flex:1;min-width:180px"></div>
<p style="text-align:center;font-weight:700;margin:12px 0">THANK YOU!</p>
<button class="sub">Submit</button><a class="btn s" href="{{ url_for('home') }}">Cancel</a>
<p class="mu" style="font-size:11.5px;margin-top:14px"><b>SP-SLSU-8.0F1, rev. 0</b><br>Effective: 08/07/2023</p></form>{% endblock %}"""

T["survey_thanks.html"] = """{% extends 'rp_base.html' %}{% block rp %}<div class="card rp-narrow" style="text-align:center"><h2 style="font-size:24px">THANK YOU! <svg class="ic ok" viewBox="0 0 24 24" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg></h2>
<p>Your feedback was received{% if no %} (Control No. <b>{{ no }}</b>){% endif %}. It helps the General Services Office serve you better.</p>
{% if tok %}<p><a class="btn" href="{{ url_for('survey_pdf', token=tok) }}">Download my answered form (PDF)</a></p>{% endif %}<a class="btn s" href="{{ url_for('home') }}">Back to sign in</a></div>{% endblock %}"""

T["gso_survey.html"] = """{% extends 'rp_base.html' %}{% block rp %}{% import 'rp_macros.html' as rm %}{{ rm.gside(tab, cnt) }}
<div class="card"><h2>Client Satisfaction Measurement - Results</h2>
<p><a class="btn s" href="{{ url_for('survey_blank_pdf') }}">Blank form (PDF)</a> <a class="btn s" href="{{ url_for('survey_page') }}">Open the online form</a></p>
{% if not total %}<p class="mu">No answers yet. The survey is a text link on the sign-in page, or share this address: <b>{{ url_for('survey_page', _external=True) }}</b></p>
{% else %}<div class="stats"><div class="stat"><b>{{ total }}</b><span>Responses</span></div><div class="stat"><b>{{ sat }}%</b><span>Satisfied with the service (SQD0: 4-5)</span></div><div class="stat"><b>{{ avg_all }}</b><span>Average of SQD0-8 (out of 5)</span></div><div class="stat"><b>{{ aware }}%</b><span>Aware of the Citizen's Charter</span></div></div>
<h4>Average per statement (out of 5, N/A not counted)</h4><div class="tb"><table><tr><th>Statement</th><th>Average</th><th></th><th>Agree (4-5)</th></tr>
{% for code, label, a, pct in rows %}<tr><td><b>{{ code }}</b> {{ label }}</td><td><b>{{ a }}</b></td><td><div class="svbar"><i style="width:{{ (a * 20)|round|int }}%"></i></div></td><td>{{ pct }}%</td></tr>{% endfor %}</table></div>
<div class="rp-2"><div><h4>Client type</h4><table>{% for n, c in by_client %}<tr><td>{{ n }}</td><td style="text-align:right">{{ c }}</td></tr>{% endfor %}</table></div>
<div><h4>CC1 - awareness of the CC</h4><table>{% for n, c in by_cc1 %}<tr><td>{{ n }}</td><td style="text-align:right">{{ c }}</td></tr>{% endfor %}</table></div></div>
<h4>Latest responses (PDF = the answered form)</h4><div class="tb"><table><tr><th>No.</th><th>Date</th><th>Client</th><th>Service</th><th>SQD0</th><th>Suggestions</th><th></th></tr>
{% for x in latest %}<tr><td>{{ x.no }}</td><td>{{ x.created_at|dt12 }}</td><td>{{ x.client_type }}</td><td>{{ x.service }}</td><td>{{ x.sqd0 if x.sqd0 else 'N/A' }}</td><td style="white-space:pre-wrap">{{ x.comment }}</td><td><a href="{{ url_for('gso_survey_pdf', rid=x.id) }}">PDF</a></td></tr>{% endfor %}</table></div>
<a class="btn" href="{{ url_for('gso_survey_csv') }}">Download CSV (Excel)</a>{% endif %}</div>{% endblock %}"""

# ---- the PDF: same layout as the ARTA form (A4 portrait). r = None gives the blank form ----
def csm_pdf(r=None):
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4); H = 297 * mm
    def T_(t, x, y, s=9, b=False, a="l", i=False):
        c.setFont("Helvetica-BoldOblique" if b and i else "Helvetica-Bold" if b else "Helvetica-Oblique" if i else "Helvetica", s)
        (c.drawCentredString if a == "c" else c.drawString)(x * mm, H - y * mm, str(t if t is not None else ""))
    def L(x1, y, x2): c.line(x1 * mm, H - y * mm, x2 * mm, H - y * mm)
    def R(x, y, w, h): c.rect(x * mm, H - (y + h) * mm, w * mm, h * mm)
    def fit(t, w, s=9):
        t = str(t or ""); 
        while t and c.stringWidth(t, "Helvetica", s) > w * mm: t = t[:-1]
        return t
    def box(x, y, on):                                  # little check box, y = text baseline
        c.setLineWidth(.6); R(x, y - 3.2, 3.6, 3.6)
        if on:
            c.setLineWidth(1.3); c.line((x + .6) * mm, H - (y - 1.2) * mm, (x + 1.5) * mm, H - (y + .1) * mm); c.line((x + 1.5) * mm, H - (y + .1) * mm, (x + 3.2) * mm, H - (y - 2.8) * mm); c.setLineWidth(.6)
    def para(t, x, y, w, s=9, lead=4.2, b=False):
        f = "Helvetica-Bold" if b else "Helvetica"
        for ln in simpleSplit(t, f, s, w * mm):
            T_(ln, x, y, s, b); y += lead
        return y
    d = lambda k: (r[k] if r is not None else None)
    c.setLineWidth(.8); R(121, 7, 76, 15)
    for i, (t, bold) in enumerate((("ANTI-RED TAPE AUTHORITY", 1), ("Client Satisfaction Measurement Form", 1), ("PSA Approved No.: ARTA-2242-3", 1), ("Expires on 31 July 2022", 1))): T_(t, 124, 11.5 + i * 3.4, 8, True)
    c.setLineWidth(.6)
    T_("Control No:", 15, 31, 9); L(33, 31.8, 55)
    if r is not None: T_(csm_no(r), 34, 30.6, 9, True)
    c.drawImage(ImageReader(io.BytesIO(base64.b64decode(LOGOS["1"]))), 62 * mm, H - 37 * mm, 10 * mm, 10 * mm, mask="auto")
    T_("SOUTHERN LUZON STATE UNIVERSITY", 75, 30, 10, True); L(75, 31, 75 + c.stringWidth("SOUTHERN LUZON STATE UNIVERSITY", "Helvetica-Bold", 10) / mm)
    T_("HELP US SERVE YOU BETTER!", 90, 34.5, 9, True)
    y = para(CSM_INTRO, 15, 43, 182, 8.5, 3.8)
    y += 1.5; T_("Client type:", 15, y, 9, False, "l", True)
    for x, (k, t) in zip((36, 58, 80), CSM_CLIENT): box(x, y, d("client_type") == k); T_(t, x + 5, y, 9)
    y += 5.5; T_("Date:", 15, y); L(25, y + 1, 62); T_(fit(dt12(d("created_at")).split(",")[0] if r is not None else "", 36), 26, y - .6)
    T_("Sex:", 78, y, 9, False, "l", True)
    for x, t in zip((88, 106), CSM_SEX): box(x, y, d("sex") == t); T_(t, x + 5, y, 9)
    T_("Age:", 130, y, 9, False, "l", True); L(139, y + 1, 160); T_(fit(d("age"), 20), 140, y - .6)
    y += 5; T_("Region of residence:", 15, y); L(47, y + 1, 94); T_(fit(d("region"), 46), 48, y - .6)
    T_("Service Availed:", 99, y); L(122, y + 1, 197); T_(fit(d("service"), 74), 123, y - .6)
    y += 5; c.setLineWidth(.9); L(14, y, 198); c.setLineWidth(.6)
    y = para(CSM_INS1.replace(' (✓)', ''), 15, y + 4.5, 182, 8.5, 3.8)
    # CC1
    y += 1.5; T_("CC1", 15, y, 9); T_("Which of the following best describes your awareness of a CC?", 33, y, 9); y += 4
    for k, t in CSM_CC1.items():
        box(31, y, str(d("cc1")) == k); T_(k + ".", 37, y, 8.5); T_(t, 46, y, 8.5); y += 3.7
    y += 1.5; T_("CC2", 15, y, 9); T_("If aware of CC (answered 1-3 in CC1), would you say that the CC of this office was...?", 33, y, 9); y += 4
    for k, t in CSM_CC2.items():
        col, row = (0, int(k) - 1) if int(k) <= 3 else (1, int(k) - 4)
        box(31 + col * 86, y + row * 4.3, str(d("cc2")) == k); T_(k + ".", 37 + col * 86, y + row * 4.3, 8.5); T_(t, 46 + col * 86, y + row * 4.3, 8.5)
    y += 3 * 3.7 + 1.5; T_("CC3", 15, y, 9); T_("If aware of CC (answered codes 1-3 in CC1), how much did the CC help you in your transaction?", 33, y, 9); y += 4
    for k, t in CSM_CC3.items():
        col, row = (0, int(k) - 1) if int(k) <= 2 else (1, int(k) - 3)
        box(31 + col * 86, y + row * 4.3, str(d("cc3")) == k); T_(k + ".", 37 + col * 86, y + row * 4.3, 8.5); T_(t, 46 + col * 86, y + row * 4.3, 8.5)
    y += 2 * 3.7 + 2; T_("INSTRUCTIONS:", 15, y, 8.5); y += 3.7
    T_("For SQD 0-8, please put a check mark on the column that the best corresponds to your answer.", 15, y, 8.5)
    # grid
    x0, qw, cw, hh, rh = 14, 72, 19, 22, 10.0; ty = y + 2.5
    xs = [x0, x0 + qw] + [x0 + qw + cw * i for i in range(1, 7)]
    c.setLineWidth(.7); R(x0, ty, qw + cw * 6, hh + rh * 9)
    for xx in xs[1:-1]: c.line(xx * mm, H - ty * mm, xx * mm, H - (ty + hh + rh * 9) * mm)
    for i in range(10): L(x0, ty + hh + rh * i, x0 + qw + cw * 6)
    def face(cx, cy, n):
        c.setLineWidth(.8); c.setFillColor(colors.white); c.circle(cx * mm, H - cy * mm, 5.6 * mm, stroke=1, fill=0); c.setFillColor(colors.black)
        for ex in (-2, 2): c.circle((cx + ex) * mm, H - (cy - 1.4) * mm, .55 * mm, stroke=0, fill=1)
        p = c.beginPath(); m = lambda dx, dy: ((cx + dx) * mm, H - (cy + dy) * mm)
        if n == 1:
            p.moveTo(*m(-2.4, 3.4)); p.curveTo(*m(-1, 1.4), *m(1, 1.4), *m(2.4, 3.4))
            c.setLineWidth(.6); c.line(*m(-3.2, -2.8), *m(-1.2, -3.6)); c.line(*m(3.2, -2.8), *m(1.2, -3.6))
        elif n == 2: p.moveTo(*m(-2.2, 3)); p.curveTo(*m(-1, 1.8), *m(1, 1.8), *m(2.2, 3))
        elif n == 3: p.moveTo(*m(-2.2, 2.4)); p.lineTo(*m(2.2, 2.4))
        elif n == 4: p.moveTo(*m(-2.4, 1.6)); p.curveTo(*m(-1, 3.4), *m(1, 3.4), *m(2.4, 1.6))
        else: p.moveTo(*m(-2.8, 1.2)); p.lineTo(*m(2.8, 1.2)); p.curveTo(*m(2.4, 4.4), *m(-2.4, 4.4), *m(-2.8, 1.2))
        c.setLineWidth(.8); c.drawPath(p, stroke=1, fill=1 if n == 5 else 0)
    for n in range(1, 6):
        cx = xs[n + 1] - cw / 2 + 0; cx = x0 + qw + cw * (n - 1) + cw / 2
        face(cx, ty + 6.6, n)
        for j, ln in enumerate(CSM_LABELS[n].replace("Neither Agree nor Disagree", "Neither Agree|nor Disagree").replace("Strongly ", "Strongly|").split("|")): T_(ln, cx, ty + 15.2 + j * 3.4, 8, False, "c")
    cx = x0 + qw + cw * 5 + cw / 2
    for j, ln in enumerate(("N/A", "Not", "Applicable")): T_(ln, cx, ty + 9.5 + j * 3.4, 8, False, "c")
    for i, (k, code, text) in enumerate(CSM_SQD):
        yy = ty + hh + rh * i; ls = simpleSplit(code + ". " + text, "Helvetica", 8, (qw - 3) * mm)
        for j, ln in enumerate(ls[:3]): T_(ln, x0 + 1.5, yy + 3.5 + j * 2.95, 8)
        v = d(k)
        if r is not None and v is not None:
            col = 5 if v == 0 else v - 1; cxx = x0 + qw + cw * col + cw / 2
            c.setLineWidth(1.6); c.line((cxx - 2.6) * mm, H - (yy + rh / 2) * mm, (cxx - .8) * mm, H - (yy + rh / 2 + 2.2) * mm); c.line((cxx - .8) * mm, H - (yy + rh / 2 + 2.2) * mm, (cxx + 3) * mm, H - (yy + rh / 2 - 3) * mm); c.setLineWidth(.6)
    y = ty + hh + rh * 9 + 6
    T_("Suggestions on how we can further improve our services (optional):", 15, y, 8.5)
    cm = simpleSplit((d("comment") or "").replace("\n", " "), "Helvetica", 9, 180 * mm) if r is not None else []
    for j in range(2):
        L(14, y + 6 + j * 5.5, 198)
        if j < len(cm): T_(cm[j] if j < 1 or len(cm) <= 2 else cm[1][:-3] + "...", 15, y + 4.9 + j * 5.5, 9)
    y += 16; T_("Email address (optional):", 15, y, 8.5); L(48, y + 1, 110); T_(fit(d("email"), 60), 49, y - .6)
    T_("THANK YOU!", 105, y + 7, 10, True, "c")
    T_("SP-SLSU-8.0F1, rev. 0", 15, 286, 8, True); T_("Effective: 08/07/2023", 15, 290, 8.5, True)
    c.showPage(); c.save(); return buf.getvalue()

def _csm_resp(data, name):
    return Response(data, mimetype="application/pdf", headers={"Content-Disposition": "inline; filename=%s" % name})

@app.route("/survey", methods=["GET", "POST"])
def survey_page():
    v, err = {}, ""
    if request.method == "POST":
        v = {k: request.form.get(k, "").strip() for k in ["website", "client_type", "sex", "age", "region", "service", "cc1", "cc2", "cc3", "comment", "email"] + [k for k, _, _ in CSM_SQD]}
        if v["website"]: return redirect(url_for("home"))
        if v["cc1"] == "4": v["cc2"], v["cc3"] = "5", "4"
        if (v["client_type"] not in [k for k, _ in CSM_CLIENT] or not v["service"] or v["cc1"] not in CSM_CC1
                or v["cc2"] not in CSM_CC2 or v["cc3"] not in CSM_CC3 or any(v[k] not in ("0", "1", "2", "3", "4", "5") for k, _, _ in CSM_SQD)):
            err = "Please answer the client type, service availed, CC1 to CC3 and every SQD statement before submitting."
        elif v["sex"] and v["sex"] not in CSM_SEX: err = "Please choose Male or Female, or leave it blank."
        elif v["age"] and not (v["age"].isdigit() and 1 <= int(v["age"]) <= 120): err = "Please enter a valid age, or leave it blank."
        elif v["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v["email"]): err = "That email address does not look right. You can leave it blank."
        elif rp_limited("survey", 5, 3600): err = "Too many answers were sent from this network. Please try again later."
        else:
            tok = secrets.token_urlsafe(16)
            ex("INSERT INTO survey_csm(token,created_at,client_type,sex,age,region,service,cc1,cc2,cc3,sqd0,sqd1,sqd2,sqd3,sqd4,sqd5,sqd6,sqd7,sqd8,comment,email) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               tok, now_s(), v["client_type"], v["sex"], int(v["age"]) if v["age"] else None, v["region"][:80], v["service"][:120], int(v["cc1"]), int(v["cc2"]), int(v["cc3"]),
               *[int(v[k]) for k, _, _ in CSM_SQD], v["comment"][:600], v["email"][:120])
            session["csm_tok"] = tok
            return redirect(url_for("survey_thanks"))
    return render_template("survey.html", v=v, err=err, intro=CSM_INTRO, ins1=CSM_INS1, clients=CSM_CLIENT, sexes=CSM_SEX, today=now_s()[:10], sqd=CSM_SQD, scale=SV_SCALE,
                           cc1=list(CSM_CC1.items()), cc2=list(CSM_CC2.items()), cc3=list(CSM_CC3.items()), page_sub="General Services Office - Client Satisfaction Measurement")

@app.route("/survey/thanks")
def survey_thanks():
    tok = session.get("csm_tok"); r = q1("SELECT * FROM survey_csm WHERE token=?", tok) if tok else None
    return render_template("survey_thanks.html", tok=tok if r else None, no=csm_no(r) if r else "", page_sub="General Services Office - Client Satisfaction Measurement")

@app.route("/survey/blank.pdf")
def survey_blank_pdf():
    return _csm_resp(csm_pdf(None), "CSM_form_blank.pdf")

@app.route("/survey/pdf/<token>")
def survey_pdf(token):
    r = q1("SELECT * FROM survey_csm WHERE token=?", token[:64])
    if not r: abort(404)
    return _csm_resp(csm_pdf(r), "%s.pdf" % csm_no(r))

@app.route("/gso/survey/<int:rid>.pdf")
@admin_required
def gso_survey_pdf(rid):
    r = q1("SELECT * FROM survey_csm WHERE id=?", rid)
    if not r: abort(404)
    return _csm_resp(csm_pdf(r), "%s.pdf" % csm_no(r))

@app.route("/gso/survey")
@admin_required
def gso_survey():
    data = q("SELECT * FROM survey_csm ORDER BY id DESC"); n = len(data); ctx = dict(tab="survey", cnt=rp_counts(), total=n, page_sub="General Services Office - Client Satisfaction Measurement")
    if n:
        def stat(k):
            vals = [r[k] for r in data if r[k]]
            return (round(sum(vals) / len(vals), 2) if vals else 0), (round(100 * sum(1 for x in vals if x >= 4) / len(vals)) if vals else 0)
        rows = [(code, text, *stat(k)) for k, code, text in CSM_SQD]
        ctx.update(rows=rows, sat=stat("sqd0")[1], avg_all=round(sum(x[2] for x in rows) / len(rows), 2), aware=round(100 * sum(1 for r in data if r["cc1"] in (1, 2, 3)) / n),
                   by_client=[(k, sum(1 for r in data if r["client_type"] == k)) for k, _ in CSM_CLIENT],
                   by_cc1=[("%s. %s" % (k, t.split(" (")[0]), sum(1 for r in data if str(r["cc1"]) == k)) for k, t in CSM_CC1.items()],
                   latest=[dict(r, no=csm_no(r)) for r in data[:40]])
    return render_template("gso_survey.html", **ctx)

@app.route("/gso/survey.csv")
@admin_required
def gso_survey_csv():
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["Control No", "Date"] + CSM_COLS.split(",")[:-2] + ["suggestions", "email"])
    for r in q("SELECT * FROM survey_csm ORDER BY id"):
        w.writerow([rp_csv(x) for x in [csm_no(r), r["created_at"]] + [("N/A" if k.startswith("sqd") and r[k] == 0 else r[k]) for k in CSM_COLS.split(",")[:-2]] + [r["comment"], r["email"]]])
    return Response("﻿" + out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=client_satisfaction_measurement.csv"})

T["base.html"] = T["base.html"].replace("'terms_page',", "'terms_page', 'survey_page', 'survey_thanks',", 1)



# ---- 15. The request form looks like the printed R.F.U. form (style only: the fields, names and scripts are untouched) ----
RFU_PAPER = """
<style>
form.rfu{max-width:860px;margin-left:auto;margin-right:auto;background:var(--card);border:1.5px solid currentColor;border-radius:4px}
form.rfu>h2{text-align:center;font:700 17px/1.3 Georgia,serif;letter-spacing:.3px;margin:6px 0 14px;text-transform:uppercase}
.rfu-h{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 8px}.rfu-h img{width:clamp(46px,11vw,76px);height:auto;flex:none}
.rfu-h div{text-align:center;line-height:1.25;min-width:0}.rfu-h b{display:block;font:700 clamp(14px,3.6vw,20px)/1.2 Georgia,serif}.rfu-h i{display:block;font-style:normal;font-size:clamp(11px,2.8vw,13px)}
.rfu-no{width:max-content;margin:0 0 12px auto;border:1.5px solid currentColor;padding:4px 10px;font-size:12.5px}.rfu-no span{display:inline-block;min-width:80px;border-bottom:1px solid currentColor;margin-left:6px;font-size:11px;opacity:.7}
html body form.rfu .g{display:grid;grid-template-columns:repeat(12,1fr);gap:12px 18px}
html body form.rfu .g>div{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 8px;grid-column:span 12}
html body form.rfu .g>div:nth-child(1){grid-column:span 8;order:1}html body form.rfu .g>div:nth-child(2){grid-column:span 4;order:2}
html body form.rfu .g>div:nth-child(3){order:3}html body form.rfu .g>div:nth-child(4){order:4}
html body form.rfu .g>div:nth-child(5){grid-column:span 7;order:5}html body form.rfu .g>div:nth-child(9){grid-column:span 5;order:6}
html body form.rfu .g>div:nth-child(7){grid-column:span 6;order:7}html body form.rfu .g>div:nth-child(8){grid-column:span 6;order:8}
html body form.rfu .g>div:nth-child(6){grid-column:span 7;order:9}
html body form.rfu label.fl,html body form.rfu label{position:static!important;transform:none!important;height:auto!important;width:auto!important;max-width:none!important;margin:0!important;padding:0;overflow:visible!important;
 white-space:nowrap;font-size:14px!important;font-weight:500!important;line-height:1.3;color:inherit!important;pointer-events:auto;text-shadow:none!important;display:inline-block}
html body form.rfu label.fl.fl.fl{color:inherit!important;font-size:14px!important;transform:none!important}
html body form.rfu .g>div>label+input,html body form.rfu .g>div>label+.tp,html body form.rfu>input,html body form.rfu .erow input,html body form.rfu .erow select,html body form.rfu .tp select{
 margin:0!important;height:auto!important;min-height:26px;padding:2px 4px!important;border:0!important;border-bottom:1.5px solid currentColor!important;border-radius:0!important;background:transparent!important;box-shadow:none!important;font-size:14px;color:inherit}
html body form.rfu .g>div>input:not([type=hidden]),html body form.rfu .g>div>.tp{flex:1 1 90px;min-width:0;width:auto}
html body form.rfu .g>div>input:focus,html body form.rfu>input:focus,html body form.rfu .erow input:focus,html body form.rfu select:focus{border-bottom-color:#0e7490!important;background:rgba(14,116,144,.07)!important}
html body form.rfu .g>div>input:disabled{opacity:1;-webkit-text-fill-color:currentColor}
html body form.rfu .g>div>.tpk{flex:1 1 110px;width:auto;height:auto!important;min-height:26px;margin:0!important;padding:2px 4px!important;background:transparent!important;border:0!important;border-bottom:1.5px solid currentColor!important;border-radius:0!important;box-shadow:none!important;font-size:14px}
html body form.rfu .g>div>.tpk .tv{font-weight:400}html body form.rfu .g>div>.tpk svg{width:17px;height:17px}
html body form.rfu>label,html body form.rfu>label.fl{display:block!important}
html body form.rfu .fe{flex-basis:100%}html body form.rfu .tp{display:flex;gap:4px}
form.rfu>label:not(.fl):first-of-type{margin-top:0}
html body form.rfu>label{display:block;margin:16px 0 6px!important;font-weight:700!important}
html body form.rfu .ck{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;margin:2px 0 8px 14px}
html body form.rfu .ck label{display:flex;align-items:center;gap:8px;margin:0!important;font-weight:400!important;white-space:normal;cursor:pointer}
html body form.rfu .ck input[type=checkbox]{appearance:none;-webkit-appearance:none;flex:none;width:20px;height:20px;margin:0!important;border:1.6px solid currentColor;border-radius:2px;background:var(--card);position:relative;cursor:pointer}
html body form.rfu .ck input[type=checkbox]:checked::after{content:"X";position:absolute;inset:0;display:grid;place-items:center;font:700 15px/1 Arial,sans-serif;color:#0e7490}
html body form.rfu .ck input[type=checkbox]:disabled{opacity:.45;cursor:not-allowed}
html body form.rfu>input[name=fac_other]{width:calc(100% - 14px);margin-left:14px!important}
html body form.rfu #eq{margin:2px 0 8px 14px}html body form.rfu .erow{gap:10px}
html body form.rfu button.s{margin-left:14px}
.rfu-eq{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 26px;margin:2px 0 6px 14px}.rfu-eq label{margin:0!important;font-weight:400!important}
html body form.rfu .rfu-sd{display:inline-flex!important;align-items:center;gap:8px;cursor:pointer}
html body form.rfu .rfu-sd input{position:absolute;opacity:0;width:0;height:0}.rfu-sd i{flex:none;width:20px;height:20px;border:1.6px solid currentColor;border-radius:2px;background:var(--card);position:relative}
.rfu-sd input:checked+i::after{content:"X";position:absolute;inset:0;display:grid;place-items:center;font:700 15px/1 Arial,sans-serif;color:#0e7490}.rfu-sd input:focus-visible+i{outline:2px solid #22d3ee}.rfu-sd input:disabled+i{opacity:.45}
html body form.rfu .rfu-n input{width:84px;text-align:center;font-weight:700;margin:0!important;padding:2px 4px!important;border:0!important;border-bottom:1.5px solid currentColor!important;border-radius:0!important;background:transparent!important;box-shadow:none!important;height:auto!important;color:inherit}
.rfu-eq em{font-style:normal;font-size:11.5px;opacity:.75;white-space:nowrap}.rfu-ot{margin:8px 0 2px 14px;font-weight:500}
.rfu-f{margin:16px 0 0;font-size:11.5px;opacity:.75}
@media(max-width:640px){html body form.rfu .g{grid-template-columns:1fr}html body form.rfu .g>div:nth-child(n){grid-column:auto}html body form.rfu .ck{grid-template-columns:1fr}
 html body form.rfu>label,html body form.rfu>label.fl{white-space:normal!important}html body form.rfu button.s{margin-left:14px;max-width:calc(100% - 14px)}html body form.rfu .erow{flex-wrap:wrap}html body form.rfu .erow select,html body form.rfu .erow input{flex:1 1 100%;min-width:0}html body form.rfu .erow input[name=item_qty]{flex:1 1 60%}html body form.rfu .erow button.r{flex:none;min-width:40px;padding:6px 12px}}
</style>
<script>(function(){var f=document.querySelector('form.card[action$="/request"]');if(!f||f.classList.contains('rfu'))return;f.classList.add('rfu');
var L={requester:"Requester's Name:",dept:'College/Department:',event:'Name/Type of Event:',event_date:'Date of the Event:',return_date:'Date to be Returned:',t1:'Time Reserved:',t2:'to',attendees:'Estimated Attendees:'};
f.querySelectorAll('.g>div').forEach(function(d,i){var lb=d.querySelector('label'),inp=d.querySelector('[name]');if(!lb)return;var req=/\\*/.test(lb.textContent);
 var t=inp&&L[inp.name]||(i==1?'Date:':null);if(t){lb.textContent=t;if(req){var s=document.createElement('span');s.textContent=' *';s.style.color='#c0392b';lb.appendChild(s)}}});
var h2=f.querySelector('h2');if(h2){h2.textContent='Request for Facility/Equipment Use';var hd=document.createElement('div');hd.className='rfu-h';
 hd.innerHTML='<img src="/logo/1.png" alt=""><div><b>Southern Luzon State University</b><b style="font-size:clamp(12px,3vw,16px)">Judge Guillermo Eleazar</b><i>GENERAL SERVICES OFFICE</i><i>Tagkawayan, Quezon</i></div><img src="/logo/2.png" alt="">';
 h2.parentNode.insertBefore(hd,h2);var no=document.createElement('div');no.className='rfu-no';no.innerHTML='R.F.U. No:<span>(given after you submit)</span>';h2.parentNode.insertBefore(no,h2)}
var lbs=f.querySelectorAll(':scope>label');lbs.forEach(function(l){var x=l.textContent.trim();if(x=='Facility to be used')l.textContent='Facility to be used:';if(x=='Equipment')l.innerHTML='<i>Equipment</i>';
 if(/^Requested by/.test(x))l.textContent='Requested by (College Department/Department Head) - optional:'});
var fo=f.querySelector('[name=fac_other]');if(fo)fo.placeholder='others (specify)';
var ft=document.createElement('p');ft.className='rfu-f';ft.innerHTML='<b>AFA-GSO-1.01F8, Rev. 0</b><br>Effective 11/08/24';f.appendChild(ft);
/* equipment line like the paper: [ ] Sound System   Table ___   Chair ___   Other: (anything else the GSO added to the list) */
var eq=f.querySelector('#eq');if(!eq)return;var DEF=['Sound System','Table','Chair'],HID={};
var bx=document.createElement('div');bx.className='rfu-eq';
bx.innerHTML='<label class="rfu-sd"><input type="checkbox" id="rq0"><i></i><span>Sound System</span><em></em></label><label class="rfu-n">Table <input type="number" min="0" id="rq1" inputmode="numeric"><em></em></label><label class="rfu-n">Chair <input type="number" min="0" id="rq2" inputmode="numeric"><em></em></label>';
var pm=eq.previousElementSibling;eq.parentNode.insertBefore(bx,eq);
var ot=document.createElement('div');ot.className='rfu-ot';ot.textContent='Other:';eq.parentNode.insertBefore(ot,eq);
DEF.forEach(function(n,i){var h=document.createElement('span');h.hidden=true;h.innerHTML='<input type="hidden" name="item_name" value="'+n+'"><input type="hidden" name="item_qty"><input type="hidden" name="item_other" value="">';bx.appendChild(h);HID[n]=h});
function ctl(i){return document.getElementById('rq'+i)}
function sync(){DEF.forEach(function(n,i){var c=ctl(i),q=i==0?(c.checked?1:0):(parseInt(c.value,10)||0),h=HID[n].querySelectorAll('input');h[1].value=q;h.forEach(function(x){x.disabled=q<=0})})}
bx.addEventListener('input',sync);bx.addEventListener('change',sync);f.addEventListener('submit',sync,true);sync();
var addb=[].find.call(f.querySelectorAll('button.s'),function(b){return /Add equipment/.test(b.textContent)});if(addb)addb.textContent='+ Add other equipment';
var first=true,old=(typeof HAS_ERR!=='undefined'&&HAS_ERR&&typeof OLD_ITEMS!=='undefined'&&OLD_ITEMS.length);
new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach(function(r){if(!r.classList||!r.classList.contains('erow'))return;var sel=r.querySelector('select'),qt=r.querySelector('[name=item_qty]'),k=DEF.indexOf(sel.value);
 if(k>=0&&old){var c=ctl(k);if(k==0)c.checked=true;else c.value=(parseInt(c.value,10)||0)+(parseInt(qt.value,10)||0);sync();r.remove();return}
 if(first&&!old){first=false;r.remove();return}first=false;
 [].slice.call(sel.options).forEach(function(o){if(DEF.indexOf(o.value)>=0)o.remove()});
 if(sel.selectedIndex<0&&sel.options.length)sel.selectedIndex=0;if(typeof paint==='function')paint()})})}).observe(eq,{childList:true});
/* show how many are left for the chosen date/time, and block more than that */
setInterval(function(){if(typeof AV==='undefined'||!AV.left)return;DEF.forEach(function(n,i){var l=AV.left[n],c=ctl(i),em=c.parentNode.querySelector('em');if(l===undefined){em.textContent='';return}
 em.textContent=l>0?' ('+l+' left)':' - OUT OF STOCK';if(i==0){c.disabled=!(l>0);if(!(l>0)&&c.checked){c.checked=false;sync()}}else{c.max=l;c.disabled=!(l>0);if(+c.value>l){c.value=l>0?l:'';sync()}}})},600)})();</script>
"""
T["student.html"] = T["student.html"].replace("{% elif tab == 'mine' %}", RFU_PAPER + "{% elif tab == 'mine' %}", 1)

RP_SCREEN_CSS = """
/* dashboards use the whole screen: the background covers every screen size, the content grows with it (the menu tab keeps 60px free on each side) */
html,body{min-height:100%}body.authbg::before,body.dash::before{background-size:cover;background-position:center}
@media(min-width:701px){.dash .wrap{max-width:min(1240px,calc(100vw - 120px))}}
@media(min-width:1400px){.dash .wrap{max-width:min(1480px,calc(100vw - 160px))}.dash .card{padding:26px 28px}.stats{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}}
@media(min-width:1900px){.dash .wrap{max-width:1680px}.dash .card{font-size:16px}.dash .card h2{font-size:27px}}
@media(min-width:1100px){.dash .rp-2{grid-template-columns:1.5fr 1fr}}
"""
_b = T["base.html"]
_b = _b.replace("</style>" + RP_THEME_INIT, RP_SCREEN_CSS + "</style>" + RP_THEME_INIT, 1)
T["base.html"] = _b


# ---- 3. Backup & export of all data (admin) -----------------------------------------------------------------------
RP_BK = [("requests", "id,rfu_no,user_id,requester,dept,event,event_date,t1,t2,return_date,attendees,facilities,head,status,remarks,created_at,returned_at", "requests"),
         ("request_items", "id,request_id,name,qty", "items"),
         ("equipment_stock", "name,stock", "inventory"),
         ("users", "id,username,fullname,student_no,role,created_at", "users"),
         ("notifications", "id,user_id,request_id,kind,message,created_at,read_at", "notifications"),
         ("damage_reports", "id,ticket,token,reporter,contact,dept,item,location,problem,urgent,description,photo_type,status,created_at,updated_at,resolved_at", "damage_reports"),
         ("damage_updates", "id,report_id,status,note,public,by_name,created_at", "damage_updates"),
         ("survey_responses", "id,created_at,client_type,service,s1,s2,s3,s4,s5,s6,s7,recommend,comment", "survey_responses"),
         ("survey_csm", "id,created_at,client_type,sex,age,region,service,cc1,cc2,cc3,sqd0,sqd1,sqd2,sqd3,sqd4,sqd5,sqd6,sqd7,sqd8,comment,email", "survey_csm")]
RP_BK_LABELS = {"requests": "Facility / equipment requests", "request_items": "Equipment in each request", "equipment_stock": "Equipment stock list",
                "users": "Users (no passwords)", "notifications": "Notifications", "damage_reports": "Damage reports", "damage_updates": "Damage report history", "survey_responses": "Client satisfaction survey answers (old format)", "survey_csm": "Client Satisfaction Measurement (ARTA CSM) answers"}

def rp_bk_csv(name):
    spec = next((x for x in RP_BK if x[0] == name), None)
    if not spec: return None
    cols = spec[1].split(",")
    out = io.StringIO(); w = csv.writer(out); w.writerow(cols)
    for r in q("SELECT %s FROM %s ORDER BY %s" % (spec[1], spec[2], cols[0])): w.writerow([rp_csv(r[k]) for k in cols])
    return "﻿" + out.getvalue()

@app.route("/gso/backup")
@admin_required
def gso_backup():
    info = [(n, RP_BK_LABELS[n], q1("SELECT COUNT(*) FROM %s" % t)[0]) for n, _, t in RP_BK]
    return render_template("gso_backup.html", info=info, tab="backup", cnt=rp_counts(), page_sub="General Services Office - Backup & Export")

@app.route("/gso/backup/<name>.csv")
@admin_required
def gso_backup_csv(name):
    data = rp_bk_csv(name)
    if data is None: abort(404)
    return Response(data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=%s.csv" % name})

@app.route("/gso/backup/full.zip")
@admin_required
def gso_backup_zip():
    buf = io.BytesIO(); stamp = now_dt().strftime("%Y%m%d-%H%M")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, _, _t in RP_BK: z.writestr(n + ".csv", rp_bk_csv(n))
        for r in q("SELECT id,ticket,photo_type FROM damage_reports WHERE photo IS NOT NULL ORDER BY id"):
            p = q1("SELECT photo FROM damage_reports WHERE id=?", r["id"])
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(r["photo_type"], "bin")
            z.writestr("photos/%s.%s" % (r["ticket"], ext), base64.b64decode(p["photo"]))
        z.writestr("README.txt", "SLSU-JGE GSO - data backup created %s (Philippine time).\r\n\r\nEach .csv file is one table; open them in Excel.\r\n"
                   "photos/ holds the pictures attached to damage reports (named by ticket number).\r\nPasswords are NOT included.\r\n"
                   "Keep this file private: it contains names, student numbers and contact details.\r\n" % now_s())
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="slsu-gso-backup-%s.zip" % stamp)

# ---- 4. Late-return reminders ---------------------------------------------------------------------------------------
_RP_DUE = "(COALESCE(r.return_date,r.event_date)||' '||r.t2)"
_RP_SEL = ("SELECT r.id,r.rfu_no,r.user_id,r.requester,r.dept,r.event,r.event_date,r.return_date,r.t2 FROM requests r WHERE r.status='Approved' AND r.returned_at IS NULL "
           "AND EXISTS(SELECT 1 FROM items i WHERE i.request_id=r.id) AND ")
def _rp_items(rid): return ", ".join("%s x%d" % (i["name"], i["qty"]) for i in q("SELECT name,qty FROM items WHERE request_id=?", rid))
def rp_overdue(): return q(_RP_SEL + _RP_DUE + " < ? ORDER BY " + _RP_DUE, now_s())
def rp_due_soon(): return q(_RP_SEL + _RP_DUE + " >= ? AND " + _RP_DUE + " <= ? ORDER BY " + _RP_DUE, now_s(), (now_dt() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))
def _rp_when(r): return dfmt(r["return_date"] or r["event_date"]) + " " + t12(r["t2"])

RP_SWEEP = [0.0]
@app.before_request
def rp_sweep():
    """Once a minute (when someone opens a dashboard) send each borrower ONE 'due soon' and ONE 'overdue' notification (their bell)."""
    try:
        if request.endpoint != "dashboard" or not g.get("user") or time.time() - RP_SWEEP[0] < 60: return
        RP_SWEEP[0] = time.time()
        for kind, rows in (("overdue", rp_overdue()), ("duesoon", rp_due_soon())):
            for r in rows:
                if q1("SELECT 1 FROM notifications WHERE request_id=? AND kind=?", r["id"], kind): continue
                if kind == "overdue":
                    msg = "REMINDER: Request %s - the borrowed equipment (%s) was due back on %s. Please return it to the GSO as soon as possible." % (r["rfu_no"], _rp_items(r["id"]), _rp_when(r))
                else:
                    msg = "Reminder: please return the borrowed equipment (%s) of request %s to the GSO by %s." % (_rp_items(r["id"]), r["rfu_no"], _rp_when(r))
                notify(r["user_id"], r["id"], kind, msg)
    except Exception as err:
        print("rp_sweep:", err)

@app.after_request
def rp_overdue_banner(resp):
    try:
        if request.endpoint != "dashboard" or not g.get("user") or resp.status_code != 200 or resp.mimetype != "text/html": return resp
        admin, tab = g.user["role"] == "admin", request.args.get("tab", "")
        if tab not in (("", "pending", "approved") if admin else ("", "new", "mine")): return resp
        rows = [r for r in rp_overdue() if admin or r["user_id"] == g.user["id"]]
        if not rows: return resp
        if admin:
            body = ("<h2 style=\"color:#a63232\">Overdue returns (%d)</h2><p class=\"mu\">These approved requests passed their return time and the items are still marked as not returned.</p><div class=\"tb\"><table>"
                    "<tr><th>RFU No.</th><th>Requester</th><th>Equipment</th><th>Was due</th></tr>%s</table></div>%s"
                    "<a class=\"btn sm\" href=\"%s\">Open Approved tab</a>") % (
                len(rows), "".join("<tr><td>%s</td><td>%s<br><span class=\"mu\">%s</span></td><td>%s</td><td>%s</td></tr>" % (_esc(r["rfu_no"]), _esc(r["requester"]), _esc(r["dept"]), _esc(_rp_items(r["id"])), _esc(_rp_when(r))) for r in rows[:6]),
                ("<p class=\"mu\">...and %d more.</p>" % (len(rows) - 6)) if len(rows) > 6 else "", url_for("dashboard", tab="approved"))
        else:
            body = ("<h2 style=\"color:#a63232\">Please return the borrowed equipment</h2>" + "".join("<p style=\"margin:4px 0\"><b>%s</b>: %s - was due back on <b>%s</b>.</p>" % (_esc(r["rfu_no"]), _esc(_rp_items(r["id"])), _esc(_rp_when(r))) for r in rows)
                    + "<p class=\"mu\">Return it to the GSO so others can use it.</p>")
        resp.set_data(resp.get_data(as_text=True).replace('<div class="wrap">', '<div class="wrap"><div class="card" style="border-left:6px solid #a63232">' + body + "</div>", 1))
    except Exception as err:
        print("rp_overdue_banner:", err)
    return resp

# ---- 5. New calendar (replaces the Calendar tab: /dashboard?tab=cal now opens /schedule; old calendar code is untouched) ----
@app.route("/schedule")
@login_required
def sched_page():
    admin = g.user["role"] == "admin"
    m = request.args.get("m") or today_ph().strftime("%Y-%m")
    try: y, mo = int(m[:4]), int(m[5:7]); date(y, mo, 1)
    except Exception: y, mo = today_ph().year, today_ph().month
    ym = "%04d-%02d" % (y, mo); first, last = ym + "-01", "%s-%02d" % (ym, monthrange(y, mo)[1])
    f, s_ = request.args.get("f", "all"), request.args.get("s", "all")
    if f not in FACILITIES and f != "__equip": f = "all"
    stq = "status='Approved'" if s_ == "ap" else "status IN ('Pending','Approved')"
    days, total = {}, 0
    for r in enrich(q("SELECT * FROM requests WHERE %s AND event_date<=? AND COALESCE(return_date,event_date)>=? ORDER BY event_date,t1" % stq, last, first)):
        if f == "__equip" and not r["its"]: continue
        if f in FACILITIES and f.lower() not in [x.lower() for x in r["fac"]]: continue
        rd = r["return_date"] or r["event_date"]
        known = [x for x in r["fac"] if x in FACILITIES]
        cls = "c%d" % FACILITIES.index(f if f in FACILITIES else known[0]) if known else ("co" if r["fac"] else "ce")
        total += 1
        for dd in range(int(max(r["event_date"], first)[8:10]), int(min(rd, last)[8:10]) + 1):
            key = "%s-%02d" % (ym, dd)
            tl = ("%s - %s" % (t12(r["t1"]), t12(r["t2"]))) if rd == r["event_date"] else ("From " + t12(r["t1"])) if key == r["event_date"] else ("Until " + t12(r["t2"])) if key == rd else "All day"
            e = dict(id=r["id"], event=r["event"], badge=r["badge"], time=tl, res=r["res"], cls=cls, rfu=r["rfu_no"], pend=r["badge"] == "Pending",
                     short=("%s %s" % (t12(r["t1"]), r["event"])) if key == r["event_date"] else r["event"])
            if admin: e.update(requester=r["requester"], dept=r["dept"], pdf=url_for("request_pdf", rid=r["id"]),
                               link=url_for("dashboard", tab="returned" if r["returned_at"] else r["status"].lower()))
            days.setdefault(dd, []).append(e)
    py, pm = (y - 1, 12) if mo == 1 else (y, mo - 1); ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
    ctx = dict(tab="cal", admin=admin, weeks=Calendar(6).monthdayscalendar(y, mo), days=days, ym=ym, title=date(y, mo, 1).strftime("%B %Y"), total=total,
               prev="%04d-%02d" % (py, pm), next="%04d-%02d" % (ny, nm), f=f, s=s_, facs=FACILITIES, today=today_ph().isoformat(), page_sub="General Services Office - Booking Calendar")
    if admin:
        ctx["cnt"] = {k: q1(sq)[0] for k, sq in {"pending": "SELECT COUNT(*) FROM requests WHERE status='Pending'", "approved": "SELECT COUNT(*) FROM requests WHERE status='Approved' AND returned_at IS NULL",
                      "returned": "SELECT COUNT(*) FROM requests WHERE status='Approved' AND returned_at IS NOT NULL", "disapproved": "SELECT COUNT(*) FROM requests WHERE status='Disapproved'"}.items()}
    else:
        sc = student_ctx("x"); ctx.update(mine=sc["mine"], notifs=sc["notifs"], unread=sc["unread"])
    return render_template("sched.html", **ctx)

_old_dash = app.view_functions["dashboard"]
def rp_dash():
    if request.args.get("tab") == "cal" and g.get("user"):
        return redirect(url_for("sched_page", m=request.args.get("m")) if request.args.get("m") else url_for("sched_page"))
    return _old_dash()
app.view_functions["dashboard"] = rp_dash

RP_CSS += """
.sc-bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}.sc-bar select,.sc-bar input{width:auto;min-width:120px}.sc-bar .btn,.sc-bar button{margin:0}.sc-bar .gap{flex:1}
table.sc{width:100%;table-layout:fixed;border-collapse:collapse}.sc th{text-align:center;font-size:12px;padding:6px 0;color:var(--mu);border-bottom:1px solid var(--bd)}
.sc td{border:1px solid var(--bd);height:96px;vertical-align:top;padding:3px;cursor:pointer;overflow:hidden;font-size:11px}.sc td.off{cursor:default;background:rgba(0,0,0,.03)}
.sc td:hover:not(.off){background:rgba(20,65,123,.08)}.sc td.sel{outline:2px solid var(--pr);outline-offset:-2px}
.dn{display:inline-block;min-width:22px;height:22px;line-height:22px;text-align:center;font-weight:600;font-size:12px;border-radius:50%}.sc td.today .dn{background:var(--pr);color:#fff}
.sc-chip{display:block;color:#fff;border-radius:3px;padding:1px 4px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:11px}
.c0{background:#14417b}.c1{background:#2b7a47}.c2{background:#b45309}.c3{background:#7b3fa0}.ce{background:#0f766e}.co{background:#5d6779}
.sc-chip.pend{background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.3) 0 5px,transparent 5px 10px);outline:1.5px dashed rgba(0,0,0,.4);outline-offset:-1.5px}
.sc-more{font-size:11px;color:var(--mu);display:block;margin-top:2px}.sc-leg{display:flex;flex-wrap:wrap;gap:6px 14px;margin:12px 0 0;font-size:12.5px;color:var(--mu)}
.sc-leg i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-2px}
.sc-ev{border-left:6px solid;padding:8px 12px;margin:8px 0;border-radius:6px;background:rgba(127,127,127,.09)}.sc-ev b{font-size:15px}.sc-ev .mu{font-size:13px}
@media(max-width:640px){.sc td{height:58px;padding:2px}.sc-chip{font-size:0;height:7px;width:7px;border-radius:50%;display:inline-block;margin:1px;padding:0}.sc-more{font-size:10px}.dn{min-width:20px;height:20px;line-height:20px}}
"""
app.jinja_env.globals["RPCSS"] = RP_CSS

T["sched.html"] = """{% extends 'rp_base.html' %}{% block rp %}{% import 'macros.html' as m %}
{% if admin %}{{ m.side([('__label','Requests',none,''),('pending','Pending',cnt.pending,'pending'),('approved','Approved',cnt.approved,'approved'),('returned','Returned',cnt.returned,'returned'),('disapproved','Disapproved',cnt.disapproved,'disapproved'),('__label','Manage',none,''),('cal','Calendar',none,'cal'),('stock','Equipment Stock',none,'stock'),('users','Users',none,'users'),('report','Monthly Report',none,'report')], 'cal') }}
{% else %}{{ m.side([('new','New Request',none,'new'),('mine','My Requests',mine|length,'list'),('cal','Calendar',none,'cal')], 'cal') }}{{ m.bell(notifs, unread) }}{% endif %}
<div class="card"><h2>Booking Calendar</h2>
<div class="sc-bar"><a class="btn s" href="{{ url_for('sched_page', m=prev, f=f, s=s) }}">&lsaquo; Prev</a><a class="btn s" href="{{ url_for('sched_page', f=f, s=s) }}">Today</a><a class="btn s" href="{{ url_for('sched_page', m=next, f=f, s=s) }}">Next &rsaquo;</a>
<b style="font-size:17px;margin-left:6px">{{ title }}</b><span class="mu">&middot; {{ total }} booking{{ 's' if total != 1 }}</span></div>
<form method="get" class="sc-bar"><input type="month" name="m" value="{{ ym }}"><select name="f"><option value="all">All facilities &amp; equipment</option>{% for x in facs %}<option value="{{ x }}" {{ 'selected' if f == x }}>{{ x }}</option>{% endfor %}<option value="__equip" {{ 'selected' if f == '__equip' }}>Equipment borrowing only</option></select>
<select name="s"><option value="all">Approved + pending</option><option value="ap" {{ 'selected' if s == 'ap' }}>Approved only</option></select><button>Apply</button></form>
<table class="sc"><tr>{% for d in ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'] %}<th>{{ d }}</th>{% endfor %}</tr>
{% for w in weeks %}<tr>{% for d in w %}{% if d %}{% set evs = days.get(d, []) %}<td id="d{{ d }}" class="{{ 'today' if ym ~ '-' ~ ('%02d'|format(d)) == today }}" onclick="selDay({{ d }})"><span class="dn">{{ d }}</span>
{% for e in evs[:3] %}<span class="sc-chip {{ e.cls }} {{ 'pend' if e.pend }}" title="{{ e.event }} - {{ e.time }}">{{ e.short }}</span>{% endfor %}{% if evs|length > 3 %}<span class="sc-more">+{{ evs|length - 3 }} more</span>{% endif %}</td>{% else %}<td class="off"></td>{% endif %}{% endfor %}</tr>{% endfor %}</table>
<div class="sc-leg">{% for x in facs %}<span><i class="c{{ loop.index0 }}"></i>{{ x }}</span>{% endfor %}<span><i class="ce"></i>Equipment only</span><span><i class="co"></i>Other facility</span><span><i style="background:#888;background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.4) 0 3px,transparent 3px 6px)"></i>Pending (striped)</span></div></div>
<div class="card" id="scPanel"></div>
<script id="scdata" type="application/json">{{ days|tojson }}</script>
<script>(function(){var D=JSON.parse(document.getElementById('scdata').textContent),ym={{ ym|tojson }},today={{ today|tojson }},ADMIN={{ 'true' if admin else 'false' }},
COL={c0:'#14417b',c1:'#2b7a47',c2:'#b45309',c3:'#7b3fa0',ce:'#0f766e',co:'#5d6779'},sel=null;
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
window.selDay=function(d){var el=document.getElementById('d'+d);if(!el)return;if(sel)sel.classList.remove('sel');el.classList.add('sel');sel=el;
 var dt=new Date(ym+'-'+('0'+d).slice(-2)+'T00:00:00'),ev=D[d]||[],h='<h2>'+esc(dt.toLocaleDateString('en-US',{weekday:'long',year:'numeric',month:'long',day:'numeric'}))+'</h2>';
 if(!ev.length)h+='<p class="mu" style="font-size:14px">Nothing is booked on this day with the current filter - it looks free.</p>';
 ev.forEach(function(e){h+='<div class="sc-ev" style="border-color:'+COL[e.cls]+'"><b>'+esc(e.event)+'</b> <span class="b '+esc(e.badge)+'">'+esc(e.badge)+'</span><br><span class="mu">'+esc(e.time)+' &middot; '+esc(e.res)+'</span>';
  if(ADMIN)h+='<br><span class="mu">'+esc(e.requester)+' ('+esc(e.dept)+') &middot; RFU '+esc(e.rfu)+'</span><br><a href="'+esc(e.pdf)+'">PDF form</a> &middot; <a href="'+esc(e.link)+'">View in list</a>';
  h+='</div>'});
 document.getElementById('scPanel').innerHTML=h};
var t=today.slice(0,7)===ym?parseInt(today.slice(8),10):1;selDay(t)})()</script>{% endblock %}"""


rp_init()
def rfu_seed_defaults():
    """Sound System, Table and Chair are on the paper form, so they are in the equipment list from the start (once). The GSO edits the numbers and adds the rest."""
    with app.app_context():
        try:
            if q1("SELECT 1 FROM meta WHERE key='paper_defaults'"): return
            for n, st in (("Sound System", 2), ("Table", 30), ("Chair", 200)):
                ex("INSERT INTO inventory(name,stock) SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM inventory WHERE name=?)", n, st, n)
            ex("INSERT INTO meta VALUES('paper_defaults','1')")
        except Exception as err:
            print("rfu_seed_defaults note:", err)
# ---- 16. After sign-in: full-screen centered interface, icon rail navigation (names show on hover), top bar navigation on phones ----
RP_RAIL_CSS = """
/* ===== icon rail (desktop / tablet) ===== */
@media(min-width:701px){
 .menubtn,.sideBg{display:none!important}
 body.dash .side{transform:none!important;width:60px;top:0;bottom:0;left:0;border-radius:0;background:#161616;color:#e5e5e5;box-shadow:none;border-right:1px solid #2a2a2a;overflow-x:hidden;overflow-y:auto;z-index:70;transition:width .22s ease,box-shadow .22s ease;scrollbar-width:none}
 body.dash .side:hover,body.dash .side:focus-within{width:236px;box-shadow:8px 0 30px rgba(0,0,0,.35)}
 body.dash .side .sideuser{padding:14px 10px;border-bottom:1px solid #2a2a2a;gap:12px;min-height:66px}
 body.dash .side .sideuser .x{display:none}body.dash .side .av{width:38px;height:38px;font-size:17px;flex:none}
 body.dash .side .who2,body.dash .side a.nav span,body.dash .side .signout span,body.dash .side .navlabel{opacity:0;white-space:nowrap;transition:opacity .15s ease}
 body.dash .side:hover .who2,body.dash .side:hover a.nav span,body.dash .side:hover .signout span,body.dash .side:hover .navlabel,
 body.dash .side:focus-within .who2,body.dash .side:focus-within a.nav span,body.dash .side:focus-within .signout span,body.dash .side:focus-within .navlabel{opacity:1;transition-delay:.06s}
 body.dash .side .navlabel{padding:12px 20px 4px;height:30px;overflow:hidden;color:#8a8a8a;border-top:1px solid #2a2a2a;margin-top:6px}body.dash .side .sideuser+.navlabel{border-top:0;margin-top:0}
 body.dash .side a.nav{margin:2px 9px;padding:10px 12px;gap:14px;border-radius:8px;color:#d4d4d4;font-weight:500;font-size:14.5px;overflow:hidden}
 body.dash .side a.nav svg,body.dash .side .signout svg{width:20px;height:20px;flex:none}
 body.dash .side a.nav:hover{background:#242424;transform:none;color:#fff}body.dash .side a.nav.on{background:#2b2b2b;color:#fff;box-shadow:none}
 body.dash .side .cnt{background:#3a3a3a;color:#fff;font-size:11px;padding:0 7px;margin-left:auto}
 body.dash .side .foot{border-top:1px solid #2a2a2a;padding:8px 0 12px}
 body.dash .side .signout{margin:2px 9px;width:calc(100% - 18px);padding:10px 12px;justify-content:flex-start;gap:14px;background:transparent;border:0;border-radius:8px;color:#d4d4d4;font-weight:500;font-size:14.5px;overflow:hidden}
 body.dash .side .signout:hover{background:#3a1f1f;color:#fff}
 /* full screen, centered */
 body.dash header{padding-left:70px}
 body.dash .wrap{max-width:none!important;width:100%;margin:18px 0!important;padding:0 clamp(16px,3vw,44px) 0 calc(60px + clamp(16px,3vw,44px))!important}
 body.dash .wrap>*{max-width:1560px;margin-left:auto;margin-right:auto}
}
/* ===== phone: the navigation sits on top of the page, in the flow, so it never covers the form ===== */
@media(max-width:700px){
 .menubtn,.sideBg{display:none!important}
 body.dash .wrap{padding:0 10px!important;margin:10px auto!important;max-width:none!important}
 body.dash .side{position:sticky!important;top:0;left:auto;bottom:auto;transform:none!important;width:auto;max-width:none;display:flex;flex-direction:row;align-items:stretch;gap:2px;margin:0 0 12px;padding:4px;border-radius:12px;background:#161616;color:#e5e5e5;box-shadow:0 4px 14px rgba(0,0,0,.28);overflow-x:auto;overflow-y:hidden;z-index:60;scrollbar-width:none}
 body.dash .side::-webkit-scrollbar{display:none}
 body.dash .side .sideuser,body.dash .side .navlabel{display:none}
 body.dash .side a.nav,body.dash .side .signout{flex:1 1 0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;min-width:54px;margin:0;padding:7px 4px;border-radius:9px;color:#d4d4d4;font-size:10.5px;font-weight:500;background:transparent;border:0;position:relative;transform:none;text-align:center;line-height:1.15;white-space:normal}
 body.dash .side a.nav svg,body.dash .side .signout svg{width:20px;height:20px}
 body.dash .side a.nav.on{background:#2b2b2b;color:#fff;box-shadow:none}body.dash .side a.nav:hover{transform:none;background:#242424}
 body.dash .side .cnt{position:absolute;top:2px;right:6px;margin:0;padding:0 5px;font-size:10px;background:#c0392b;color:#fff;border-radius:10px}
 body.dash .side .foot{display:flex;flex:2 1 0;margin:0;padding:0;border:0;gap:2px}body.dash .side .foot form{margin:0;display:flex;flex:1 1 0}body.dash .side .foot>a.nav,body.dash .side .foot .signout{flex:1 1 0}
 body.dash .bell{top:auto;bottom:16px;right:14px}
}
"""
_b = T["base.html"]
_b = _b.replace("</style>" + RP_THEME_INIT, RP_RAIL_CSS + "</style>" + RP_THEME_INIT, 1)
T["base.html"] = _b

rfu_seed_defaults()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "reset-admin":
        with app.app_context():
            pw = os.environ.get("RFU_ADMIN_PASSWORD", "admin123")
            ex("UPDATE users SET pw_hash=?, must_change=1 WHERE username='admin'", generate_password_hash(pw))
        print("Admin password reset to '%s' (must be changed at next login)." % pw)
    else:
        app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=False)
