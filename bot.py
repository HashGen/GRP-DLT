import logging
import json
import os
import asyncio
from aiohttp import web
import pymongo
from datetime import datetime, timedelta
from bson.objectid import ObjectId

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# --- Basic Bot Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- MongoDB Database Setup ---
MONGO_URI = os.environ.get('MONGO_URI')
mongo_client = None
db = None
config_collection = None
loops_collection = None

if MONGO_URI:
    try:
        mongo_client = pymongo.MongoClient(MONGO_URI)
        db = mongo_client.get_database("telegram_bot_db")
        config_collection = db.config
        loops_collection = db.active_loops
        # Create an index for faster lookups
        loops_collection.create_index("current_message_id")
        logger.info("Successfully connected to MongoDB.")
    except Exception as e:
        logger.error(f"Could not connect to MongoDB: {e}")
else:
    logger.warning("MONGO_URI not found. Bot will not have permanent memory.")

# --- State Management using MongoDB ---
def get_config():
    default_config = {"_id": "main_config", "repost_delay_seconds": 30, "loop_duration_seconds": 43200}
    if config_collection is not None:
        config = config_collection.find_one({"_id": "main_config"})
        if config:
            for key, value in default_config.items():
                config.setdefault(key, value)
            return config
    return default_config

def save_config(config):
    if config_collection is not None:
        config_collection.update_one({"_id": "main_config"}, {"$set": config}, upsert=True)

# --- Admin Check and Handlers ---
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
    help_text = "Hello! This is the bulletproof Scrubber Bot.\n\n" \
                "**How it works:**\n" \
                "1. Set a loop duration ONCE with `/setloopduration`.\n" \
                "2. Any file you send will loop for that duration.\n" \
                "3. After the time is up, the file is deleted permanently.\n\n" \
                "**Admin Commands:**\n" \
                "`/setloopduration <time>`\n" \
                "`/setdelay <seconds>`\n" \
                "`/stopallloops`\n" \
                "`/status`"
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def setloopduration_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    # ... (code is correct, no changes needed)
    if not context.args: await update.message.reply_text("Example: `/setloopduration 12h`"); return
    duration_str = context.args[0].lower()
    try:
        value = int(duration_str[:-1])
        unit = duration_str[-1]
        if unit == 'h': seconds = value * 3600
        elif unit == 'm': seconds = value * 60
        else: raise ValueError("Invalid unit")
        config = get_config(); config['loop_duration_seconds'] = seconds; save_config(config)
        await update.message.reply_text(f"✅ Loop duration for all new files set to **{value}{unit}**.")
    except (ValueError, IndexError): await update.message.reply_text("Invalid format. Use: `/setloopduration 12h` or `/setloopduration 30m`")

async def setdelay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    # ... (code is correct, no changes needed)
    try:
        delay = int(context.args[0])
        if not 5 <= delay <= 300: await update.message.reply_text("❗Delay must be between 5 and 300 seconds."); return
        config = get_config(); config['repost_delay_seconds'] = delay; save_config(config)
        await update.message.reply_text(f"✅ Repost delay set to **{delay} seconds**.", parse_mode='Markdown')
    except (IndexError, ValueError): await update.message.reply_text("Incorrect format! Use: `/setdelay 30`")

