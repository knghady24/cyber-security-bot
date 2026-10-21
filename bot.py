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
DEFAULT_ADMIN_ID = os.environ.get("ADMIN_ID") # الأيدي الأساسي من إعدادات Render

def init_db():
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
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
    # جدول الأقسام المخصصة الإضافية للمادة
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS custom_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            category_name TEXT
        )
    ''')
    # جدول المشرفين (يدعم ID أو Username)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            admin_identifier TEXT UNIQUE
        )
    ''')
    # جدول الطلاب/المستخدمين لتتبع الإذاعة والإحصائيات
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id TEXT UNIQUE,
            username TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# دالة للتحقق هل المستخدم أدمن؟
def is_user_admin(user_id, username=None):
    str_id = str(user_id)
    clean_username = f"@{username.replace('@', '')}" if username else ""

    if DEFAULT_ADMIN_ID and str_id == str(DEFAULT_ADMIN_ID):
        return True
    
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE admin_identifier = ? OR (admin_identifier = ? AND admin_identifier != '')", 
                   (str_id, clean_username))
    res = cursor.fetchone()
    conn.close()
    return res is not None

# دالة لتسجيل المستخدمين الجدد
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

# حالات المحادثة للأدمن
ADD_SUB_TERM, ADD_SUB_NAME = range(2)
CONT_SUB, CONT_CAT, CONT_TITLE, CONT_URL = range(2, 6)
ADD_CAT_SUB, ADD_CAT_NAME = range(6, 8)
ADD_ADMIN_INPUT = range(8, 9)
BROADCAST_MSG = range(9, 10)

