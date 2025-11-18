import logging
import json
import os
import asyncio
from aiohttp import web

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# --- Basic Bot Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

STATE_FILE = 'bot_state.json'
logger.info(f"State file path: {STATE_FILE}")


# --- File-based State Management ---
def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("State file not found. Creating a default state.")
        return {"is_running": False, "delay_seconds": 30}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save state file: {e}")

# --- Admin Check, Command Handlers, Message Handlers ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or update.message.chat.type == 'private': return True
    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    if 'admins' not in context.chat_data:
        admins = await context.bot.get_chat_administrators(chat_id)
        context.chat_data['admins'] = [admin.user.id for admin in admins]
    return user_id in context.chat_data.get('admins', [])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
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
        await update.message.reply_text(f"✅ Delay time set to **{delay} seconds**.", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text("Incorrect format! Use: `/setdelay 30`")

async def startscrub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    state = load_state()
    state['is_running'] = True
    save_state(state)
    await update.message.reply_text("🚀 **Scrubber process started!**", parse_mode='Markdown')

async def stopscrub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    state = load_state()
    state['is_running'] = False
    save_state(state)
    await update.message.reply_text("🛑 **Scrubber process stopped.**", parse_mode='Markdown')

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
    
    context.application.job_queue.run_once(
        repost_and_delete,
        state.get('delay_seconds', 30),
        data={'chat_id': update.message.chat_id, 'message_id': update.message.message_id},
        name=str(update.message.message_id)
    )

async def repost_and_delete(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id, message_id = job.data['chat_id'], job.data['message_id']
    try:
        await context.bot.copy_message(chat_id=chat_id, from_chat_id=chat_id, message_id=message_id)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as e:
        logger.warning(f"Could not process message {message_id}: {e}")

# --- Web Server to keep Render alive ---
async def web_server():
    app = web.Application()
    async def hello(request):
        return web.Response(text="Bot is running!")
    app.add_routes([web.get('/', hello)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    logger.info("Starting web server...")
    await site.start()
    # Keep the server running in the background
    while True:
        await asyncio.sleep(3600)

async def main():
    TOKEN = os.environ.get("TOKEN")
    if not TOKEN:
        logger.critical("CRITICAL ERROR: Bot Token not found!")
        return

    application = Application.builder().token(TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("setdelay", setdelay_command))
    application.add_handler(CommandHandler("startscrub", startscrub_command))
    application.add_handler(CommandHandler("stopscrub", stopscrub_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    # Run bot and web server concurrently
    async with application:
        logger.info("Starting bot polling...")
        await application.start()
        await application.updater.start_polling()
        
        # Start the web server
        web_task = asyncio.create_task(web_server())
        
        # Keep everything running
        await web_task
        
        # Stop the bot when the web server stops (won't happen in this case)
        await application.updater.stop()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())
