import os
import sqlite3
import asyncio
import logging
from contextlib import contextmanager
from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.error import TelegramError, Forbidden, BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# ============================================================
# إعدادات البوت وتكامل Render
# ============================================================
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ROOT_ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "cyber_study.db")
PORT = int(os.getenv("PORT", 8080))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("cyber-library")

PENDING_KEY = "admin_pending"

# ------------------------------------------------------------
# سيرفر فحص الصحة (Health Check Server) لمنع خمول Render
# ------------------------------------------------------------
async def health_check(request):
    return web.Response(text="Cyber Library Bot is Running Perfectly!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    log.info(f"Web server started on port {PORT}")

# ------------------------------------------------------------
# التعامل مع قاعدة البيانات
# ------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS admins (
            admin_id TEXT PRIMARY KEY,
            added_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bot_users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS menu_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER REFERENCES menu_nodes(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            action_type TEXT NOT NULL DEFAULT 'section',
            action_value TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_parent ON menu_nodes(parent_id, sort_order, id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS node_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL REFERENCES menu_nodes(id) ON DELETE CASCADE,
            content_type TEXT NOT NULL,
            title TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_content_node ON node_contents(node_id, id)")
        
        if table_exists(conn, "admins"):
            admin_cols = {r["name"] for r in conn.execute("PRAGMA table_info(admins)").fetchall()}
            if "added_by" not in admin_cols:
                conn.execute("ALTER TABLE admins ADD COLUMN added_by TEXT")
            if "created_at" not in admin_cols:
                conn.execute("ALTER TABLE admins ADD COLUMN created_at TEXT")
        if table_exists(conn, "bot_users"):
            user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bot_users)").fetchall()}
            if "first_name" not in user_cols:
                conn.execute("ALTER TABLE bot_users ADD COLUMN first_name TEXT")
            if "last_seen" not in user_cols:
                conn.execute("ALTER TABLE bot_users ADD COLUMN last_seen TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS legacy_imports (
            import_key TEXT PRIMARY KEY,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")

    migrate_legacy_data()
    seed_default_menu()

def table_exists(conn, table_name):
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
    return row is not None

def migrate_legacy_data():
    with db() as conn:
        if not table_exists(conn, "menu_buttons"):
            return
        done = conn.execute("SELECT 1 FROM legacy_imports WHERE import_key='menu_buttons_v1'").fetchone()
        if done:
            return
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(menu_buttons)").fetchall()}
        if not {"button_name", "parent_name"}.issubset(cols):
            return
        rows = conn.execute("SELECT button_name, parent_name, action_type FROM menu_buttons ORDER BY id").fetchall() if "id" in cols else conn.execute("SELECT button_name, parent_name, action_type FROM menu_buttons").fetchall()
        name_to_id = {}
        for r in rows:
            name = (r["button_name"] or "").strip()
            if not name:
                continue
            exists = conn.execute("SELECT id FROM menu_nodes WHERE name=? AND parent_id IS NULL", (name,)).fetchone()
            if exists:
                name_to_id[name] = exists["id"]
            elif not r["parent_name"]:
                cur = conn.execute("INSERT INTO menu_nodes(parent_id,name,action_type,action_value) VALUES(NULL,?,?,?)", (name, "link" if (r["action_type"] or "").startswith("http") else "section", r["action_type"] if (r["action_type"] or "").startswith("http") else None))
                name_to_id[name] = cur.lastrowid
        for r in rows:
            name = (r["button_name"] or "").strip()
            parent_name = (r["parent_name"] or "").strip()
            if not name or not parent_name:
                continue
            parent = conn.execute("SELECT id FROM menu_nodes WHERE name=? ORDER BY id LIMIT 1", (parent_name,)).fetchone()
            if not parent:
                parent_cur = conn.execute("INSERT INTO menu_nodes(parent_id,name) VALUES(NULL,?)", (parent_name,))
                parent_id = parent_cur.lastrowid
            else:
                parent_id = parent["id"]
            child = conn.execute("SELECT id FROM menu_nodes WHERE name=? AND parent_id=?", (name, parent_id)).fetchone()
            if not child:
                at = r["action_type"] or "section"
                kind = "link" if at.startswith("http") else "section"
                conn.execute("INSERT INTO menu_nodes(parent_id,name,action_type,action_value) VALUES(?,?,?,?)", (parent_id, name, kind, at if kind == "link" else None))
        conn.execute("INSERT OR IGNORE INTO legacy_imports(import_key) VALUES('menu_buttons_v1')")

        if table_exists(conn, "subjects") and table_exists(conn, "contents"):
            if not conn.execute("SELECT 1 FROM legacy_imports WHERE import_key='subjects_v1'").fetchone():
                subject_cols = {r["name"] for r in conn.execute("PRAGMA table_info(subjects)").fetchall()}
                content_cols = {r["name"] for r in conn.execute("PRAGMA table_info(contents)").fetchall()}
                if {"id", "year", "term", "name"}.issubset(subject_cols) and {"subject_id", "category", "title", "url"}.issubset(content_cols):
                    subjects = conn.execute("SELECT id,year,term,name FROM subjects ORDER BY id").fetchall()
                    for s in subjects:
                        year_name = (s["year"] or "سنة غير محددة").strip()
                        term_name = (s["term"] or "فصل غير محدد").strip()
                        subj_name = (s["name"] or "مادة بدون اسم").strip()
                        year = conn.execute("SELECT id FROM menu_nodes WHERE name=? AND parent_id IS NULL", (year_name,)).fetchone()
                        if year: year_id = year["id"]
                        else: year_id = conn.execute("INSERT INTO menu_nodes(parent_id,name) VALUES(NULL,?)", (year_name,)).lastrowid
                        term = conn.execute("SELECT id FROM menu_nodes WHERE name=? AND parent_id=?", (term_name, year_id)).fetchone()
                        if term: term_id = term["id"]
                        else: term_id = conn.execute("INSERT INTO menu_nodes(parent_id,name) VALUES(?,?)", (year_id, term_name)).lastrowid
                        subj = conn.execute("SELECT id FROM menu_nodes WHERE name=? AND parent_id=?", (subj_name, term_id)).fetchone()
                        if subj: subj_id = subj["id"]
                        else: subj_id = conn.execute("INSERT INTO menu_nodes(parent_id,name) VALUES(?,?)", (term_id, subj_name)).lastrowid
                        for c in conn.execute("SELECT category,title,url FROM contents WHERE subject_id=?", (s["id"],)).fetchall():
                            title = (c["title"] or "محتوى").strip()
                            url = (c["url"] or "").strip()
                            if url:
                                conn.execute("INSERT INTO node_contents(node_id,content_type,title,value) VALUES(?,?,?,?)", (subj_id, "link", f"{c['category'] or 'محتوى'}: {title}", url))
                    conn.execute("INSERT OR IGNORE INTO legacy_imports(import_key) VALUES('subjects_v1')")

def seed_default_menu():
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM menu_nodes WHERE parent_id IS NULL").fetchone()["n"]
        if count == 0:
            conn.execute("INSERT INTO menu_nodes(parent_id,name,action_type) VALUES(NULL,?,'section')", ("🛡️ تخصص الأمن السيبراني",))
            conn.execute("INSERT INTO menu_nodes(parent_id,name,action_type) VALUES(NULL,?,'link')", ("📢 قناة الدفعة",))
            conn.execute("UPDATE menu_nodes SET action_value=? WHERE name=? AND parent_id IS NULL", ("https://t.me/telegram", "📢 قناة الدفعة"))
            conn.execute("INSERT INTO menu_nodes(parent_id,name,action_type) VALUES(NULL,?,'text')", ("📅 الجدول الدراسي",))
            conn.execute("UPDATE menu_nodes SET action_value=? WHERE name=? AND parent_id IS NULL", ("لم تتم إضافة الجدول الدراسي بعد. يمكن للإدارة تعديله من لوحة التحكم.", "📅 الجدول الدراسي"))

def is_admin(user_id):
    uid = str(user_id or "")
    if ROOT_ADMIN_ID and uid == ROOT_ADMIN_ID:
        return True
    with db() as conn:
        return conn.execute("SELECT 1 FROM admins WHERE admin_id=?", (uid,)).fetchone() is not None

def is_root(user_id):
    return bool(ROOT_ADMIN_ID) and str(user_id) == ROOT_ADMIN_ID

def register_user(user):
    if not user:
        return
    with db() as conn:
        conn.execute("""INSERT INTO bot_users(user_id,username,first_name,last_seen)
            VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
            first_name=excluded.first_name,last_seen=CURRENT_TIMESTAMP""",
            (str(user.id), user.username or "", user.first_name or ""))

def get_node(node_id):
    with db() as conn:
        return conn.execute("SELECT * FROM menu_nodes WHERE id=?", (node_id,)).fetchone()

def get_children(parent_id=None):
    with db() as conn:
        if parent_id is None:
            return conn.execute("SELECT * FROM menu_nodes WHERE parent_id IS NULL ORDER BY sort_order,id").fetchall()
        return conn.execute("SELECT * FROM menu_nodes WHERE parent_id=? ORDER BY sort_order,id", (parent_id,)).fetchall()

def breadcrumb(node_id):
    parts = []
    with db() as conn:
        current = conn.execute("SELECT id,parent_id,name FROM menu_nodes WHERE id=?", (node_id,)).fetchone()
        safety = 0
        while current and safety < 50:
            parts.append(current["name"])
            if current["parent_id"] is None:
                break
            current = conn.execute("SELECT id,parent_id,name FROM menu_nodes WHERE id=?", (current["parent_id"],)).fetchone()
            safety += 1
    return " / ".join(reversed(parts))

def student_keyboard(parent_id=None):
    nodes = get_children(parent_id)
    keyboard = [[KeyboardButton(n["name"])] for n in nodes]
    if parent_id is not None:
        keyboard.append([KeyboardButton("🔙 رجوع")])
    keyboard.append([KeyboardButton("🏠 القائمة الرئيسية")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def admin_only(update: Update):
    user = update.effective_user
    return bool(user and is_admin(user.id))

# ------------------------------------------------------------
# أوامر ومعالجات البوت
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        register_user(update.effective_user)
    context.user_data.pop("current_node_id", None)
    await update.effective_message.reply_text(
        "🏠 القائمة الرئيسية\n\nاختر القسم المطلوب:",
        reply_markup=student_keyboard(None),
    )

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"الآيدي الخاص بك: `{update.effective_user.id}`", parse_mode="Markdown")

async def show_node(update: Update, context: ContextTypes.DEFAULT_TYPE, node):
    node_id = node["id"]
    context.user_data["current_node_id"] = node_id
    children = get_children(node_id)
    with db() as conn:
        contents = conn.execute("SELECT * FROM node_contents WHERE node_id=? ORDER BY id", (node_id,)).fetchall()
    action_type = node["action_type"]
    value = node["action_value"] or ""

    if action_type == "link" and value:
        await update.effective_message.reply_text(f"🔗 {node['name']}\n{value}", disable_web_page_preview=True)
    elif action_type == "text" and value:
        await update.effective_message.reply_text(value)

    if contents:
        for item in contents:
            title = item["title"] or "محتوى"
            kind = item["content_type"]
            val = item["value"]
            try:
                if kind == "document":
                    await context.bot.send_document(update.effective_chat.id, document=val, caption=title[:1024])
                elif kind == "video":
                    await context.bot.send_video(update.effective_chat.id, video=val, caption=title[:1024])
                elif kind == "photo":
                    await context.bot.send_photo(update.effective_chat.id, photo=val, caption=title[:1024])
                elif kind == "text":
                    await update.effective_message.reply_text(f"📌 {title}\n\n{val}")
                elif kind == "link":
                    await update.effective_message.reply_text(f"🔗 {title}\n{val}", disable_web_page_preview=True)
            except TelegramError:
                log.exception("Failed to send content %s", item["id"])
                await update.effective_message.reply_text(f"تعذر إرسال المحتوى: {title}")

    if children:
        await update.effective_message.reply_text(
            f"📂 {node['name']}\n\nاختر قسمًا فرعيًا:",
            reply_markup=student_keyboard(node_id),
        )
    else:
        await update.effective_message.reply_text(
            f"📂 {node['name']}\n\n{'اختر إجراءً آخر أو ارجع للقائمة.' if contents or value else 'لا توجد أقسام أو محتويات مضافة هنا حتى الآن.'}",
            reply_markup=student_keyboard(node_id),
        )

async def student_text_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if update.effective_user:
        register_user(update.effective_user)
    if text == "🏠 القائمة الرئيسية":
        await start(update, context)
        return
    current_id = context.user_data.get("current_node_id")
    if text == "🔙 رجوع":
        if current_id is None:
            await start(update, context)
            return
        current = get_node(current_id)
        parent_id = current["parent_id"] if current else None
        if parent_id is None:
            context.user_data.pop("current_node_id", None)
            await update.message.reply_text("🏠 القائمة الرئيسية", reply_markup=student_keyboard(None))
        else:
            parent = get_node(parent_id)
            await show_node(update, context, parent)
        return

    matches = [n for n in get_children(current_id) if n["name"] == text]
    if not matches and current_id is not None:
        matches = [n for n in get_children(None) if n["name"] == text]
    if not matches:
        return
    await show_node(update, context, matches[0])

# -------------------------- لوحة الإدارة --------------------------
def admin_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧩 إدارة جميع الأزرار", callback_data="a:nodes:0")],
        [InlineKeyboardButton("➕ إضافة زر رئيسي", callback_data="a:addroot")],
        [InlineKeyboardButton("📎 إدارة المحتوى", callback_data="a:contentroot")],
        [InlineKeyboardButton("👥 إدارة المشرفين", callback_data="a:admins")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="a:stats"), InlineKeyboardButton("📢 إذاعة", callback_data="a:broadcast")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية للطلاب", callback_data="a:studenthome")],
    ])

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.effective_message.reply_text("❌ غير مصرح لك باستخدام لوحة الإدارة.")
        return
    context.user_data.pop(PENDING_KEY, None)
    await update.effective_message.reply_text("🔐 لوحة إدارة المكتبة\nاختر العملية المطلوبة:", reply_markup=admin_home_keyboard())

def node_list_keyboard(parent_id=None):
    nodes = get_children(parent_id)
    rows = []
    for n in nodes:
        label = ("📂 " if n["action_type"] == "section" else "🔗 " if n["action_type"] == "link" else "📝 ") + n["name"]
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"a:node:{n['id']}")])
    if parent_id is not None:
        rows.append([InlineKeyboardButton("➕ إضافة زر داخل هذا القسم", callback_data=f"a:addchild:{parent_id}")])
    rows.append([InlineKeyboardButton("➕ إضافة زر رئيسي", callback_data="a:addroot")])
    rows.append([InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="a:home")])
    return InlineKeyboardMarkup(rows)

