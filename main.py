import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid

from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from urllib.request import Request, urlopen

from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_SIZE = 30 * 1024 * 1024
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
        con.execute("""
            CREATE TABLE IF NOT EXISTS item_files (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                file_size INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
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
        rows = con.execute(
            "SELECT stored_name FROM item_files WHERE item_id=?", (item_id,)
        ).fetchall()
        cur = con.execute("DELETE FROM items WHERE id=?", (item_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Item not found.")
        con.execute("DELETE FROM item_files WHERE item_id=?", (item_id,))
    for row in rows:
        try:
            (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


@app.get("/api/admin/status")
def admin_status(authorization: str | None = Header(default=None)):
    user = get_user(authorization)
    return {"is_admin": int(user["id"]) in ADMIN_IDS}


def file_dict(row):
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "name": row["display_name"],
        "content_type": row["content_type"],
        "size": row["file_size"],
        "created_at": row["created_at"],
        "url": f"/api/files/{row['id']}/download",
        "preview_url": f"/api/files/{row['id']}/preview",
    }


def verify_file_user(authorization: str | None = None, auth: str | None = None):
    raw = authorization[4:].strip() if authorization and authorization.startswith("tma ") else (auth or "")
    return validate_init_data(raw)


@app.get("/api/items/{item_id}/files")
def list_item_files(item_id: str, authorization: str | None = Header(default=None)):
    get_user(authorization)
    with db() as con:
        exists = con.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "المادة غير موجودة.")
        rows = con.execute(
            "SELECT * FROM item_files WHERE item_id=? ORDER BY created_at DESC",
            (item_id,)
        ).fetchall()
    return {"files": [file_dict(row) for row in rows]}


@app.post("/api/items/{item_id}/files")
async def upload_item_file(
    item_id: str,
    file: UploadFile = File(...),
    display_name: str = Form(""),
    authorization: str | None = Header(default=None),
):
    user = get_user(authorization)
    require_admin(user)

    with db() as con:
        exists = con.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "المادة غير موجودة.")

    original_name = Path(file.filename or "file").name
    suffix = Path(original_name).suffix.lower()
    allowed_extensions = {
        ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif",
        ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
        ".txt", ".zip", ".mp4"
    }
    if suffix not in allowed_extensions:
        raise HTTPException(415, "نوع الملف غير مدعوم.")

    stored_name = f"{uuid.uuid4().hex}{suffix}"
    destination = UPLOAD_DIR / stored_name
    total = 0
    try:
        with destination.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise HTTPException(413, "حجم الملف يتجاوز 30 MB.")
                out.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if total == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, "الملف فارغ.")

    file_id = uuid.uuid4().hex
    clean_name = (display_name or "").strip() or Path(original_name).stem
    clean_name = clean_name[:160]
    content_type = file.content_type or "application/octet-stream"

    with db() as con:
        con.execute("""
            INSERT INTO item_files
            (id,item_id,display_name,stored_name,content_type,file_size,created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (
            file_id, item_id, clean_name, stored_name,
            content_type, total, int(time.time())
        ))
    return {"ok": True, "file": {"id": file_id, "name": clean_name}}


@app.patch("/api/files/{file_id}")
def rename_item_file(
    file_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
):
    user = get_user(authorization)
    require_admin(user)
    name = str(payload.get("name", "")).strip()
    if not name or len(name) > 160:
        raise HTTPException(422, "اكتب اسماً من 1 إلى 160 حرفاً.")
    with db() as con:
        cur = con.execute(
            "UPDATE item_files SET display_name=? WHERE id=?", (name, file_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "الملف غير موجود.")
    return {"ok": True}


@app.delete("/api/files/{file_id}")
def delete_item_file(file_id: str, authorization: str | None = Header(default=None)):
    user = get_user(authorization)
    require_admin(user)
    with db() as con:
        row = con.execute(
            "SELECT stored_name FROM item_files WHERE id=?", (file_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "الملف غير موجود.")
        con.execute("DELETE FROM item_files WHERE id=?", (file_id,))
    (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/files/{file_id}/download")
def download_item_file(
    file_id: str,
    authorization: str | None = Header(default=None),
    auth: str | None = Query(default=None),
):
    verify_file_user(authorization, auth)
    with db() as con:
        row = con.execute(
            "SELECT * FROM item_files WHERE id=?", (file_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "الملف غير موجود.")
    path = UPLOAD_DIR / row["stored_name"]
    if not path.is_file():
        raise HTTPException(404, "الملف غير متاح على الخادم.")
    suffix = path.suffix
    download_name = f"{row['display_name']}{suffix}"
    return FileResponse(
        path,
        media_type=row["content_type"],
        filename=download_name,
        content_disposition_type="attachment",
    )


@app.get("/api/files/{file_id}/preview")
def preview_item_file(
    file_id: str,
    authorization: str | None = Header(default=None),
    auth: str | None = Query(default=None),
):
    verify_file_user(authorization, auth)
    with db() as con:
        row = con.execute(
            "SELECT * FROM item_files WHERE id=?", (file_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "الملف غير موجود.")
    if not row["content_type"].startswith("image/"):
        raise HTTPException(415, "هذا الملف ليس صورة.")
    path = UPLOAD_DIR / row["stored_name"]
    if not path.is_file():
        raise HTTPException(404, "الصورة غير متاحة على الخادم.")
    return FileResponse(
        path,
        media_type=row["content_type"],
        headers={"Content-Disposition": "inline"},
    )
