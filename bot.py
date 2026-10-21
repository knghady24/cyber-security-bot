import os
import asyncio
import logging
from contextlib import contextmanager
from aiohttp import web
import psycopg2
import psycopg2.extras
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
# إعدادات البوت وتكامل Render و Supabase
# ============================================================
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ROOT_ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
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
    return web.Response(text="Cyber Library Bot is Running Perfectly with Supabase!", status=200)

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
# الاتصال بقاعدة البيانات السحابية (PostgreSQL / Supabase Pooler)
# ------------------------------------------------------------
@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("متغير البيئة DATABASE_URL غير موجود أو فارغ!")
    
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS admins (
                admin_id TEXT PRIMARY KEY,
                added_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS bot_users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS menu_nodes (
                id SERIAL PRIMARY KEY,
                parent_id INTEGER REFERENCES menu_nodes(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                action_type TEXT NOT NULL DEFAULT 'section',
                action_value TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS node_contents (
                id SERIAL PRIMARY KEY,
                node_id INTEGER NOT NULL REFERENCES menu_nodes(id) ON DELETE CASCADE,
                content_type TEXT NOT NULL,
                title TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
    seed_default_menu()

def seed_default_menu():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM menu_nodes WHERE parent_id IS NULL")
            count = cur.fetchone()["n"]
            if count == 0:
                cur.execute("INSERT INTO menu_nodes(parent_id,name,action_type) VALUES(NULL,%s,'section')", ("🛡️ تخصص الأمن السيبراني",))
                cur.execute("INSERT INTO menu_nodes(parent_id,name,action_type) VALUES(NULL,%s,'link')", ("📢 قناة الدفعة الرسمية",))
                cur.execute("UPDATE menu_nodes SET action_value=%s WHERE name=%s AND parent_id IS NULL", ("https://t.me/telegram", "📢 قناة الدفعة الرسمية"))
                cur.execute("INSERT INTO menu_nodes(parent_id,name,action_type) VALUES(NULL,%s,'text')", ("📅 الجدول الدراسي والمحاضرات",))
                cur.execute("UPDATE menu_nodes SET action_value=%s WHERE name=%s AND parent_id IS NULL", ("📌 لم تتم إضافة الجدول الدراسي بعد.\n✨ ترقبوا التحديثات قريبًا عبر لوحة التحكم.", "📅 الجدول الدراسي والمحاضرات"))

def is_admin(user_id):
    uid = str(user_id or "")
    if ROOT_ADMIN_ID and uid == ROOT_ADMIN_ID:
        return True
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM admins WHERE admin_id=%s", (uid,))
            return cur.fetchone() is not None

def is_root(user_id):
    return bool(ROOT_ADMIN_ID) and str(user_id) == ROOT_ADMIN_ID

def register_user_and_check_new(user):
    if not user:
        return False
    is_new = False
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM bot_users WHERE user_id=%s", (str(user.id),))
            if not cur.fetchone():
                is_new = True
            cur.execute("""
                INSERT INTO bot_users(user_id,username,first_name,last_seen)
                VALUES(%s,%s,%s,CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET 
                username=EXCLUDED.username, 
                first_name=EXCLUDED.first_name, 
                last_seen=CURRENT_TIMESTAMP
            """, (str(user.id), user.username or "", user.first_name or ""))
    return is_new

def get_node(node_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM menu_nodes WHERE id=%s", (node_id,))
            return cur.fetchone()

def get_children(parent_id=None):
    with db() as conn:
        with conn.cursor() as cur:
            if parent_id is None:
                cur.execute("SELECT * FROM menu_nodes WHERE parent_id IS NULL ORDER BY sort_order, id")
            else:
                cur.execute("SELECT * FROM menu_nodes WHERE parent_id=%s ORDER BY sort_order, id", (parent_id,))
            return cur.fetchall()

def breadcrumb(node_id):
    parts = []
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,parent_id,name FROM menu_nodes WHERE id=%s", (node_id,))
            current = cur.fetchone()
            safety = 0
            while current and safety < 50:
                parts.append(current["name"])
                if current["parent_id"] is None:
                    break
                cur.execute("SELECT id,parent_id,name FROM menu_nodes WHERE id=%s", (current["parent_id"],))
                current = cur.fetchone()
                safety += 1
    return " ⚡ ".join(reversed(parts))

def student_keyboard(parent_id=None):
    nodes = get_children(parent_id)
    keyboard = [[KeyboardButton(n["name"])] for n in nodes]
    if parent_id is not None:
        keyboard.append([KeyboardButton("🔙 العودة للقسم السابق")])
    keyboard.append([KeyboardButton("🏠 القائمة الرئيسية")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def admin_only(update: Update):
    user = update.effective_user
    return bool(user and is_admin(user.id))

# ------------------------------------------------------------
# أوامر ومعالجات البوت بتصميم فائق الأناقة
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        is_new = register_user_and_check_new(user)
        if is_new and ROOT_ADMIN_ID:
            username_str = f"@{user.username}" if user.username else "بدون يوزر"
            msg = (
                f"🚨 **تنبيه نظام إداري:**\n"
                f"👤 انضم إلينا طالب جديد في البوت!\n\n"
                f"• **الاسم:** {user.first_name}\n"
                f"• **المعرف:** {username_str}\n"
                f"• **المعرف الرقمي (ID):** `{user.id}`"
            )
            try:
                await context.bot.send_message(chat_id=int(ROOT_ADMIN_ID), text=msg, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"Failed to send new user notification: {e}")

    context.user_data.pop("current_node_id", None)
    
    welcome_text = (
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "✨ **مرحباً بك في بوت الدفعة الموحد** ✨\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎓 **بوابتك الأكاديمية الشاملة للأمن السيبراني**\n\n"
        "تم تصميم وهندسة هذا البوت بكل حب واجتهاد ليكون سندك الدائم، ويوفر عليك عناء البحث الطويل خلف المراجع والملاحظات المهمة. 🚀\n\n"
        "⏳ *خلف هذه الواجهة ساعات طويلة من التفكير، البرمجة، والتعب المستمر لتخرج الخدمة بهذا الشكل المشرف.. كل ما أرجوه منكم هو دعوة صادقة بظهر الغيب أن يوفقني الله ويسر أمري.* 🤲🤍\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🏠 **لوحة التحكم الرئيسية**\n"
        "اختر القسم المطلوبة من الأزرار أدناه 👇"
    )
    
    await update.effective_message.reply_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=student_keyboard(None),
    )

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.effective_message.reply_text(f"🆔 **معرفك الرقمي الخاص:** `{uid}`", parse_mode="Markdown")

async def show_node(update: Update, context: ContextTypes.DEFAULT_TYPE, node):
    node_id = node["id"]
    context.user_data["current_node_id"] = node_id
    children = get_children(node_id)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM node_contents WHERE node_id=%s ORDER BY id", (node_id,))
            contents = cur.fetchall()
            
    action_type = node["action_type"]
    value = node["action_value"] or ""

    if action_type == "link" and value:
        await update.effective_message.reply_text(f"🔗 **{node['name']}**\n\n👉 اضغط للرابط المباشر:\n{value}", disable_web_page_preview=True, parse_mode="Markdown")
    elif action_type == "text" and value:
        await update.effective_message.reply_text(f"📌 **{node['name']}**\n\n{value}", parse_mode="Markdown")

    if contents:
        for item in contents:
            title = item["title"] or "محتوى"
            kind = item["content_type"]
            val = item["value"]
            try:
                if kind == "document":
                    await context.bot.send_document(update.effective_chat.id, document=val, caption=f"📄 **{title}**", parse_mode="Markdown")
                elif kind == "video":
                    await context.bot.send_video(update.effective_chat.id, video=val, caption=f"🎬 **{title}**", parse_mode="Markdown")
                elif kind == "photo":
                    await context.bot.send_photo(update.effective_chat.id, photo=val, caption=f"🖼️ **{title}**", parse_mode="Markdown")
                elif kind == "text":
                    await update.effective_message.reply_text(f"📌 **{title}**\n\n{val}", parse_mode="Markdown")
                elif kind == "link":
                    await update.effective_message.reply_text(f"🔗 **{title}**\n\n👉 الرابط: {val}", disable_web_page_preview=True, parse_mode="Markdown")
            except TelegramError:
                log.exception("Failed to send content %s", item["id"])
                await update.effective_message.reply_text(f"⚠️ تعذر إرسال المحتوى: {title}")

    path_str = breadcrumb(node_id)
    if children:
        await update.effective_message.reply_text(
            f"📂 **{node['name']}**\n📍 المسار: `{path_str}`\n\n📂 **اختر القسم الفرعي المطلوب:**",
            parse_mode="Markdown",
            reply_markup=student_keyboard(node_id),
        )
    else:
        await update.effective_message.reply_text(
            f"📂 **{node['name']}**\n📍 المسار: `{path_str}`\n\n✨ {'تصفح الأقسام أو عد للقائمة الرئيسية.' if contents or value else '⚠️ لا توجد محتويات مضافة هنا حتى الآن.'}",
            parse_mode="Markdown",
            reply_markup=student_keyboard(node_id),
        )

async def student_text_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    user = update.effective_user
    if user:
        register_user_and_check_new(user)

    if text == "🏠 القائمة الرئيسية":
        await start(update, context)
        return
    current_id = context.user_data.get("current_node_id")
    if text == "🔙 العودة للقسم السابق":
        if current_id is None:
            await start(update, context)
            return
        current = get_node(current_id)
        parent_id = current["parent_id"] if current else None
        if parent_id is None:
            context.user_data.pop("current_node_id", None)
            await update.message.reply_text("🏠 **القائمة الرئيسية**", parse_mode="Markdown", reply_markup=student_keyboard(None))
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

# -------------------------- لوحة الإدارة بتصميم فاخر --------------------------
def admin_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧩 إدارة جميع الأقسام والأزرار", callback_data="a:nodes:0")],
        [InlineKeyboardButton("➕ إضافة زر رئيسي جديد", callback_data="a:addroot")],
        [InlineKeyboardButton("📎 إدارة المحتويات والمرفقات", callback_data="a:contentroot")],
        [InlineKeyboardButton("👥 سجل المشتركين الكامل", callback_data="a:listusers")],
        [InlineKeyboardButton("🛠 إدارة طاقم الإشراف", callback_data="a:admins")],
        [InlineKeyboardButton("📊 الإحصائيات العامة", callback_data="a:stats"), InlineKeyboardButton("📢 إذاعة عامة", callback_data="a:broadcast")],
        [InlineKeyboardButton("🏠 العودة لواجهة الطلاب", callback_data="a:studenthome")],
    ])

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.effective_message.reply_text("❌ **عذراً، هذه اللوحة مخصصة للمشرفين فقط.**", parse_mode="Markdown")
        return
    context.user_data.pop(PENDING_KEY, None)
    await update.effective_message.reply_text("🔐 **لوحة التحكم الإدارية المركزية**\n⚡ اختر العملية المطلوبة بإتقان:", parse_mode="Markdown", reply_markup=admin_home_keyboard())

def node_list_keyboard(parent_id=None):
    nodes = get_children(parent_id)
    rows = []
    for n in nodes:
        label = ("📂 " if n["action_type"] == "section" else "🔗 " if n["action_type"] == "link" else "📝 ") + n["name"]
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"a:node:{n['id']}")])
    if parent_id is not None:
        rows.append([InlineKeyboardButton("➕ إضافة فرع داخل هذا القسم", callback_data=f"a:addchild:{parent_id}")])
    rows.append([InlineKeyboardButton("➕ إضافة زر رئيسي جديد", callback_data="a:addroot")])
    rows.append([InlineKeyboardButton("🔙 لوحة التحكم الإدارية", callback_data="a:home")])
    return InlineKeyboardMarkup(rows)

async def edit_node_screen(query, node_id):
    node = get_node(node_id)
    if not node:
        await query.edit_message_text("⚠️ هذا الزر لم يعد موجوداً في قاعدة البيانات.", reply_markup=admin_home_keyboard())
        return
    parent_text = breadcrumb(node_id)
    kb = [
        [InlineKeyboardButton("✏️ تعديل الاسم", callback_data=f"a:rename:{node_id}")],
        [InlineKeyboardButton("🔀 نقل إلى قسم آخر", callback_data=f"a:move:{node_id}")],
        [InlineKeyboardButton("🧭 تغيير نوع الزر", callback_data=f"a:type:{node_id}")],
        [InlineKeyboardButton("🔗 تعيين رابط", callback_data=f"a:setlink:{node_id}")],
        [InlineKeyboardButton("📝 تعيين نص ترحيبي", callback_data=f"a:settext:{node_id}")],
        [InlineKeyboardButton("📂 استعراض الأقسام الفرعية", callback_data=f"a:nodes:{node_id}")],
        [InlineKeyboardButton("➕ إضافة فرع جديد", callback_data=f"a:addchild:{node_id}")],
        [InlineKeyboardButton("📎 إضافة محتوى مرفق", callback_data=f"a:addcontent:{node_id}")],
        [InlineKeyboardButton("📚 إدارة محتويات الزر", callback_data=f"a:contents:{node_id}")],
        [InlineKeyboardButton("🗑️ حذف هذا الزر بالكامل", callback_data=f"a:delete:{node_id}")],
        [InlineKeyboardButton("🔙 العودة للقائمة", callback_data=f"a:nodes:{node['parent_id'] or 0}")],
    ]
    await query.edit_message_text(
        f"🧩 **إدارة خصائص الزر**\n\n• **الاسم:** {node['name']}\n• **المسار:** `{parent_text}`\n• **النوع:** `{node['action_type']}`\n\nاختر التعديل المطلوب:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def ask_text(update, context, prompt, pending):
    context.user_data[PENDING_KEY] = pending
    await update.callback_query.edit_message_text(f"{prompt}\n\n⚠️ *أرسل /cancel للإلغاء في أي وقت.*", parse_mode="Markdown")

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
        await q.edit_message_text("🔐 **لوحة التحكم الإدارية المركزية**", parse_mode="Markdown", reply_markup=admin_home_keyboard())
    elif action == "studenthome":
        context.user_data.pop("current_node_id", None)
        await q.edit_message_text("✨ واجهة الطلاب تعمل عبر أمر `/start`.\n\nإليك لوحة التحكم الإدارية:", parse_mode="Markdown", reply_markup=admin_home_keyboard())
    elif action == "contentroot":
        await q.edit_message_text(
            "📎 **إدارة المحتويات العامة:**\n\nيمكنك إضافة أو تعديل محتويات أي مادة أو قسم عبر:\n🧩 *إدارة الأزرار* -> اختر القسم -> *إضافة محتوى مرفق*.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧩 الذهاب لإدارة الأزرار", callback_data="a:nodes:0")],
                [InlineKeyboardButton("🔙 لوحة التحكم", callback_data="a:home")]
            ])
        )
    elif action == "listusers":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, username, first_name, last_seen FROM bot_users ORDER BY last_seen DESC")
                users = cur.fetchall()
        
        total = len(users)
        text = f"👥 **سجل المشتركين (الإجمالي: {total}):**\n\n"
        
        user_lines = []
        for i, u in enumerate(users, 1):
            uname = f"@{u['username']}" if u['username'] else "بدون يوزر"
            fname = u['first_name'] or "مستخدم"
            uid = u['user_id']
            user_lines.append(f"{i}. {fname} | `{uname}` | ID: `{uid}`")
        
        full_text = text + "\n".join(user_lines)
        if len(full_text) > 3800:
            full_text = text + "\n".join(user_lines[:100]) + f"\n\n... و {len(user_lines) - 100} مستخدمين آخرين."
        if not users:
            full_text = "👥 لا يوجد مستخدمين مسجلين حتى الآن."
            
        try:
            await q.edit_message_text(full_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة التحكم", callback_data="a:home")]]))
        except Exception:
            await q.message.reply_text(full_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة التحكم", callback_data="a:home")]]))

    elif action == "nodes":
        parent_id = int(parts[2]) if len(parts) > 2 else 0
        actual_parent = parent_id or None
        title = "📁 **الأزرار الرئيسية:**" if actual_parent is None else f"📂 **المسار:** `{breadcrumb(actual_parent)}`"
        await q.edit_message_text(title, parse_mode="Markdown", reply_markup=node_list_keyboard(actual_parent))
    elif action == "node":
        await edit_node_screen(q, int(parts[2]))
    elif action == "addroot":
        await ask_text(update, context, "➕ **أدخل اسم الزر الرئيسي الجديد:**", {"op": "add_node", "parent_id": None})
    elif action == "addchild":
        parent_id = int(parts[2])
        if not get_node(parent_id):
            await q.edit_message_text("⚠️ القسم الأب غير موجود.", reply_markup=admin_home_keyboard())
            return
        await ask_text(update, context, f"➕ **أدخل اسم الفرع الجديد داخل:**\n`{breadcrumb(parent_id)}`", {"op": "add_node", "parent_id": parent_id})
    elif action == "rename":
        node_id = int(parts[2])
        node = get_node(node_id)
        if node:
            await ask_text(update, context, f"✏️ **أدخل الاسم الجديد للزر الحالي:**\n`{node['name']}`", {"op": "rename", "node_id": node_id})
    elif action == "setlink":
        node_id = int(parts[2])
        await ask_text(update, context, "🔗 **أرسل الرابط الكامل المراد ربطه بالزر (يبدأ بـ https://):**", {"op": "set_value", "node_id": node_id, "type": "link"})
    elif action == "settext":
        node_id = int(parts[2])
        await ask_text(update, context, "📝 **أرسل النص الترحيبي الذي سيظهر عند فتح هذا الزر:**", {"op": "set_value", "node_id": node_id, "type": "text"})
    elif action == "type":
        node_id = int(parts[2])
        await q.edit_message_text("🧭 **اختر نوع الزر الجديد:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 قسم يفتح فروعاً وأزراراً", callback_data=f"a:changetype:{node_id}:section")],
            [InlineKeyboardButton("🔗 رابط خارجي", callback_data=f"a:changetype:{node_id}:link")],
            [InlineKeyboardButton("📝 رسالة نصية", callback_data=f"a:changetype:{node_id}:text")],
            [InlineKeyboardButton("🔙 رجوع", callback_data=f"a:node:{node_id}")],
        ]))
    elif action == "changetype":
        node_id, kind = int(parts[2]), parts[3]
        with db() as conn:
            with conn.cursor() as cur:
                if kind == 'section':
                    cur.execute("UPDATE menu_nodes SET action_type=%s, action_value=NULL WHERE id=%s", (kind, node_id))
                else:
                    cur.execute("UPDATE menu_nodes SET action_type=%s WHERE id=%s", (kind, node_id))
        await edit_node_screen(q, node_id)
    elif action == "move":
        node_id = int(parts[2])
        nodes = get_children(None)
        rows = [[InlineKeyboardButton("🏠 القائمة الرئيسية للجذر", callback_data=f"a:movehere:{node_id}:0")]]
        for n in nodes:
            if n["id"] != node_id and not is_descendant(n["id"], node_id):
                rows.append([InlineKeyboardButton(f"📂 {n['name'][:45]}", callback_data=f"a:movechoose:{node_id}:{n['id']}")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"a:node:{node_id}")])
        await q.edit_message_text("🔀 **اختر القسم الرئيسي لنقل الزر إليه:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "movechoose":
        node_id, parent_id = int(parts[2]), int(parts[3])
        children = get_children(parent_id)
        rows = [[InlineKeyboardButton("✅ نقل إلى هذا القسم بالتحديد", callback_data=f"a:movehere:{node_id}:{parent_id}")]]
        for n in children:
            if n["id"] != node_id and not is_descendant(n["id"], node_id):
                rows.append([InlineKeyboardButton(f"📂 {n['name'][:45]}", callback_data=f"a:movechoose:{node_id}:{n['id']}")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"a:node:{node_id}")])
        await q.edit_message_text(f"📍 المسار الحالي: `{breadcrumb(parent_id)}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "movehere":
        node_id, parent_id = int(parts[2]), int(parts[3])
        new_parent = parent_id or None
        if new_parent == node_id or (new_parent is not None and is_descendant(new_parent, node_id)):
            await q.edit_message_text("⚠️ لا يمكن نقل الزر داخل نفسه أو داخل أحد فروعه.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"a:node:{node_id}")]]))
            return
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE menu_nodes SET parent_id=%s WHERE id=%s", (new_parent, node_id))
        await edit_node_screen(q, node_id)
    elif action == "delete":
        node_id = int(parts[2])
        node = get_node(node_id)
        if node:
            await q.edit_message_text(
                f"🗑️ **تأكيد الحذف النهائي:**\nهل أنت متأكد من حذف الزر «{node['name']}»؟\n⚠️ سيتم حذف جميع الفروع والمحتويات المرتبطة به نهائياً.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑️ نعم، احذف نهائياً", callback_data=f"a:deleteyes:{node_id}")],
                    [InlineKeyboardButton("❌ إلغاء", callback_data=f"a:node:{node_id}")],
                ]),
            )
    elif action == "deleteyes":
        node_id = int(parts[2])
        node = get_node(node_id)
        parent_id = node["parent_id"] if node else None
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM menu_nodes WHERE id=%s", (node_id,))
        await q.edit_message_text("✅ **تم حذف الزر وكل ما يتبعه بنجاح.**", parse_mode="Markdown", reply_markup=node_list_keyboard(parent_id))
    elif action == "addcontent":
        node_id = int(parts[2])
        await q.edit_message_text("📎 **اختر نوع المحتوى المرفق المراد إضافته:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 ملف مستند / PDF", callback_data=f"a:contentkind:{node_id}:document")],
            [InlineKeyboardButton("🎥 فيديو تعليمي", callback_data=f"a:contentkind:{node_id}:video")],
            [InlineKeyboardButton("🖼️ صورة توضيحية", callback_data=f"a:contentkind:{node_id}:photo")],
            [InlineKeyboardButton("🔗 رابط خارجي", callback_data=f"a:contentkind:{node_id}:link")],
            [InlineKeyboardButton("📝 نص إضافي", callback_data=f"a:contentkind:{node_id}:text")],
            [InlineKeyboardButton("🔙 رجوع", callback_data=f"a:node:{node_id}")],
        ]))
    elif action == "contentkind":
        node_id, kind = int(parts[2]), parts[3]
        context.user_data[PENDING_KEY] = {"op": "add_content_title", "node_id": node_id, "type": kind}
        await q.edit_message_text("📌 **أرسل عنوان المحتوى أولاً (الوصف)، ثم أرسل الملف أو الرابط في الرسالة التالية.**", parse_mode="Markdown")
    elif action == "contents":
        node_id = int(parts[2])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,title,content_type FROM node_contents WHERE node_id=%s ORDER BY id", (node_id,))
                items = cur.fetchall()
        rows = [[InlineKeyboardButton(f"🗑️ حذف: {i['title'][:35]} ({i['content_type']})", callback_data=f"a:delcontent:{node_id}:{i['id']}")] for i in items]
        rows.append([InlineKeyboardButton("➕ إضافة محتوى جديد", callback_data=f"a:addcontent:{node_id}")])
        rows.append([InlineKeyboardButton("🔙 رجوع لإدارة الزر", callback_data=f"a:node:{node_id}")])
        await q.edit_message_text("📚 **إدارة ومراجعة محتويات هذا الزر:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "delcontent":
        node_id, content_id = int(parts[2]), int(parts[3])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM node_contents WHERE id=%s AND node_id=%s", (content_id, node_id))
        await q.edit_message_text("✅ **تم حذف المحتوى بنجاح.**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 العودة للزر", callback_data=f"a:node:{node_id}")]]))
    elif action == "admins":
        if not is_root(update.effective_user.id):
            await q.edit_message_text("⚠️ إدارة المشرفين متاحة للأدمن الرئيسي فقط.", reply_markup=admin_home_keyboard())
            return
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT admin_id FROM admins ORDER BY admin_id")
                admins = cur.fetchall()
        rows = []
        for a in admins:
            rows.append([InlineKeyboardButton(f"❌ إزالة المشرف: {a['admin_id']}", callback_data=f"a:deladmin:{a['admin_id']}")])
        rows.append([InlineKeyboardButton("➕ إضافة مشرف جديد", callback_data="a:addadmin")])
        rows.append([InlineKeyboardButton("🔙 لوحة التحكم", callback_data="a:home")])
        admin_list_text = "\n".join(f"• `{a['admin_id']}`" for a in admins) or "لا يوجد مشرفون إضافيون مسجلون."
        text = f"🛠 **إدارة طاقم الإشراف:**\n👑 الأدمن الرئيسي: `{ROOT_ADMIN_ID}`\n\n{admin_list_text}"
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
    elif action == "addadmin":
        if not is_root(update.effective_user.id):
            await q.edit_message_text("⚠️ هذه الصلاحية للأدمن الرئيسي فقط.")
            return
        await ask_text(update, context, "➕ **أرسل المعرف الرقمي (ID) للمشرف الجديد:**", {"op": "add_admin"})
    elif action == "deladmin":
        if not is_root(update.effective_user.id):
            await q.edit_message_text("⚠️ هذه الصلاحية للأدمن الرئيسي فقط.")
            return
        target = parts[2]
        if target == ROOT_ADMIN_ID:
            await q.edit_message_text("⚠️ لا يمكن حذف الأدمن الرئيسي من النظام.", reply_markup=admin_home_keyboard())
            return
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admins WHERE admin_id=%s", (target,))
        await q.edit_message_text(f"✅ تم حذف المشرف `{target}` بنجاح.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إدارة المشرفين", callback_data="a:admins")]]))
    elif action == "stats":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) n FROM bot_users")
                users = cur.fetchone()["n"]
                cur.execute("SELECT COUNT(*) n FROM menu_nodes")
                nodes = cur.fetchone()["n"]
                cur.execute("SELECT COUNT(*) n FROM node_contents")
                contents = cur.fetchone()["n"]
                cur.execute("SELECT COUNT(*) n FROM admins")
                admins = cur.fetchone()["n"]
        stats_text = (
            "📊 **الإحصائيات والتقارير العامة للنظام:**\n\n"
            f"👥 **إجمالي المشتركين:** `{users}`\n"
            f"🧩 **الأقسام والأزرار:** `{nodes}`\n"
            f"📎 **المرفقات والملفات:** `{contents}`\n"
            f"🛠 **المشرفون المسجلون:** `{admins}`"
        )
        await q.edit_message_text(stats_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة التحكم", callback_data="a:home")]]))
    elif action == "broadcast":
        context.user_data[PENDING_KEY] = {"op": "broadcast"}
        await q.edit_message_text("📢 **أرسل رسالة الإذاعة العامة الآن.** ستصل لجميع المشتركين بالبوت.\n\n⚠️ أرسل /cancel للإلغاء.", parse_mode="Markdown")

def is_descendant(candidate_id, ancestor_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT parent_id FROM menu_nodes WHERE id=%s", (candidate_id,))
            row = cur.fetchone()
            safety = 0
            while row and row["parent_id"] is not None and safety < 100:
                if row["parent_id"] == ancestor_id:
                    return True
                cur.execute("SELECT parent_id FROM menu_nodes WHERE id=%s", (row["parent_id"],))
                row = cur.fetchone()
                safety += 1
    return False

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(PENDING_KEY, None)
    await update.effective_message.reply_text("❌ **تم إلغاء العملية بنجاح.**", parse_mode="Markdown")

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
            await message.reply_text("⚠️ اسم الزر لا يمكن أن يكون فارغاً. أرسل الاسم مجدداً:")
            return
        parent_id = pending.get("parent_id")
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO menu_nodes(parent_id,name,action_type) VALUES(%s,%s, 'section')", (parent_id, text))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text(f"✅ **تم إضافة الزر بنجاح:** `{text}`\nيمكنك إدارته عبر لوحة التحكم.", parse_mode="Markdown")
    elif op == "rename":
        if not text:
            await message.reply_text("⚠️ الاسم لا يمكن أن يكون فارغاً.")
            return
        node_id = pending["node_id"]
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE menu_nodes SET name=%s WHERE id=%s", (text, node_id))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text(f"✅ **تم تعديل اسم الزر إلى:** `{text}`", parse_mode="Markdown")
    elif op == "set_value":
        node_id, kind = pending["node_id"], pending["type"]
        if kind == "link" and not (text.startswith("https://") or text.startswith("http://")):
            await message.reply_text("⚠️ الرابط غير صالح. تأكد أنه يبدأ بـ `https://`", parse_mode="Markdown")
            return
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE menu_nodes SET action_type=%s, action_value=%s WHERE id=%s", (kind, text, node_id))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text("✅ **تم تحديث بيانات الزر بنجاح.**", parse_mode="Markdown")
    elif op == "add_content_title":
        if not text:
            await message.reply_text("⚠️ العنوان لا يمكن أن يكون فارغاً.")
            return
        pending["title"] = text
        pending["op"] = "add_content_value"
        context.user_data[PENDING_KEY] = pending
        kind = pending["type"]
        prompts = {
            "document": "📄 أرسل ملف PDF أو المستند المطلوب الآن:",
            "video": "🎥 أرسل الفيديو التعليمي الآن:",
            "photo": "🖼️ أرسل الصورة التوضيحية الآن:",
            "link": "🔗 أرسل الرابط الكامل الآن:",
            "text": "📝 أرسل النص المراد حفظه الآن:"
        }
        await message.reply_text(prompts.get(kind, "أرسل المحتوى الآن:"))
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
            await message.reply_text("⚠️ نوع الملف المرسل لا يتطابق مع النوع المطلوب. أرسل المحتوى الصحيح أو /cancel للإلغاء.")
            return
        if kind == "link" and not (value.startswith("https://") or value.startswith("http://")):
            await message.reply_text("⚠️ الرابط يجب أن يبدأ بـ `https://`", parse_mode="Markdown")
            return
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO node_contents(node_id,content_type,title,value) VALUES(%s,%s,%s,%s)", (node_id, kind, title, value))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text("✅ **تم حفظ وحفظ المرفق بنجاح.**", parse_mode="Markdown")
    elif op == "add_admin":
        if not is_root(update.effective_user.id):
            context.user_data.pop(PENDING_KEY, None)
            await message.reply_text("⚠️ إضافة المشرفين للأدمن الرئيسي فقط.")
            return
        if not text.isdigit():
            await message.reply_text("⚠️ المعرف الرقمي يجب أن يتكون من أرقام فقط (ID).")
            return
        if text == ROOT_ADMIN_ID:
            await message.reply_text("⚠️ هذا هو الأدمن الرئيسي بالفعل.")
            return
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO admins(admin_id,added_by) VALUES(%s,%s) ON CONFLICT (admin_id) DO NOTHING", (text, str(update.effective_user.id)))
        context.user_data.pop(PENDING_KEY, None)
        await message.reply_text(f"✅ **تمت ترقية وتسجيل المشرف بنجاح:** `{text}`", parse_mode="Markdown")
    elif op == "broadcast":
        context.user_data.pop(PENDING_KEY, None)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM bot_users")
                users = [r["user_id"] for r in cur.fetchall()]
        sent = 0
        blocked = 0
        status_msg = await message.reply_text("⏳ **جاري بدء إذاعة الرسالة لجميع الطلاب...**", parse_mode="Markdown")
        for uid in users:
            try:
                await context.bot.send_message(chat_id=int(uid), text=message.text or "")
                sent += 1
                await asyncio.sleep(0.04)
            except Forbidden:
                blocked += 1
            except (TelegramError, ValueError):
                log.exception("Broadcast failed for user %s", uid)
        await status_msg.edit_text(f"📢 **انتهت الإذاعة العامة بنجاح!**\n\n• **تمت بنجاح إلى:** `{sent}` مستخدماً\n• **فشل (حظر البوت):** `{blocked}`", parse_mode="Markdown")

async def admin_pending_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get(PENDING_KEY)
    if pending and pending.get("op") == "add_content_value":
        await admin_pending_input(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception while processing update", exc_info=context.error)

# ------------------------------------------------------------
# التشغيل الأساسي للبوت ودعم Render & Supabase
# ------------------------------------------------------------
async def main():
    if not TOKEN:
        raise RuntimeError("ضع توكن البوت في متغير البيئة BOT_TOKEN.")
    if not ROOT_ADMIN_ID or not ROOT_ADMIN_ID.isdigit():
        raise RuntimeError("ضع ID الرقمي للأدمن الرئيسي في متغير البيئة ADMIN_ID.")
    if not DATABASE_URL:
        raise RuntimeError("ضع رابط قاعدة البيانات في متغير البيئة DATABASE_URL.")
    
    init_db()
    await start_web_server()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^a:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_pending_input))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.PHOTO, admin_pending_media))
    app.add_error_handler(error_handler)

    log.info("Starting Cyber Library bot with Supabase PostgreSQL...")
    await app.initialize()
    await app.start()
    app.updater.start_polling(drop_pending_updates=False)

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