async def edit_node_screen(query, node_id):
    node = get_node(node_id)
    if not node:
        await query.edit_message_text("هذا الزر لم يعد موجودًا.", reply_markup=admin_home_keyboard())
        return
    parent_text = breadcrumb(node_id)
    kb = [
        [InlineKeyboardButton("✏️ تعديل الاسم", callback_data=f"a:rename:{node_id}")],
        [InlineKeyboardButton("🔀 نقل إلى قسم آخر", callback_data=f"a:move:{node_id}")],
        [InlineKeyboardButton("🧭 تغيير نوع الزر", callback_data=f"a:type:{node_id}")],
        [InlineKeyboardButton("🔗 تعيين رابط", callback_data=f"a:setlink:{node_id}")],
        [InlineKeyboardButton("📝 تعيين نص عند الفتح", callback_data=f"a:settext:{node_id}")],
        [InlineKeyboardButton("➕ إضافة زر فرعي", callback_data=f"a:addchild:{node_id}")],
        [InlineKeyboardButton("📎 إضافة محتوى", callback_data=f"a:addcontent:{node_id}")],
        [InlineKeyboardButton("📚 إدارة محتويات الزر", callback_data=f"a:contents:{node_id}")],
        [InlineKeyboardButton("🗑️ حذف هذا الزر", callback_data=f"a:delete:{node_id}")],
        [InlineKeyboardButton("🔙 عرض الأزرار", callback_data=f"a:nodes:{node['parent_id'] or 0}")],
    ]
    await query.edit_message_text(
        f"🧩 إدارة الزر\n\nالاسم: {node['name']}\nالمسار: {parent_text}\nالنوع: {node['action_type']}\n\nاختر ما تريد تعديله:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def ask_text(update, context, prompt, pending):
    context.user_data[PENDING_KEY] = pending
    await update.callback_query.edit_message_text(prompt + "\n\nأرسل /cancel للإلغاء.")

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    if not admin_only(update):
        await q.answer("غير مصرح لك.", show_alert=True)
        return
    await q.answer()
    data = q.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "home":
        context.user_data.pop(PENDING_KEY, None)
        await q.edit_message_text("🔐 لوحة إدارة المكتبة", reply_markup=admin_home_keyboard())
    elif action == "studenthome":
        context.user_data.pop("current_node_id", None)
        await q.edit_message_text("القائمة الرئيسية للطلاب تظهر من أمر /start.\n\nلوحة الإدارة:", reply_markup=admin_home_keyboard())
    elif action == "nodes":
        parent_id = int(parts[2]) if len(parts) > 2 else 0
        actual_parent = parent_id or None
        title = "الأزرار الرئيسية" if actual_parent is None else f"الأزرار داخل: {breadcrumb(actual_parent)}"
        await q.edit_message_text(title, reply_markup=node_list_keyboard(actual_parent))
    elif action == "node":
        await edit_node_screen(q, int(parts[2]))
    elif action == "addroot":
        await ask_text(update, context, "اكتب اسم الزر الرئيسي الجديد:", {"op": "add_node", "parent_id": None})
    elif action == "addchild":
        parent_id = int(parts[2])
        if not get_node(parent_id):
            await q.edit_message_text("القسم الأب غير موجود.", reply_markup=admin_home_keyboard())
            return
        await ask_text(update, context, f"اكتب اسم الزر الجديد داخل:\n{breadcrumb(parent_id)}", {"op": "add_node", "parent_id": parent_id})
    elif action == "rename":
        node_id = int(parts[2])
        node = get_node(node_id)
        if node:
            await ask_text(update, context, f"أرسل الاسم الجديد للزر الحالي:\n{node['name']}", {"op": "rename", "node_id": node_id})
    elif action == "setlink":
        node_id = int(parts[2])
        await ask_text(update, context, "أرسل الرابط الكامل الذي تريد ربطه بهذا الزر (https://...):", {"op": "set_value", "node_id": node_id, "type": "link"})
    elif action == "settext":
        node_id = int(parts[2])
        await ask_text(update, context, "أرسل النص الذي يظهر عند فتح هذا الزر:", {"op": "set_value", "node_id": node_id, "type": "text"})
    elif action == "type":
        node_id = int(parts[2])
        await q.edit_message_text("اختر نوع الزر:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 قسم يفتح أزرارًا فرعية", callback_data=f"a:changetype:{node_id}:section")],
            [InlineKeyboardButton("🔗 رابط", callback_data=f"a:changetype:{node_id}:link")],
            [InlineKeyboardButton("📝 نص", callback_data=f"a:changetype:{node_id}:text")],
            [InlineKeyboardButton("🔙 رجوع", callback_data=f"a:node:{node_id}")],
        ]))
    elif action == "changetype":
        node_id, kind = int(parts[2]), parts[3]
        with db() as conn:
            conn.execute("UPDATE menu_nodes SET action_type=?, action_value=CASE WHEN ?='section' THEN NULL ELSE action_value END WHERE id=?", (kind, kind, node_id))
        await edit_node_screen(q, node_id)
    elif action == "move":
        node_id = int(parts[2])
        nodes = get_children(None)
        rows = [[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data=f"a:movehere:{node_id}:0")]]
        for n in nodes:
            if n["id"] != node_id and not is_descendant(n["id"], node_id):
                rows.append([InlineKeyboardButton(f"📂 {n['name'][:45]}", callback_data=f"a:movechoose:{node_id}:{n['id']}")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"a:node:{node_id}")])
        await q.edit_message_text("اختر القسم الرئيسي أو قسمًا رئيسيًا لتصفح أقسامه ثم النقل إليه:", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "movechoose":
        node_id, parent_id = int(parts[2]), int(parts[3])
        children = get_children(parent_id)
        rows = [[InlineKeyboardButton("✅ انقل إلى هذا القسم", callback_data=f"a:movehere:{node_id}:{parent_id}")]]
        for n in children:
            if n["id"] != node_id and not is_descendant(n["id"], node_id):
                rows.append([InlineKeyboardButton(f"📂 {n['name'][:45]}", callback_data=f"a:movechoose:{node_id}:{n['id']}")])
        rows.append([InlineKeyboardButton("🔙", callback_data=f"a:node:{node_id}")])
        await q.edit_message_text(f"اختر وجهة النقل. المسار: {breadcrumb(parent_id)}", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "movehere":
        node_id, parent_id = int(parts[2]), int(parts[3])
        new_parent = parent_id or None
        if new_parent == node_id or (new_parent is not None and is_descendant(new_parent, node_id)):
            await q.edit_message_text("لا يمكن نقل الزر داخل نفسه أو داخل أحد أبنائه.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"a:node:{node_id}")]]))
            return
        with db() as conn:
            conn.execute("UPDATE menu_nodes SET parent_id=? WHERE id=?", (new_parent, node_id))
        await edit_node_screen(q, node_id)
    elif action == "delete":
        node_id = int(parts[2])
        node = get_node(node_id)
        if node:
            await q.edit_message_text(
                f"تأكيد حذف الزر «{node['name']}»؟\nسيتم حذف جميع أزراره الفرعية ومحتوياته المرتبطة به نهائيًا.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑️ نعم، احذف", callback_data=f"a:deleteyes:{node_id}")],
                    [InlineKeyboardButton("إلغاء", callback_data=f"a:node:{node_id}")],
                ]),
            )
    elif action == "deleteyes":
        node_id = int(parts[2])
        node = get_node(node_id)
        parent_id = node["parent_id"] if node else None
        with db() as conn:
            conn.execute("DELETE FROM menu_nodes WHERE id=?", (node_id,))
        await q.edit_message_text("✅ تم حذف الزر وكل ما يتبعه.", reply_markup=node_list_keyboard(parent_id))
    elif action == "addcontent":
        node_id = int(parts[2])
        await q.edit_message_text("اختر نوع المحتوى الذي تريد إضافته:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 ملف PDF/مستند", callback_data=f"a:contentkind:{node_id}:document")],
            [InlineKeyboardButton("🎥 فيديو", callback_data=f"a:contentkind:{node_id}:video")],
            [InlineKeyboardButton("🖼️ صورة", callback_data=f"a:contentkind:{node_id}:photo")],
            [InlineKeyboardButton("🔗 رابط", callback_data=f"a:contentkind:{node_id}:link")],
            [InlineKeyboardButton("📝 نص", callback_data=f"a:contentkind:{node_id}:text")],
            [InlineKeyboardButton("🔙 رجوع", callback_data=f"a:node:{node_id}")],
        ]))
    elif action == "contentkind":
        node_id, kind = int(parts[2]), parts[3]
        context.user_data[PENDING_KEY] = {"op": "add_content_title", "node_id": node_id, "type": kind}
        await q.edit_message_text("أرسل عنوان المحتوى أولًا، ثم أرسل الملف/الرابط/النص في الرسالة التالية.\n\nأرسل /cancel للإلغاء.")
    elif action == "contents":
        node_id = int(parts[2])
        with db() as conn:
            items = conn.execute("SELECT id,title,content_type FROM node_contents WHERE node_id=? ORDER BY id", (node_id,)).fetchall()
        rows = [[InlineKeyboardButton(f"🗑 {i['title'][:40]} ({i['content_type']})", callback_data=f"a:delcontent:{node_id}:{i['id']}")] for i in items]
        rows.append([InlineKeyboardButton("➕ إضافة محتوى", callback_data=f"a:addcontent:{node_id}")])
        rows.append([InlineKeyboardButton("🔙 رجوع للزر", callback_data=f"a:node:{node_id}")])
        await q.edit_message_text("اختر محتوى لحذفه، أو أضف محتوى جديدًا:", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "delcontent":
        node_id, content_id = int(parts[2]), int(parts[3])
        with db() as conn:
            conn.execute("DELETE FROM node_contents WHERE id=? AND node_id=?", (content_id, node_id))
        await q.edit_message_text("✅ تم حذف المحتوى.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إدارة الزر", callback_data=f"a:node:{node_id}")]]))
    elif action == "admins":
        if not is_root(update.effective_user.id):
            await q.edit_message_text("إدارة المشرفين متاحة للأدمن الرئيسي فقط.", reply_markup=admin_home_keyboard())
            return
        with db() as conn:
            admins = conn.execute("SELECT admin_id FROM admins ORDER BY admin_id").fetchall()
        rows = []
        for a in admins:
            rows.append([InlineKeyboardButton(f"❌ حذف المشرف {a['admin_id']}", callback_data=f"a:deladmin:{a['admin_id']}")])
        rows.append([InlineKeyboardButton("➕ إضافة مشرف", callback_data="a:addadmin")])
        rows.append([InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="a:home")])
        text = "👥 إدارة المشرفين\nالأدمن الرئيسي: " + (ROOT_ADMIN_ID or "غير مضبوط") + "\n\n" + ("\n".join(f"• {a['admin_id']}" for a in admins) or "لا يوجد مشرفون إضافيون.")
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
    elif action == "addadmin":
        if not is_root(update.effective_user.id):
            await q.edit_message_text("هذه الصلاحية للأدمن الرئيسي فقط.")
            return
        await ask_text(update, context, "أرسل رقم ID الرقمي للمشرف الجديد:", {"op": "add_admin"})
    elif action == "deladmin":
        if not is_root(update.effective_user.id):
            await q.edit_message_text("هذه الصلاحية للأدمن الرئيسي فقط.")
            return
        target = parts[2]
        if target == ROOT_ADMIN_ID:
            await q.edit_message_text("لا يمكن حذف الأدمن الرئيسي.", reply_markup=admin_home_keyboard())
            return
        with db() as conn:
            conn.execute("DELETE FROM admins WHERE admin_id=?", (target,))
        await q.edit_message_text(f"تم حذف المشرف {target}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إدارة المشرفين", callback_data="a:admins")]]))
    elif action == "stats":
        with db() as conn:
            users = conn.execute("SELECT COUNT(*) n FROM bot_users").fetchone()["n"]
            nodes = conn.execute("SELECT COUNT(*) n FROM menu_nodes").fetchone()["n"]
            contents = conn.execute("SELECT COUNT(*) n FROM node_contents").fetchone()["n"]
            admins = conn.execute("SELECT COUNT(*) n FROM admins").fetchone()["n"]
        await q.edit_message_text(f"📊 إحصائيات البوت\n\n👥 المستخدمون: {users}\n🧩 الأزرار: {nodes}\n📎 المحتويات: {contents}\n🛠 المشرفون الإضافيون: {admins}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="a:home")]]))
    elif action == "broadcast":
        context.user_data[PENDING_KEY] = {"op": "broadcast"}
        await q.edit_message_text("أرسل رسالة الإذاعة. ستُرسل كنص عادي دون تفسير Markdown.\n\nأرسل /cancel للإلغاء.")

def is_descendant(candidate_id, ancestor_id):
    with db() as conn:
        row = conn.execute("SELECT parent_id FROM menu_nodes WHERE id=?", (candidate_id,)).fetchone()
        safety = 0
        while row and row["parent_id"] is not None and safety < 100:
            if row["parent_id"] == ancestor_id:
                return True
            row = conn.execute("SELECT parent_id FROM menu_nodes WHERE id=?", (row["parent_id"],)).fetchone()
            safety += 1
    return False

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(PENDING_KEY, None)
    await update.effective_message.reply_text("تم إلغاء العملية.")

async def admin_pending_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get(PENDING_KEY)
    if not pending:
        await student_text_navigation(update, context)
        return
    if not admin_only(update):
        context.user_data.pop(PENDING_KEY, None)
        await update.effective_message.reply_text("غير مصرح.")
        return
    message = update.effective_message
    op = pending.get("op")
    text = (message.text or "").strip() if message.text is not None else ""

    if op == "add_node":
        if not text:
            await message.reply_text("اسم الزر لا يمكن أن يكون فارغًا. أرسل الاسم مرة أخرى.")
            return
        parent_id = pending.get("parent_id")
        with db() as conn:
            conn.execute("INSERT INTO menu_nodes(parent_id,name,action_type) VALUES(?,?, 'section')", (parent_id, text))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text(f"✅ أُضيف الزر: {text}\nيمكنك إدارته من /admin.")
    elif op == "rename":
        if not text:
            await message.reply_text("الاسم لا يمكن أن يكون فارغًا. حاول مجددًا.")
            return
        node_id = pending["node_id"]
        with db() as conn:
            conn.execute("UPDATE menu_nodes SET name=? WHERE id=?", (text, node_id))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text(f"✅ تم تعديل اسم الزر إلى: {text}")
    elif op == "set_value":
        node_id, kind = pending["node_id"], pending["type"]
        if kind == "link" and not (text.startswith("https://") or text.startswith("http://")):
            await message.reply_text("الرابط غير صالح. أرسله كاملًا ويبدأ بـ https:// أو http://")
            return
        with db() as conn:
            conn.execute("UPDATE menu_nodes SET action_type=?, action_value=? WHERE id=?", (kind, text, node_id))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text("✅ تم تحديث الزر.")
    elif op == "add_content_title":
        if not text:
            await message.reply_text("العنوان لا يمكن أن يكون فارغًا.")
            return
        pending["title"] = text
        pending["op"] = "add_content_value"
        context.user_data[PENDING_KEY] = pending
        kind = pending["type"]
        prompts = {"document": "أرسل ملف PDF أو مستندًا الآن.", "video": "أرسل الفيديو الآن.", "photo": "أرسل الصورة الآن.", "link": "أرسل الرابط الكامل الآن.", "text": "أرسل النص المطلوب حفظه الآن."}
        await message.reply_text(prompts.get(kind, "أرسل المحتوى الآن."))
    elif op == "add_content_value":
        kind, title, node_id = pending["type"], pending["title"], pending["node_id"]
        value = None
        if kind == "document" and message.document:
            value = message.document.file_id
        elif kind == "video" and message.video:
            value = message.video.file_id
        elif kind == "photo" and message.photo:
            value = message.photo[-1].file_id
        elif kind in ("link", "text") and text:
            value = text
        if not value:
            await message.reply_text("نوع الرسالة لا يطابق المحتوى المطلوب. أرسل النوع الصحيح أو /cancel للإلغاء.")
            return
        if kind == "link" and not (value.startswith("https://") or value.startswith("http://")):
            await message.reply_text("الرابط غير صالح. يجب أن يبدأ بـ https:// أو http://")
            return
        with db() as conn:
            conn.execute("INSERT INTO node_contents(node_id,content_type,title,value) VALUES(?,?,?,?)", (node_id, kind, title, value))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text("✅ تم حفظ المحتوى بنجاح.")
    elif op == "add_admin":
        if not is_root(update.effective_user.id):
            context.user_data.pop(PENDING_KEY, None)
            await message.reply_text("إضافة المشرفين للأدمن الرئيسي فقط.")
            return
        if not text.isdigit():
            await message.reply_text("أرسل ID رقميًا فقط.")
            return
        if text == ROOT_ADMIN_ID:
            await message.reply_text("هذا هو الأدمن الرئيسي بالفعل.")
            return
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO admins(admin_id,added_by) VALUES(?,?)", (text, str(update.effective_user.id)))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text(f"✅ تم تسجيل المشرف {text}.")
    elif op == "broadcast":
        context.user_data.pop(PENDING_KEY, None)
        with db() as conn:
            users = [r["user_id"] for r in conn.execute("SELECT user_id FROM bot_users").fetchall()]
        sent = 0
        blocked = 0
        for uid in users:
            try:
                await context.bot.send_message(chat_id=int(uid), text=message.text or "")
                sent += 1
                await asyncio.sleep(0.04)
            except Forbidden:
                blocked += 1
            except (TelegramError, ValueError):
                log.exception("Broadcast failed for user %s", uid)
        await message.reply_text(f"انتهت الإذاعة.\n✅ تم الإرسال: {sent}\n⚠️ تعذر الإرسال: {blocked + max(0, len(users)-sent-blocked)}")

async def admin_pending_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get(PENDING_KEY)
    if pending and pending.get("op") == "add_content_value":
        await admin_pending_input(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception while processing update", exc_info=context.error)

# ------------------------------------------------------------
# نقطة البدء الرئيسية لدعم منصة Render
# ------------------------------------------------------------
async def main():
    if not TOKEN:
        raise RuntimeError("ضع توكن البوت في متغير البيئة BOT_TOKEN.")
    if not ROOT_ADMIN_ID or not ROOT_ADMIN_ID.isdigit():
        raise RuntimeError("ضع ID الرقمي للأدمن الرئيسي في متغير البيئة ADMIN_ID.")
    
    # 1. تهيئة قاعدة البيانات
    init_db()

    # 2. تشغيل سيرفر الويب المصغر لمنع وضع خمول Render
    await start_web_server()

    # 3. إعداد وتطبيق البوت
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^a:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_pending_input))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.PHOTO, admin_pending_media))
    app.add_error_handler(error_handler)

    log.info("Starting Cyber Library bot on Render...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=False)

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