async def stopallloops_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if loops_collection is not None:
        result = loops_collection.delete_many({})
        await update.message.reply_text(f"🛑 **All {result.deleted_count} active loops have been stopped.**")
    else: await update.message.reply_text("Database not connected.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    config = get_config()
    delay = config.get('repost_delay_seconds')
    duration_seconds = config.get('loop_duration_seconds')
    duration_hours = duration_seconds / 3600
    active_loops_count = loops_collection.count_documents({}) if loops_collection is not None else 0
    status_msg = f"**📊 Bot Status**\n\n" \
                 f"🔹 **Repost Delay:** **{delay} seconds**\n" \
                 f"🔹 **Loop Duration:** **{duration_hours:.1f} hours**\n" \
                 f"🔹 **Files Looping:** **{active_loops_count}**"
    await update.message.reply_text(status_msg, parse_mode='Markdown')

# --- New, Bulletproof Message Processing Logic ---
async def loop_processor(context: ContextTypes.DEFAULT_TYPE, loop_id: ObjectId):
    """A dedicated, self-sustaining loop for a single file."""
    while True:
        config = get_config()
        repost_delay = config.get('repost_delay_seconds')
        
        await asyncio.sleep(repost_delay)
        
        # Get the latest info for this loop from the DB
        loop_doc = loops_collection.find_one({"_id": loop_id})

        if not loop_doc:
            logger.info(f"Loop {loop_id} stopped (manually or deleted).")
            break # Exit the loop

        if loop_doc.get("expiration_time") < datetime.now():
            logger.info(f"Loop {loop_id} expired. Performing final delete.")
            try:
                await context.bot.delete_message(chat_id=loop_doc.get("current_chat_id"), message_id=loop_doc.get("current_message_id"))
            except BadRequest: pass
            loops_collection.delete_one({"_id": loop_id})
            break # Exit the loop

        # If we are here, the loop is active. Repost and continue.
        chat_id = loop_doc.get("current_chat_id")
        message_id = loop_doc.get("current_message_id")

        try:
            new_message = await context.bot.copy_message(chat_id=chat_id, from_chat_id=chat_id, message_id=message_id)
            # Update the DB with the new message ID
            loops_collection.update_one({"_id": loop_id}, {"$set": {"current_message_id": new_message.message_id}})
        except Exception as e:
            logger.error(f"Failed to repost message for loop {loop_id}: {e}")
            # If reposting fails, stop the loop to prevent issues
            loops_collection.delete_one({"_id": loop_id})
            break
        finally:
            # Always delete the old message
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except BadRequest: pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles only NEW messages from HUMAN users to start a loop."""
    if not update.message or loops_collection is None: return
    
    # IMPORTANT: Ignore messages from any bot, including itself
    if update.message.from_user.is_bot:
        return

    config = get_config()
    expiration_time = datetime.now() + timedelta(seconds=config.get('loop_duration_seconds'))
    
    # Create a new document in MongoDB for the new file
    new_loop = {
        "current_chat_id": update.message.chat_id,
        "current_message_id": update.message.message_id,
        "expiration_time": expiration_time
    }
    result = loops_collection.insert_one(new_loop)
    
    # Start the dedicated processor for this new loop
    logger.info(f"Starting a new loop: {result.inserted_id}")
    asyncio.create_task(loop_processor(context, result.inserted_id))

# --- Web Server and Main Bot Execution ---
async def web_server():
    # ... (code is correct, no changes needed)
    app = web.Application()
    async def hello(request): return web.Response(text="Bot is running!")
    app.add_routes([web.get('/', hello)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()
    while True: await asyncio.sleep(3600)

async def main():
    TOKEN = os.environ.get("TOKEN")
    if not TOKEN: logger.critical("CRITICAL ERROR: Bot Token not found!"); return
    application = Application.builder().token(TOKEN).build()
    
    # Add all handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("setloopduration", setloopduration_command))
    application.add_handler(CommandHandler("setdelay", setdelay_command))
    application.add_handler(CommandHandler("stopallloops", stopallloops_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    # Run bot and web server
    async with application:
        await application.start()
        await application.updater.start_polling()
        web_task = asyncio.create_task(web_server())
        await web_task
        await application.updater.stop()
        await application.stop()

if __name__ == "__main__":
    if loops_collection is not None:
        # On startup, restart any loops that were active when the bot stopped
        logger.info("Restarting any pending loops from the database...")
        active_loops = list(loops_collection.find({}))
        if active_loops:
            # We need a dummy context to start the loops
            # This is a bit of a hack but necessary for this structure
            TOKEN = os.environ.get("TOKEN")
            dummy_app = Application.builder().token(TOKEN).build()
            for loop in active_loops:
                logger.info(f"Re-activating loop: {loop['_id']}")
                asyncio.create_task(loop_processor(dummy_app, loop['_id']))
                
    asyncio.run(main())
