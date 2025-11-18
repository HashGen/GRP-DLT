import logging
import json
import os
import threading
from flask import Flask

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# --- Flask App Setup ---
# This will keep the web service alive
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running!"

# --- Basic Bot Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DATA_DIR = '/data'
if os.path.exists(DATA_DIR):
    STATE_FILE = os.path.join(DATA_DIR, 'bot_state.json')
else:
    STATE_FILE = 'bot_state.json'
    
logger.info(f"State file path: {STATE_FILE}")


# --- State Management Functions (For the bot's memory) ---

def load_state():
    """Loads the state from the JSON file."""
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("State file not found. Creating a default state.")
        return {"is_running": False, "delay_seconds": 30}

def save_state(state):
    """Saves the current state to the JSON file."""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save state file: {e}")


# --- Admin Check Function ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the user sending the command is an admin."""
    if update.message.chat.type == 'private': return True
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    if 'admins' not in context.chat_data:
        admins = await context.bot.get_chat_administrators(chat_id)
        context.chat_data['admins'] = [admin.user.id for admin in admins]
    return user_id in context.chat_data.get('admins', [])


# --- Command Handlers (Same as before) ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return
    help_text = (
        "Hello! I am a Content Scrubber Bot.\n\n"
        "I delete every message and repost it on my behalf after a set delay.\n\n"
        "**Admin Commands:**\n"
        "`/setdelay <seconds>` - Set the repost delay. (e.g., `/setdelay 15`)\n"
        "`/startscrub` - Start the scrubbing process.\n"
        "`/stopscrub` - Stop the process.\n"
        "`/status` - Check the bot's current status."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def setdelay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return
    try:
        delay = int(context.args[0])
        if delay < 5 or delay > 300:
            await update.message.reply_text("❗Delay must be between 5 and 300 seconds.")
            return
        state = load_state()
        state['delay_seconds'] = delay
        save_state(state)
        await update.message.reply_text(f"✅ Delay time has been set to **{delay} seconds**.", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text("Incorrect format! Use: `/setdelay 30`")

async def startscrub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return
    state = load_state()
    state['is_running'] = True
    save_state(state)
    await update.message.reply_text("🚀 **Scrubber process has been started!**", parse_mode='Markdown')

async def stopscrub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return
    state = load_state()
    state['is_running'] = False
    save_state(state)
    await update.message.reply_text("🛑 **Scrubber process has been stopped.**", parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return
    state = load_state()
    status_text = "🟢 **Running**" if state.get('is_running', False) else "🔴 **Stopped**"
    delay_text = state.get('delay_seconds', 'N/A')
    await update.message.reply_text(f"**📊 Bot Status**\n\n🔹 **Process:** {status_text}\n🔹 **Delay:** **{delay_text} seconds**", parse_mode='Markdown')


# --- Message Handler (Same as before) ---
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
    except Exception as e:
        logger.error(f"Unexpected error for message {message_id}: {e}")

# --- Main Bot Function to run in a thread ---
def run_bot():
    """Initializes and runs the bot."""
    TOKEN = os.environ.get("TOKEN")
    if not TOKEN:
        logger.critical("CRITICAL ERROR: Bot Token not found!")
        return

    application = Application.builder().token(TOKEN).build()
    
    # Add all handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("setdelay", setdelay_command))
    application.add_handler(CommandHandler("startscrub", startscrub_command))
    application.add_handler(CommandHandler("stopscrub", stopscrub_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    logger.info("Starting bot polling...")
    application.run_polling()

# --- Main Execution Block ---
if __name__ == "__main__":
    # Run the bot in a separate thread
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    
    # Run the Flask web server in the main thread
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting Flask web server on port {port}...")
    app.run(host='0.0.0.0', port=port)