# ----------------------------------------------------
# 2. سيرفر الويب لمنع وضع النوم (UptimeRobot)
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
# 3. واجهة الطلاب (أزرار الكيبورد الثابتة)
# ----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username)

    keyboard = [
        [KeyboardButton("🛡️ تخصص الأمن السيبراني")],
        [KeyboardButton("📢 قناة الدفعة")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    text = "👋 أهلاً بك في بوت المكتبة الأكاديمية!\n\nاختر من الكيبورد أدناه للوصول إلى المواد والملازم:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def student_text_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    register_user(user.id, user.username)

    # 1. القائمة الرئيسية / التخصصات
    if text in ["🛡️ تخصص الأمن السيبراني", "🔙 رجوع للتخصصات"]:
        keyboard = [
            [KeyboardButton("🎓 سنة ثالثة")],
            [KeyboardButton("🏠 القائمة الرئيسية")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("🛡️ **تخصص الأمن السيبراني**\n\nاختر السنة الدراسية:", reply_markup=reply_markup, parse_mode='Markdown')

    elif text == "🎓 سنة ثالثة" or text == "🔙 رجوع للفصول":
        keyboard = [
            [KeyboardButton("📖 الفصل الأول"), KeyboardButton("📖 الفصل الثاني")],
            [KeyboardButton("🔙 رجوع للتخصصات")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("🎓 **سنة ثالثة**\n\nاختر الفصل الدراسي:", reply_markup=reply_markup, parse_mode='Markdown')

    elif text in ["📖 الفصل الأول", "📖 الفصل الثاني"]:
        term_name = "الفصل الأول" if text == "📖 الفصل الأول" else "الفصل الثاني"
        year_name = "سنة ثالثة"
        
        conn = sqlite3.connect('cyber_study.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM subjects WHERE year = ? AND term = ?", (year_name, term_name))
        subjects = cursor.fetchall()
        conn.close()

        keyboard = []
        for sub_id, sub_name in subjects:
            keyboard.append([KeyboardButton(f"📘 {sub_name}")])
        
        keyboard.append([KeyboardButton("🔙 رجوع للفصول")])
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

            await update.message.reply_text(f"📘 **مادة: {sub_name_input}**\n\nاختر نوع المحتوى الذي تبحث عنه:", 
                                           reply_markup=reply_markup, parse_mode='Markdown')

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
        except Exception as e:
            pass

    elif text == "🏠 القائمة الرئيسية":
        await start(update, context)

    elif text == "📢 قناة الدفعة":
        await update.message.reply_text("📢 **قناة الدفعة الرسمية:**\nhttps://t.me/telegram")

# ----------------------------------------------------
# 4. لوحة تحكم الأدمن (أزرار شفافة Inline)
# ----------------------------------------------------
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_user_admin(user.id, user.username):
        if update.message:
            await update.message.reply_text("❌ عذراً، هذا الأمر مخصص لإدارة البوت فقط.")
        return

    keyboard = [
        [InlineKeyboardButton("➕ إضافة مادة جديدة", callback_data='adm_add_sub')],
        [InlineKeyboardButton("🔗 إضافة رابط/ملزمة", callback_data='adm_add_cont')],
        [InlineKeyboardButton("🗂️ إضافة زر/قسم جديد للمادة", callback_data='adm_add_cat')],
        [InlineKeyboardButton("👥 إضافة مشرف جديد (Admin)", callback_data='adm_add_admin')],
        [InlineKeyboardButton("📢 إذاعة عامة للدفعة", callback_data='adm_broadcast')],
        [InlineKeyboardButton("📊 إحصائيات البوت", callback_data='adm_stats')],
        [InlineKeyboardButton("🏠 خروج", callback_data='main_menu')]
    ]
    
    text = "🔐 **لوحة تحكم إدارة البوت الشاملة:**\n\nاختر العملية التي تريد القيام بها:"
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- أ. خطوات إضافة مادة جديدة ---
async def admin_add_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("📖 الفصل الأول", callback_data='term_1')],
        [InlineKeyboardButton("📖 الفصل الثاني", callback_data='term_2')]
    ]
    await query.edit_message_text("اختر الفصل الدراسي للمادة الجديدة (سنة ثالثة):", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_SUB_TERM

async def admin_sub_term_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['new_sub_term'] = "الفصل الأول" if query.data == 'term_1' else "الفصل الثاني"
    await query.edit_message_text(f"ممتاز! اكتب الآن **اسم المادة** الجديدة المراد إضافتها لـ ({context.user_data['new_sub_term']} - سنة ثالثة):")
    return ADD_SUB_NAME

async def admin_sub_name_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sub_name = update.message.text.strip()
    term = context.user_data['new_sub_term']
    
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO subjects (year, term, name) VALUES (?, ?, ?)", ("سنة ثالثة", term, sub_name))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ تم بنجاح إضافة مادة **({sub_name})** إلى (سنة ثالثة - {term})!", parse_mode='Markdown')
    return ConversationHandler.END

# --- ب. خطوات إضافة محتوى/رابط لمادة ---
async def admin_add_cont_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("📖 الفصل الأول", callback_data='cterm_1')],
        [InlineKeyboardButton("📖 الفصل الثاني", callback_data='cterm_2')]
    ]
    await query.edit_message_text("اختر الفصل الدراسي للمادة:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONT_SUB

async def admin_cont_term_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    term_name = "الفصل الأول" if query.data == 'cterm_1' else "الفصل الثاني"
    
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM subjects WHERE year = ? AND term = ?", ("سنة ثالثة", term_name))
    subjects = cursor.fetchall()
    conn.close()

    if not subjects:
        await query.edit_message_text("⚠️ لا توجد مواد مضافة في هذا الفصل بعد. أضف مادة أولاً.")
        return ConversationHandler.END

    keyboard = []
    for sub_id, sub_name in subjects:
        keyboard.append([InlineKeyboardButton(f"📘 {sub_name}", callback_data=f"csub_{sub_id}")])
    
    await query.edit_message_text("اختر المادة المطلوبة:", reply_markup=InlineKeyboardMarkup(keyboard))
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

    await query.edit_message_text("اختر القسم الذي تريد إضافة الرابط إليه:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONT_CAT

async def admin_cont_cat_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split('_', 1)[1]
    context.user_data['target_category'] = category

    await query.edit_message_text("اكتب **عنوان الرابط أو الملف** (مثال: ملزمة المحاضرة الأولى):", parse_mode='Markdown')
    return CONT_TITLE

async def admin_cont_title_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['target_title'] = update.message.text
    await update.message.reply_text("أرسل الآن **رابط الملف أو الفيديو**:")
    return CONT_URL

async def admin_cont_url_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    sub_id = context.user_data['target_sub_id']
    category = context.user_data['target_category']
    title = context.user_data['target_title']

    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO contents (subject_id, category, title, url) VALUES (?, ?, ?, ?)",
                   (sub_id, category, title, url))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ تم بنجاح إضافة ({title}) إلى قسم ({category})!", parse_mode='Markdown')
    return ConversationHandler.END

# --- ج. خطوات إضافة قسم/زر جديد للمادة ---
async def admin_add_cat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("📖 الفصل الأول", callback_data='dterm_1')],
        [InlineKeyboardButton("📖 الفصل الثاني", callback_data='dterm_2')]
    ]
    await query.edit_message_text("اختر الفصل الدراسي للمادة لإضافة قسم جديد لها:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_CAT_SUB

async def admin_cat_term_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    term_name = "الفصل الأول" if query.data == 'dterm_1' else "الفصل الثاني"
    
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM subjects WHERE year = ? AND term = ?", ("سنة ثالثة", term_name))
    subjects = cursor.fetchall()
    conn.close()

    if not subjects:
        await query.edit_message_text("⚠️ لا توجد مواد في هذا الفصل.")
        return ConversationHandler.END

    keyboard = []
    for sub_id, sub_name in subjects:
        keyboard.append([InlineKeyboardButton(f"📘 {sub_name}", callback_data=f"dsub_{sub_id}")])
    
    await query.edit_message_text("اختر المادة:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_CAT_SUB

async def admin_cat_sub_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sub_id = query.data.split('_')[1]
    context.user_data['cat_sub_id'] = sub_id

    await query.edit_message_text("اكتب الآن **اسم الزر/القسم الجديد** (مثال: مشاريع العملي):", parse_mode='Markdown')
    return ADD_CAT_NAME

async def admin_cat_name_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat_name = update.message.text.strip()
    sub_id = context.user_data['cat_sub_id']

    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO custom_categories (subject_id, category_name) VALUES (?, ?)", (sub_id, cat_name))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ تم إضافة زر القسم الجديد **({cat_name})** بنجاح للمادة!", parse_mode='Markdown')
    return ConversationHandler.END

# --- د. إضافة مشرف جديد (يدعم Username أو ID) ---
async def admin_add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "👥 **إضافة مشرف جديد:**\n\n"
        "أرسل الآن **اسم المستخدم (Username)** مثل `@username` أو الـ **Telegram ID** الخاص بالشخص المراد ترقيته لأدمن:",
        parse_mode='Markdown'
    )
    return ADD_ADMIN_INPUT

async def admin_add_admin_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_text = update.message.text.strip()
    if not input_text.startswith("@") and not input_text.isdigit():
        input_text = f"@{input_text}"

    try:
        conn = sqlite3.connect('cyber_study.db')
        cursor = conn.cursor()
        cursor.execute("INSERT INTO admins (admin_identifier) VALUES (?)", (input_text,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ تم منح صلاحيات الإدارة بنجاح للمشرف: `{input_text}`", parse_mode='Markdown')
    except sqlite3.IntegrityError:
        await update.message.reply_text("⚠️ هذا المستخدم مسجل كأدمن مسبقاً.")
    except Exception as e:
        await update.message.reply_text(f"حدث خطأ: {e}")
    return ConversationHandler.END

# --- هـ. الإذاعة العامة (Broadcast) ---
async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📢 **الإذاعة العامة:**\n\nأرسل الآن الرسالة التي تريد إرسالها لجميع طلاب الدفعة المشتركين في البوت:", parse_mode='Markdown')
    return BROADCAST_MSG

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = update.message.text
    
    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM bot_users")
    users = cursor.fetchall()
    conn.close()

    success_count = 0
    await update.message.reply_text(f"⏳ جاري إرسال الرسالة إلى {len(users)} طالباً...")

    for (u_id,) in users:
        try:
            await context.bot.send_message(chat_id=int(u_id), text=f"📢 **تنبيه هام للدفعة:**\n\n{msg_text}", parse_mode='Markdown')
            success_count += 1
            await asyncio.sleep(0.1)
        except:
            pass

    await update.message.reply_text(f"✅ تمت الإذاعة بنجاح! وصل الإشعار إلى ({success_count}) طالباً.")
    return ConversationHandler.END

# --- و. إحصائيات البوت ---
async def admin_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = sqlite3.connect('cyber_study.db')
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM bot_users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM subjects")
    total_subjects = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM contents")
    total_contents = cursor.fetchone()[0]
    conn.close()

    stats_text = (
        "📊 **إحصائيات بوت الدفعة:**\n\n"
        f"👥 عدد الطلاب المشتركين: `{total_users}`\n"
        f"📘 إجمالي المواد المضافة: `{total_subjects}`\n"
        f"📄 إجمالي الملفات والروابط: `{total_contents}`\n"
    )
    keyboard = [[InlineKeyboardButton("🔙 رجوع للوحة الأدمن", callback_data='back_to_admin')]]
    await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def back_to_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    update.message = query.message
    await admin_start(update, context)

# ----------------------------------------------------
# 5. التشغيل الرئيسي وإعداد المعالجات
# ----------------------------------------------------
async def main_async():
    if not TOKEN:
        print("خطأ: لم يتم تحديد BOT_TOKEN!")
        return

    await start_web_server()

    app = Application.builder().token(TOKEN).build()

    add_sub_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_sub_start, pattern='^adm_add_sub$')],
        states={
            ADD_SUB_TERM: [CallbackQueryHandler(admin_sub_term_chosen, pattern='^term_[12]$')],
            ADD_SUB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_sub_name_saved)]
        },
        fallbacks=[]
    )

    add_cont_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_cont_start, pattern='^adm_add_cont$')],
        states={
            CONT_SUB: [
                CallbackQueryHandler(admin_cont_term_chosen, pattern='^cterm_[12]$'),
                CallbackQueryHandler(admin_cont_sub_chosen, pattern='^csub_')
            ],
            CONT_CAT: [CallbackQueryHandler(admin_cont_cat_chosen, pattern='^ccat_')],
            CONT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cont_title_saved)],
            CONT_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cont_url_saved)]
        },
        fallbacks=[]
    )

    add_cat_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_cat_start, pattern='^adm_add_cat$')],
        states={
            ADD_CAT_SUB: [
                CallbackQueryHandler(admin_cat_term_chosen, pattern='^dterm_[12]$'),
                CallbackQueryHandler(admin_cat_sub_chosen, pattern='^dsub_')
            ],
            ADD_CAT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cat_name_saved)]
        },
        fallbacks=[]
    )

    add_admin_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_admin_start, pattern='^adm_add_admin$')],
        states={
            ADD_ADMIN_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_admin_saved)]
        },
        fallbacks=[]
    )

    broadcast_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern='^adm_broadcast$')],
        states={
            BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send)]
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_start))
    app.add_handler(add_sub_handler)
    app.add_handler(add_cont_handler)
    app.add_handler(add_cat_handler)
    app.add_handler(add_admin_handler)
    app.add_handler(broadcast_handler)

    app.add_handler(CallbackQueryHandler(admin_stats_handler, pattern='^adm_stats$'))
    app.add_handler(CallbackQueryHandler(back_to_admin_handler, pattern='^back_to_admin$'))
    
    # معالج التصفح بالكيبورد للطلاب
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, student_text_navigation))

    print("🚀 بوت الأمن السيبراني يعمل بأزرار الكيبورد الثابتة وميزة رفع الأدمن باليوزر...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

def main():
    asyncio.run(main_async())

if __name__ == '__main__':
    main()
