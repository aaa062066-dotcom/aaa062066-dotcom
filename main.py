import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from urllib.request import Request, urlopen

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "uase.db")))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

app = FastAPI(title="UASE Aviation Command Center", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


DEFAULT_ITEMS = [
    ("num-analysis", "التحليلات العددية", "Numerical Analysis", "subject", "🔢",
     "المحاضرات والملفات الخاصة بالتحليلات العددية.", "", 10),
    ("analog-communications", "الاتصالات التماثلية", "Analog Communications", "subject", "〰️",
     "المحاضرات والملفات الخاصة بالاتصالات التماثلية.", "", 20),
    ("control-theory", "نظرية السيطرة", "Control Theory", "subject", "🕹️",
     "المحاضرات والملفات الخاصة بنظرية السيطرة.", "", 30),
    ("aircraft-dynamics", "ديناميك الطائرة", "Aircraft Dynamics", "subject", "🌀",
     "المحاضرات والملفات الخاصة بديناميك الطائرة.", "", 40),
    ("antennas", "الهوائيات وانتشار الموجات", "Antennas & Wave Propagation", "subject", "📡",
     "المحاضرات والملفات الخاصة بالهوائيات وانتشار الموجات.", "", 50),
    ("aircraft-systems", "أنظمة الطائرة", "Aircraft Systems", "subject", "🛩️",
     "المحاضرات والملفات الخاصة بأنظمة الطائرة.", "", 60),
    ("vibration-lab", "مختبر الاهتزازات", "Vibration Laboratory", "lab", "📳",
     "التجارب والقراءات والتقارير المختبرية.", "", 10),
    ("analog-lab", "مختبر الاتصالات التماثلية", "Analog Communications Lab", "lab", "📻",
     "التجارب والقراءات والتقارير المختبرية.", "", 20),
    ("timetable", "جدول المحاضرات", "Class Timetable", "schedule", "📅",
     "جدول المحاضرات الأسبوعي.", "", 10),
    ("drive-library", "مكتبة الملازم والمصادر", "Notes & Resources", "resources", "☁️",
     "مجلد الملازم والملفات المشتركة على Google Drive.",
     "https://drive.google.com/drive/folders/1-RXMC6DKzon2YzxG8-LbhW9rDCrzdQEc", 10),
]


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                title_ar TEXT NOT NULL,
                title_en TEXT NOT NULL,
                category TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '📘',
                description TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        count = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if count == 0:
            now = int(time.time())
            con.executemany("""
                INSERT INTO items
                (id,title_ar,title_en,category,icon,description,url,sort_order,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, [(*item, now) for item in DEFAULT_ITEMS])


@app.on_event("startup")
def startup():
    init_db()
    if BOT_TOKEN and WEBAPP_URL.startswith("https://"):
        try:
            set_menu_button()
        except Exception as exc:
            print(f"Menu button setup warning: {exc}")
    else:
        print("Menu button not configured. Set BOT_TOKEN and HTTPS WEBAPP_URL.")


def set_menu_button():
    payload = json.dumps({
        "menu_button": {
            "type": "web_app",
            "text": "Open Student Portal",
            "web_app": {"url": WEBAPP_URL}
        }
    }).encode()
    req = Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=15) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))
    print("Telegram menu button configured.")


def validate_init_data(raw: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(503, "Server is missing BOT_TOKEN configuration.")
    if not raw:
        raise HTTPException(401, "Open this app from its Telegram bot.")
    try:
        pairs = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            raise ValueError("Missing hash")
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(pairs.items())
        )
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            raise ValueError("Invalid signature")
        auth_date = int(pairs.get("auth_date", "0"))
        if auth_date <= 0 or time.time() - auth_date > INIT_DATA_MAX_AGE:
            raise ValueError("Expired init data")
        user = json.loads(pairs.get("user", "{}"))
        if not isinstance(user, dict) or not user.get("id"):
            raise ValueError("Missing Telegram user")
        return user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Telegram session could not be verified.")


def get_user(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Telegram authentication required.")
    return validate_init_data(authorization[4:].strip())


def require_admin(user: dict):
    if int(user.get("id", 0)) not in ADMIN_IDS:
        raise HTTPException(403, "Admin access only.")


class ItemInput(BaseModel):
    id: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9-]+$")
    title_ar: str = Field(min_length=1, max_length=100)
    title_en: str = Field(min_length=1, max_length=100)
    category: str = Field(pattern=r"^(subject|lab|schedule|resources)$")
    icon: str = Field(default="📘", max_length=12)
    description: str = Field(default="", max_length=500)
    url: str = Field(default="", max_length=2000)
    sort_order: int = Field(default=100, ge=0, le=10000)


def item_dict(row):
    return dict(row)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "app": "UASE Aviation Command Center", "version": "1.0.0"}


@app.get("/api/bootstrap")
def bootstrap(authorization: str | None = Header(default=None)):
    user = get_user(authorization)
    with db() as con:
        rows = con.execute(
            "SELECT * FROM items ORDER BY category, sort_order, title_ar"
        ).fetchall()
    return {
        "user": {
            "id": user["id"],
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
        },
        "is_admin": int(user["id"]) in ADMIN_IDS,
        "items": [item_dict(row) for row in rows],
    }


@app.post("/api/items")
def create_item(payload: ItemInput, authorization: str | None = Header(default=None)):
    user = get_user(authorization)
    require_admin(user)
    if payload.url and not payload.url.startswith("https://"):
        raise HTTPException(422, "Only HTTPS links are allowed.")
    now = int(time.time())
    with db() as con:
        exists = con.execute("SELECT id FROM items WHERE id=?", (payload.id,)).fetchone()
        if exists:
            raise HTTPException(409, "This item ID already exists.")
        con.execute("""
            INSERT INTO items
            (id,title_ar,title_en,category,icon,description,url,sort_order,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            payload.id, payload.title_ar, payload.title_en, payload.category,
            payload.icon, payload.description, payload.url, payload.sort_order, now
        ))
    return {"ok": True}


@app.put("/api/items/{item_id}")
def update_item(item_id: str, payload: ItemInput,
                authorization: str | None = Header(default=None)):
    user = get_user(authorization)
    require_admin(user)
    if item_id != payload.id:
        raise HTTPException(400, "Item ID cannot be changed.")
    if payload.url and not payload.url.startswith("https://"):
        raise HTTPException(422, "Only HTTPS links are allowed.")
    with db() as con:
        cur = con.execute("""
            UPDATE items SET title_ar=?,title_en=?,category=?,icon=?,
            description=?,url=?,sort_order=?,updated_at=? WHERE id=?
        """, (
            payload.title_ar, payload.title_en, payload.category, payload.icon,
            payload.description, payload.url, payload.sort_order, int(time.time()),
            item_id
        ))
        if cur.rowcount == 0:
            raise HTTPException(404, "Item not found.")
    return {"ok": True}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str, authorization: str | None = Header(default=None)):
    user = get_user(authorization)
    require_admin(user)
    with db() as con:
        cur = con.execute("DELETE FROM items WHERE id=?", (item_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Item not found.")
    return {"ok": True}


@app.get("/api/admin/status")
def admin_status(authorization: str | None = Header(default=None)):
    user = get_user(authorization)
    return {"is_admin": int(user["id"]) in ADMIN_IDS}
