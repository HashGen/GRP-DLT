import logging
import json
import os
import threading
import psycopg2
import asyncio # <-- Nayi line add ki hai
from urllib.parse import urlparse
from flask import Flask

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# --- Flask App Setup ---
app = Flask(__name__)
@app.route('/')
def index():
    return "Bot is running and live!"

# --- Basic Bot Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Database Setup ---
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Could not connect to the database: {e}")
        return None

def setup_database():
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL
                );
            """)
            conn.commit()
        conn.close()
        logger.info("Database table checked/created successfully.")

# --- New State Management Functions (Using Database) ---
def load_state():
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_state WHERE key = 'main_state';")
            result = cur.fetchone()
        conn.close()
        if result:
            return result[0]
    
    logger.warning("No state found in DB. Returning default state.")
    return {"is_running": False, "delay_seconds": 30}

def save_state(state):
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_state (key, value)
                VALUES ('main_state', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
            """, (json.dumps(state),))
            conn.commit()
        conn.close()

# --- Admin Check, Command Handlers, Message Handlers (SAME AS BEFORE) ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.message.chat.type == 'private': return True
    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    if 'admins' not in context.chat_data:
        admins = await context.bot.get_chat_administrators(chat_id)
        context.chat_data['admins'] = [admin.user.id for admin in admins]
    return user_id in context.chat_data.get('admins', [])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return
    help_text = "Hello! I am a Content Scrubber Bot.\n\n" \
                "**Admin Commands:**\n" \
                "`/setdelay <seconds>`\n" \
                "`/startscrub`\n" \
                "`/stopscrub`\n" \
                "`/status`"
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def setdelay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    try:
        delay = int(context.args[0])
        if not 5 <= delay <= 300:
            await update.message.reply_text("❗Delay must be between 5 and 300 seconds.")
            return
        state = load_state()
        state['delay_seconds'] = delay
        save_state(state)
        await update.message.reply_text(f"✅ Delay time has been set to **{delay} seconds**.", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text("Incorrect format! Use: `/setdelay 30`")

async def startscrub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    state = load_state()
    state['is_running'] = True
    save_state(state)
    await update.message.reply_text("🚀 **Scrubber process has been started!**", parse_mode='Markdown')

async def stopscrub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    state = load_state()
    state['is_running'] = False
    save_state(state)
    await update.message.reply_text("🛑 **Scrubber process has been stopped.**", parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    state = load_state()
    status_text = "🟢 **Running**" if state.get('is_running', False) else "🔴 **Stopped**"
    delay_text = state.get('delay_seconds', 'N/A')
    await update.message.reply_text(f"**📊 Bot Status**\n\n🔹 **Process:** {status_text}\n🔹 **Delay:** **{delay_text} seconds**", parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not state.get('is_running', False): return
    if update.message and update.message.from_user.id == context.bot.id: return
    message = update.message
    delay = state.get('delay_seconds', 30)
    context.job_queue.run_once(repost_and_delete, delay, data={'chat_id': message.chat_id, 'message_id': message.message_id}, name=str(message.message_id))

async def repost_and_delete(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id, message_id = job.data['chat_id'], job.data['message_id']
    try:
        await context.bot.copy_message(chat_id=chat_id, from_chat_id=chat_id, message_id=message_id)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as e:
        logger.warning(f"Could not process message {message_id}: {e}")

# --- Main Bot Function to run in a thread ---
def run_bot():
    # =========================================================================
    # CRITICAL FIX FOR THREADING ISSUE: Create a new event loop for this thread
    # =========================================================================
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # =========================================================================

    TOKEN = os.environ.get("TOKEN")
    if not TOKEN:
        logger.critical("CRITICAL ERROR: Bot Token not found!")
        return
        
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("setdelay", setdelay_command))
    application.add_handler(CommandHandler("startscrub", startscrub_command))
    application.add_handler(CommandHandler("stopscrub", stopscrub_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    logger.info("Starting bot polling...")
    application.run_polling()
    logger.info("Bot polling has stopped.")

# --- Main Execution Block ---
if DATABASE_URL:
    setup_database()
else:
    logger.warning("DATABASE_URL not found. Bot will not be able to save state.")

bot_thread = threading.Thread(target=run_bot)
bot_thread.daemon = True
bot_thread.start()
