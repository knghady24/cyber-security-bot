import os
import sqlite3
import asyncio
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, ContextTypes, ConversationHandler, filters
)

# ----------------------------------------------------
# 1. إعداد قاعدة البيانات والاتصال
# ----------------------------------------------------
TOKEN = os.environ.get("BOT_TOKEN")
DEFAULT_ADMIN_ID = os.environ.get("ADMIN_ID")

def init_db():
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    # جدول الأزرار الرئيسية والقوائم الديناميكية
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS menu_buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_name TEXT,
            button_name TEXT UNIQUE,
            action_type TEXT
        )
    ''')
    # جدول المواد
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year TEXT,
            term TEXT,
            name TEXT
        )
    ''')
    # جدول المحتوى والروابط
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            category TEXT,
            title TEXT,
            url TEXT
        )
    ''')
    # جدول الأقسام المخصصة للمادة
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS custom_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            category_name TEXT
        )
    ''')
    # جدول المشرفين
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            admin_id TEXT UNIQUE
        )
    ''')
    # جدول الطلاب
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id TEXT UNIQUE,
            username TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def is_user_admin(user_id):
    str_id = str(user_id)
    if DEFAULT_ADMIN_ID and str_id == str(DEFAULT_ADMIN_ID):
        return True
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE admin_id = ?", (str_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def register_user(user_id, username=None):
    try:
        conn = sqlite3.connect('cyber_study.db')
        cursor = conn.cursor()
        uname = f"@{username}" if username else ""
        cursor.execute("INSERT OR REPLACE INTO bot_users (user_id, username) VALUES (?, ?)", (str(user_id), uname))
        conn.commit()
        conn.close()
    except:
        pass

async def get_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(f"🆔 **الآيدي الخاص بك هو:**\n`{user_id}`", parse_mode='Markdown')

# حالات المحادثة للأدمن
ADD_SUB_TERM, ADD_SUB_NAME = range(2)
CONT_SUB, CONT_CAT, CONT_TITLE, CONT_URL = range(2, 6)
ADD_CAT_SUB, ADD_CAT_NAME = range(6, 8)
ADD_ADMIN_INPUT = range(8, 9)
BROADCAST_MSG = range(9, 10)
DEL_SUB_TERM, DEL_SUB_CHOOSE = range(10, 12)
ADD_MENU_BTN, EDIT_MENU_BTN = range(12, 14)

# ----------------------------------------------------
# 2. سيرفر الويب لمنع وضع النوم
# ----------------------------------------------------
async def health_check(request):
    return web.Response(text="Cyber Security Bot is Online & Active!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# ----------------------------------------------------
# 3. واجهة الطلاب الديناميكية
# ----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username)

    # جلب الأزرار الرئيسية المضافة من قاعدة البيانات أو الافتراضية
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT button_name FROM menu_buttons WHERE parent_name IS NULL")
    buttons = cursor.fetchall()
    conn.close()

    keyboard = []
    if buttons:
        for (b_name,) in buttons:
            keyboard.append([KeyboardButton(b_name)])
    else:
        # أزرار افتراضية أولية لو القائمة فارغة
        keyboard = [
            [KeyboardButton("🛡️ تخصص الأمن السيبراني")],
            [KeyboardButton("📢 قناة الدفعة")],
            [KeyboardButton("📅 الجدول الدراسي")]
        ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    text = "👋 أهلاً بك في بوت المكتبة الأكاديمية الشاملة!\n\nاختر من الكيبورد أدناه للوصول إلى الأقسام:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def student_text_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    register_user(user.id, user.username)

    if text == "🏠 القائمة الرئيسية":
        await start(update, context)
        return

    # فحص لو كان الزر يفتح قناة أو رابط أو قسم فرعي
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT action_type FROM menu_buttons WHERE button_name = ?", (text,))
    res = cursor.fetchone()
    conn.close()

    if res and res[0] and res[0].startswith("http"):
        await update.message.reply_text(f"🔗 رابط القسم ({text}):\n{res[0]}")
        return

    # التفاعل الافتراضي للتخصصات والسنوات
    if "الأمن السيبراني" in text or text == "🔙 رجوع للتخصصات":
        conn = sqlite3.connect('cyber_study.db')
        cursor = conn.cursor()
        cursor.execute("SELECT button_name FROM menu_buttons WHERE parent_name = 'الأمن السيبراني'")
        sub_btns = cursor.fetchall()
        conn.close()

        keyboard = []
        if sub_btns:
            for (sb,) in sub_btns:
                keyboard.append([KeyboardButton(sb)])
        else:
            keyboard = [[KeyboardButton("🎓 سنة ثالثة")], [KeyboardButton("🎓 سنة رابعة")]]
        
        keyboard.append([KeyboardButton("🏠 القائمة الرئيسية")])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(f"🛡️ **{text}**\n\nاختر السنة الدراسية:", reply_markup=reply_markup, parse_mode='Markdown')

    elif "سنة ثالثة" in text or "سنة رابعة" in text or text == "🔙 رجوع للفصول":
        context.user_data['current_year'] = text
        keyboard = [
            [KeyboardButton("📖 الفصل الأول"), KeyboardButton("📖 الفصل الثاني")],
            [KeyboardButton("🔙 رجوع للتخصصات")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(f"🎓 **{text}**\n\nاختر الفصل الدراسي:", reply_markup=reply_markup, parse_mode='Markdown')

    elif text in ["📖 الفصل الأول", "📖 الفصل الثاني"]:
        term_name = "الفصل الأول" if text == "📖 الفصل الأول" else "الفصل الثاني"
        year_name = context.user_data.get('current_year', "سنة ثالثة")
        
        conn = sqlite3.connect('cyber_study.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM subjects WHERE year = ? AND term = ?", (year_name, term_name))
        subjects = cursor.fetchall()
        conn.close()

        keyboard = []
        for sub_id, sub_name in subjects:
            keyboard.append([KeyboardButton(f"📘 {sub_name}")])
        
        keyboard.append([KeyboardButton("🔙 رجوع للفصول"), KeyboardButton("🏠 القائمة الرئيسية")])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        msg = f"📚 **مواد {year_name} - {term_name}:**\n\n"
        if not subjects:
            msg += "⚠️ لا توجد مواد مضافة في هذا الفصل بعد."
        else:
            msg += "اختر المادة لعرض محتوياتها:"
            
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='Markdown')

    elif text.startswith("📘 "):
        sub_name_input = text.replace("📘 ", "").strip()
        conn = sqlite3.connect('cyber_study.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, term FROM subjects WHERE name = ?", (sub_name_input,))
        sub_info = cursor.fetchone()

        if sub_info:
            sub_id, sub_term = sub_info
            cursor.execute("SELECT category_name FROM custom_categories WHERE subject_id = ?", (sub_id,))
            custom_cats = cursor.fetchall()
            conn.close()

            keyboard = [
                [KeyboardButton(f"📄 ملازم - {sub_name_input}"), KeyboardButton(f"🎥 فيديوهات - {sub_name_input}")],
                [KeyboardButton(f"📝 امتحانات - {sub_name_input}")]
            ]
            for (cat_name,) in custom_cats:
                keyboard.append([KeyboardButton(f"📌 {cat_name} - {sub_name_input}")])

            keyboard.append([KeyboardButton(f"🔙 رجوع لمواد {sub_term}")])
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

            await update.message.reply_text(f"📘 **مادة: {sub_name_input}**\n\nاختر نوع المحتوى:", reply_markup=reply_markup, parse_mode='Markdown')

    elif text.startswith("🔙 رجوع لمواد "):
        term_target = text.replace("🔙 رجوع لمواد ", "").strip()
        update.message.text = f"📖 {term_target}"
        await student_text_navigation(update, context)

    elif any(text.startswith(prefix) for prefix in ["📄 ملازم - ", "🎥 فيديوهات - ", "📝 امتحانات - ", "📌 "]):
        try:
            if " - " in text:
                cat_part, sub_name = text.split(" - ", 1)
                category = cat_part.replace("📄 ملازم", "ملازم").replace("🎥 فيديوهات", "فيديوهات").replace("📝 امتحانات", "امتحانات").replace("📌 ", "")
            else:
                return

            conn = sqlite3.connect('cyber_study.db')
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM subjects WHERE name = ?", (sub_name,))
            sub_row = cursor.fetchone()

            if sub_row:
                sub_id = sub_row[0]
                cursor.execute("SELECT title, url FROM contents WHERE subject_id = ? AND category = ?", (sub_id, category))
                items = cursor.fetchall()
                conn.close()

                text_resp = f"📌 **قسم ({category}) - مادة {sub_name}:**\n\n"
                if not items:
                    text_resp += "لا توجد روابط مضافة هنا حالياً."
                else:
                    for title, url in items:
                        text_resp += f"• [{title}]({url})\n"

                await update.message.reply_text(text_resp, parse_mode='Markdown', disable_web_page_preview=True)
        except:
            pass

    elif text == "📢 قناة الدفعة":
        await update.message.reply_text("📢 **قناة الدفعة الرسمية:**\nhttps://t.me/telegram")

# ----------------------------------------------------
# 4. لوحة تحكم الأدمن الشاملة
# ----------------------------------------------------
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_user_admin(user.id):
        if update.message:
            await update.message.reply_text("❌ عذراً، هذا الأمر مخصص لإدارة البوت فقط.")
        return

    keyboard = [
        [InlineKeyboardButton("➕ إضافة زر رئيسي جديد", callback_data='adm_add_menu_btn'), InlineKeyboardButton("❌ حذف زر رئيسي", callback_data='adm_del_menu_btn')],
        [InlineKeyboardButton("➕ إضافة مادة", callback_data='adm_add_sub'), InlineKeyboardButton("❌ حذف مادة", callback_data='adm_del_sub')],
        [InlineKeyboardButton("🔗 إضافة رابط/ملزمة", callback_data='adm_add_cont')],
        [InlineKeyboardButton("🗂️ إضافة قسم فرعي للمادة", callback_data='adm_add_cat')],
        [InlineKeyboardButton("👥 إدارة المشرفين", callback_data='adm_manage_admins')],
        [InlineKeyboardButton("📢 إذاعة عامة", callback_data='adm_broadcast'), InlineKeyboardButton("📊 الإحصائيات", callback_data='adm_stats')],
        [InlineKeyboardButton("🏠 خروج", callback_data='main_menu')]
    ]
    
    text = "🔐 **لوحة التحكم المطلقة (صلاحيات كاملة):**\n\nاختر العملية:"
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- أ. إدارة الأزرار الرئيسية الديناميكية ---
async def admin_add_menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("اكتب الآن **اسم الزر الجديد** الذي تريد إضافته في القائمة الرئيسية (مثال: الجدول الدراسي، سنة رابعة، قناة الدفعة):")
    return ADD_MENU_BTN

async def admin_add_menu_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btn_name = update.message.text.strip()
    try:
        conn = sqlite3.connect('cyber_study.db')
        cursor = conn.cursor()
        cursor.execute("INSERT INTO menu_buttons (button_name) VALUES (?)", (btn_name,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ تم إضافة الزر الرئيسي **({btn_name})** بنجاح!")
    except Exception as e:
        await update.message.reply_text(f"خطأ أو الزر موجود مسبقاً: {e}")
    return ConversationHandler.END

# --- ب. إضافة مادة وحذفها ---
async def admin_add_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("📖 الفصل الأول", callback_data='term_1')],
        [InlineKeyboardButton("📖 الفصل الثاني", callback_data='term_2')]
    ]
    await query.edit_message_text("اختر الفصل الدراسي:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_SUB_TERM

async def admin_sub_term_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['new_sub_term'] = "الفصل الأول" if query.data == 'term_1' else "الفصل الثاني"
    await query.edit_message_text("اكتب اسم السنة الدراسية للمادة (مثال: سنة ثالثة أو سنة رابعة):")
    return ADD_SUB_NAME

async def admin_sub_name_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # حفظ السنة ثم الانتقال لكتابة اسم المادة
    if 'temp_year' not in context.user_data:
        context.user_data['temp_year'] = update.message.text.strip()
        await update.message.reply_text(f"ممتاز! الآن اكتب **اسم المادة** لـ ({context.user_data['temp_year']} - {context.user_data['new_sub_term']}):")
        return ADD_SUB_NAME
    else:
        sub_name = update.message.text.strip()
        year = context.user_data['temp_year']
        term = context.user_data['new_sub_term']
        
        conn = sqlite3.connect('cyber_study.db')
        cursor = conn.cursor()
        cursor.execute("INSERT INTO subjects (year, term, name) VALUES (?, ?, ?)", (year, term, sub_name))
        conn.commit()
        conn.close()

        await update.message.reply_text(f"✅ تم إضافة مادة **({sub_name})** بنجاح!")
        context.user_data.pop('temp_year', None)
        return ConversationHandler.END

async def admin_del_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, year, term FROM subjects")
    subjects = cursor.fetchall()
    conn.close()

    if not subjects:
        await query.edit_message_text("⚠️ لا توجد مواد مضافة لحذفها.")
        return ConversationHandler.END

    keyboard = []
    for sub_id, s_name, s_year, s_term in subjects:
        keyboard.append([InlineKeyboardButton(f"❌ حذف: {s_name} ({s_year} - {s_term})", callback_data=f"delsub_{sub_id}")])
    
    await query.edit_message_text("اختر المادة التي تريد حذفها:", reply_markup=InlineKeyboardMarkup(keyboard))
    return DEL_SUB_CHOOSE

async def admin_del_sub_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sub_id = query.data.split('_')[1]

    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM contents WHERE subject_id = ?", (sub_id,))
    cursor.execute("DELETE FROM custom_categories WHERE subject_id = ?", (sub_id,))
    cursor.execute("DELETE FROM subjects WHERE id = ?", (sub_id,))
    conn.commit()
    conn.close()

    await query.edit_message_text("✅ تم حذف المادة نهائياً.")
    return ConversationHandler.END

# --- ج. إضافة محتوى وقسم ---
async def admin_add_cont_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, year, term FROM subjects")
    subjects = cursor.fetchall()
    conn.close()

    if not subjects:
        await query.edit_message_text("⚠️ أضف مواد أولاً.")
        return ConversationHandler.END

    keyboard = []
    for sub_id, s_name, s_year, s_term in subjects:
        keyboard.append([InlineKeyboardButton(f"📘 {s_name} ({s_year})", callback_data=f"csub_{sub_id}")])
    
    await query.edit_message_text("اختر المادة الإضافة الملف إليها:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONT_SUB

async def admin_cont_sub_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sub_id = query.data.split('_')[1]
    context.user_data['target_sub_id'] = sub_id

    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT category_name FROM custom_categories WHERE subject_id = ?", (sub_id,))
    custom_cats = cursor.fetchall()
    conn.close()

    keyboard = [
        [InlineKeyboardButton("📄 ملازم وكتب", callback_data='ccat_ملازم')],
        [InlineKeyboardButton("🎥 فيديوهات ودروس", callback_data='ccat_فيديوهات')],
        [InlineKeyboardButton("📝 امتحانات وأسئلة", callback_data='ccat_امتحانات')]
    ]
    for (cat_name,) in custom_cats:
        keyboard.append([InlineKeyboardButton(f"📌 {cat_name}", callback_data=f"ccat_{cat_name}")])

    await query.edit_message_text("اختر القسم:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONT_CAT

async def admin_cont_cat_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split('_', 1)[1]
    context.user_data['target_category'] = category

    await query.edit_message_text("اكتب عنوان الملف أو المحاضرة:")
    return CONT_TITLE

async def admin_cont_title_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['target_title'] = update.message.text
    await update.message.reply_text("أرسل رابط الملف أو الفيديو:")
    return CONT_URL

async def admin_cont_url_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    sub_id = context.user_data['target_sub_id']
    category = context.user_data['target_category']
    title = context.user_data['target_title']

    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO contents (subject_id, category, title, url) VALUES (?, ?, ?, ?)", (sub_id, category, title, url))
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ تم إضافة المحتوى بنجاح!")
    return ConversationHandler.END

# --- د. إدارة المشرفين ---
async def admin_manage_admins_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT admin_id FROM admins")
    admins = cursor.fetchall()
    conn.close()

    text = "👥 **إدارة المشرفين:**\n\n"
    keyboard = []
    if DEFAULT_ADMIN_ID:
        text += f"• الأدمن الرئيسي (Root): `{DEFAULT_ADMIN_ID}`\n"

    for (aid,) in admins:
        text += f"• مشرف (ID: `{aid}`)\n"
        keyboard.append([InlineKeyboardButton(f"❌ حذف المشرف: {aid}", callback_data=f"deladmin_{aid}")])

    keyboard.append([InlineKeyboardButton("➕ إضافة مشرف بالـ ID", callback_data='adm_add_admin_start')])
    keyboard.append([InlineKeyboardButton("🔙 رجوع لوحة الأدمن", callback_data='back_to_admin')])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_add_admin_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("أرسل الـ ID الرقمي للمشرف الجديد (يمكنه معرفته بإرسال الأمر /id للبوت):")
    return ADD_ADMIN_INPUT

async def admin_add_admin_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = update.message.text.strip()
    try:
        conn = sqlite3.connect('cyber_study.db')
        cursor = conn.cursor()
        cursor.execute("INSERT INTO admins (admin_id) VALUES (?)", (target_id,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ تم تعيين المشرف `{target_id}` بنجاح!")
    except:
        await update.message.reply_text("⚠️ مسجل مسبقاً أو حدث خطأ.")
    return ConversationHandler.END

async def admin_delete_admin_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    aid = query.data.split('_')[1]
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM admins WHERE admin_id = ?", (aid,))
    conn.commit()
    conn.close()
    await query.edit_message_text("✅ تم حذف المشرف بنجاح.")

# --- هـ. الإذاعة والإحصائيات ---
async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📢 أرسل رسالة الإذاعة للطلاب:")
    return BROADCAST_MSG

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = update.message.text
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM bot_users")
    users = cursor.fetchall()
    conn.close()

    count = 0
    for (u_id,) in users:
        try:
            await context.bot.send_message(chat_id=int(u_id), text=f"📢 **تنبيه:**\n\n{msg_text}", parse_mode='Markdown')
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await update.message.reply_text(f"✅ تمت الإذاعة إلى ({count}) طالباً.")
    return ConversationHandler.END

async def admin_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM bot_users")
    u = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM subjects")
    s = cursor.fetchone()[0]
    conn.close()
    
    await query.edit_message_text(f"📊 الإحصائيات:\n👥 الطلاب: {u}\n📘 المواد: {s}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data='back_to_admin')]]))

async def back_to_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    update.message = query.message
    await admin_start(update, context)

# ----------------------------------------------------
# 5. التشغيل الرئيسي
# ----------------------------------------------------
async def main_async():
    if not TOKEN:
        return
    await start_web_server()
    app = Application.builder().token(TOKEN).build()

    add_menu_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_menu_start, pattern='^adm_add_menu_btn$')],
        states={ADD_MENU_BTN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_menu_saved)]},
        fallbacks=[]
    )

    add_sub_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_sub_start, pattern='^adm_add_sub$')],
        states={
            ADD_SUB_TERM: [CallbackQueryHandler(admin_sub_term_chosen, pattern='^term_[12]$')],
            ADD_SUB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_sub_name_saved)]
        },
        fallbacks=[]
    )

    del_sub_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_del_sub_start, pattern='^adm_del_sub$')],
        states={DEL_SUB_CHOOSE: [CallbackQueryHandler(admin_del_sub_execute, pattern='^delsub_')]},
        fallbacks=[]
    )

    add_cont_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_cont_start, pattern='^adm_add_cont$')],
        states={
            CONT_SUB: [CallbackQueryHandler(admin_cont_sub_chosen, pattern='^csub_')],
            CONT_CAT: [CallbackQueryHandler(admin_cont_cat_chosen, pattern='^ccat_')],
            CONT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cont_title_saved)],
            CONT_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cont_url_saved)]
        },
        fallbacks=[]
    )

    add_admin_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_admin_prompt, pattern='^adm_add_admin_start$')],
        states={ADD_ADMIN_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_admin_saved)]},
        fallbacks=[]
    )

    broadcast_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern='^adm_broadcast$')],
        states={BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send)]},
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_start))
    app.add_handler(CommandHandler("id", get_id_command))

    app.add_handler(add_menu_handler)
    app.add_handler(add_sub_handler)
    app.add_handler(del_sub_handler)
    app.add_handler(add_cont_handler)
    app.add_handler(add_admin_handler)
    app.add_handler(broadcast_handler)

    app.add_handler(CallbackQueryHandler(admin_manage_admins_handler, pattern='^adm_manage_admins$'))
    app.add_handler(CallbackQueryHandler(admin_delete_admin_execute, pattern='^deladmin_'))
    app.add_handler(CallbackQueryHandler(admin_stats_handler, pattern='^adm_stats$'))
    app.add_handler(CallbackQueryHandler(back_to_admin_handler, pattern='^back_to_admin$'))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, student_text_navigation))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

def main():
    asyncio.run(main_async())

if __name__ == '__main__':
    main()
