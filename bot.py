import logging
import json
import asyncio
import os
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# --- Basic Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Mount Path for Render Disk. If not using Render Disk,
# it will create the file in the current directory.
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
        # If the file doesn't exist, create a default state
        logger.warning("State file not found. Creating a default state.")
        return {
            "is_running": False,
            "delay_seconds": 30, # Default 30 seconds
        }

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
    if update.message.chat.type == 'private':
        return True # No need to check for admin in private chat
    
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    # Attempt to use cached admin list for better performance
    if 'admins' not in context.chat_data:
        logger.info(f"Fetching admins for chat {chat_id}")
        admins = await context.bot.get_chat_administrators(chat_id)
        context.chat_data['admins'] = [admin.user.id for admin in admins]
    
    return user_id in context.chat_data.get('admins', [])


# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a help message on /start or /help."""
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return
    
    help_text = (
        "Hello! I am a Content Scrubber Bot.\n\n"
        "I delete every message in the group after a set delay and repost it on my behalf. This hides the name of the original sender.\n\n"
        "**Admin Commands:**\n"
        "`/setdelay <seconds>` - Set the time after which a message is deleted/reposted. (e.g., `/setdelay 15`)\n\n"
        "`/startscrub` - Start the delete/repost process.\n\n"
        "`/stopscrub` - Stop this process.\n\n"
        "`/status` - Check the bot's current status."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def setdelay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the delay time."""
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return

    try:
        delay = int(context.args[0])
        if delay < 5 or delay > 300: # Limit delay from 5 seconds to 5 minutes
            await update.message.reply_text("❗Delay must be between 5 and 300 seconds.")
            return
            
        state = load_state()
        state['delay_seconds'] = delay
        save_state(state)
        await update.message.reply_text(f"✅ Delay time has been set to **{delay} seconds**.", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text("Incorrect format! Please use it like this: `/setdelay 30`")

async def startscrub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the delete/repost process."""
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return

    state = load_state()
    state['is_running'] = True
    save_state(state)
    await update.message.reply_text("🚀 **Scrubber process has been started!**\nAll messages will now be deleted and reposted after the set delay.", parse_mode='Markdown')

async def stopscrub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stops the process."""
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return
        
    state = load_state()
    state['is_running'] = False
    save_state(state)
    await update.message.reply_text("🛑 **Scrubber process has been stopped.**", parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the bot's current status."""
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Sorry, this command can only be used by admins.")
        return
    
    state = load_state()
    status_text = "🟢 **Running**" if state.get('is_running', False) else "🔴 **Stopped**"
    delay_text = state.get('delay_seconds', 'N/A')
    
    await update.message.reply_text(
        f"**📊 Bot Status**\n\n"
        f"🔹 **Process:** {status_text}\n"
        f"🔹 **Delete/Repost Delay:** **{delay_text} seconds**",
        parse_mode='Markdown'
    )

# --- Message Handler (The main logic) ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles every incoming message."""
    state = load_state()
    
    # Do nothing if the process is stopped
    if not state.get('is_running', False):
        return

    # Ignore messages sent by the bot itself to prevent an infinite loop
    if update.message and update.message.from_user.id == context.bot.id:
        return

    message = update.message
    chat_id = message.chat_id
    message_id = message.message_id
    delay = state.get('delay_seconds', 30)

    # Schedule the job to run in the background after the delay
    context.job_queue.run_once(repost_and_delete, delay, data={'chat_id': chat_id, 'message_id': message_id}, name=str(message_id))

async def repost_and_delete(context: ContextTypes.DEFAULT_TYPE):
    """The actual job that reposts and deletes the message."""
    job = context.job
    chat_id = job.data['chat_id']
    message_id = job.data['message_id']

    try:
        # First, copy the message and send it as the bot
        await context.bot.copy_message(chat_id=chat_id, from_chat_id=chat_id, message_id=message_id)
        logger.info(f"Message {message_id} reposted in chat {chat_id}")
        
        # Then, delete the original message
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Original message {message_id} deleted from chat {chat_id}")

    except BadRequest as e:
        if "message to delete not found" in e.message.lower():
            logger.warning(f"Message {message_id} was already deleted.")
        elif "message to copy not found" in e.message.lower():
             logger.warning(f"Message {message_id} was deleted before it could be copied.")
        else:
            logger.error(f"Error processing message {message_id} in chat {chat_id}: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred with message {message_id} in chat {chat_id}: {e}")


# --- Main Function ---

def main():
    """Starts the bot."""
    # Get the token from environment variables
    TOKEN = os.environ.get("TOKEN")
    
    if not TOKEN:
        logger.error("CRITICAL ERROR: Bot Token not found! Please set the TOKEN environment variable.")
        return

    application = Application.builder().token(TOKEN).build()
    
    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("setdelay", setdelay_command))
    application.add_handler(CommandHandler("startscrub", startscrub_command))
    application.add_handler(CommandHandler("stopscrub", stopscrub_command))
    application.add_handler(CommandHandler("status", status_command))
    
    # Message Handler
    # Handles all messages that are not commands
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    # Run the bot
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
