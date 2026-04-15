import logging
from datetime import datetime, time, timedelta
from typing import Dict, Tuple, Optional
import pytz
import asyncio
import aiohttp

# Bot configuration
BOT_TOKEN = "8606760194:AAGJuUtLfoLF8IuDcjyzzx2WD_4tdBeiEHE"

# Bot state
BOT_ENABLED = True

# Allowed group IDs and their remaining limits
ALLOWED_GROUPS = {
    -5173068786: {"name": "Main Group", "remain": 100, "initial_remain": 100},
    -1003847529783: {"name": "Friend Group", "remain": 50, "initial_remain": 50},
}

# Admin user IDs
ADMIN_USER_IDS = {6457628082, 6457628082}

# API Configuration
API_TIMEOUT = 120  # 2 minutes timeout
API_RETRIES = 3  # Number of retry attempts

# Define API URLs based on region
API_URLS = {
    "ind": "https://new-like-api-by-ajay-two.vercel.app/like",
    "bd": "https://new-like-api-by-ajay-two.vercel.app/like",
}

# Initialize logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class BotData:
    user_daily_usage: Dict[int, Dict[int, bool]] = {}

bot_data = BotData()

async def call_like_api(region: str, uid: str) -> Optional[dict]:
    """Make API request with enhanced error handling."""
    if region not in API_URLS:
        logger.warning(f"Unknown region: {region}")
        return None
    
    api_url = API_URLS[region]
    params = {"uid": uid, "server_name": region}
    
    for attempt in range(API_RETRIES):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    api_url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)
                ) as response:
                    
                    if response.status != 200:
                        logger.warning(f"API {api_url} returned status {response.status}")
                        continue
                        
                    data = await response.json()
                    if data.get("status") == 1:
                        return data
                    
                    logger.warning(f"API {api_url} error: {data.get('message', 'No error message')}")
                    return None
                    
        except asyncio.TimeoutError:
            logger.warning(f"Timeout on attempt {attempt + 1} for {api_url}")
            if attempt < API_RETRIES - 1:
                await asyncio.sleep(5)
            continue
                
        except Exception as e:
            logger.error(f"API call to {api_url} failed: {str(e)}")
            if attempt < API_RETRIES - 1:
                await asyncio.sleep(2)
            continue
    
    return None

def main():
    """Main function to start the bot."""
    try:
        # Try to use pyTelegramBotAPI (simpler)
        import telebot
        from telebot.async_telebot import AsyncTeleBot
        from telebot import asyncio_helper
        
        bot = AsyncTeleBot(BOT_TOKEN)
        logger.info("✅ Using pyTelegramBotAPI library")
        
    except ImportError:
        logger.error("❌ pyTelegramBotAPI not installed!")
        logger.info("👉 Please run: pip install pyTelegramBotAPI")
        return
    
    @bot.message_handler(commands=['start', 'help'])
    async def send_welcome(message):
        welcome_text = (
            "🤖 **Free Fire Like Bot**\n\n"
            "📝 **Commands:**\n"
            "• /like {region} {uid} - Send likes to player\n"
            "• /remain - Check remaining limits\n"
            "• /off - Turn off bot (admin)\n"
            "• /on - Turn on bot (admin)\n\n"
            "🌍 **Available regions:**\n"
            "• ind - India server\n"
            "• bd - Bangladesh server\n\n"
            "📌 **Example:**\n"
            "/like ind 2799233875\n"
            "/like bd 2799233875"
        )
        await bot.reply_to(message, welcome_text, parse_mode="Markdown")
    
    @bot.message_handler(commands=['like', 'likes'])
    async def handle_like(message):
        chat_id = message.chat.id
        user_id = message.from_user.id
        
        # Check if bot is enabled
        if not BOT_ENABLED:
            await bot.reply_to(message, "🔴 Bot is currently disabled by admin.")
            return
            
        # Check if group is allowed
        if chat_id not in ALLOWED_GROUPS:
            await bot.reply_to(message, "❌ This bot only works in specific groups.")
            return
            
        # Parse command
        parts = message.text.split()
        if len(parts) < 3:
            await bot.reply_to(message, 
                "📝 Usage: /like {region} {uid}\n"
                "Example: /like ind 2437607413\n"
                "Example: /like bd 2799233875"
            )
            return
            
        region = parts[1].lower()
        uid = parts[2]
        
        # Check region
        if region not in API_URLS:
            await bot.reply_to(message,
                "❌ Invalid region!\n\n"
                "Available regions:\n"
                "• ind - India server\n"
                "• bd - Bangladesh server"
            )
            return
        
        # Check user permissions
        global bot_data
        
        if not BOT_ENABLED:
            await bot.reply_to(message, "🔴 Bot is currently disabled by admin.")
            return
            
        if user_id in ADMIN_USER_IDS:
            pass  # Admin has unlimited access
        else:
            # Check daily user limit
            if bot_data.user_daily_usage.get(chat_id, {}).get(user_id, False):
                await bot.reply_to(message, "⏳ You can only use this once per day. Try again after 4 AM IST.")
                return
            
            # Check group limit
            if ALLOWED_GROUPS[chat_id]["remain"] <= 0:
                await bot.reply_to(message, "🔴 No remaining uses left. Resets at 4 AM IST.")
                return
        
        # Send processing message
        processing_msg = await bot.reply_to(message, 
            f"⏳ Sending likes to UID {uid} ({region.upper()} server)...\n"
            f"Please wait (may take 1-5 second)"
        )
        
        # Call API
        api_data = await call_like_api(region, uid)
        
        if not api_data:
            await bot.edit_message_text(
                f"❌ Likes already reached for {region.upper()} server:\n"
                "• You have daily 1limit on one uid\n"
                "• try new uid\n"
                "• Try again later with new uid",
                chat_id=chat_id,
                message_id=processing_msg.message_id
            )
            return
        
        # Update usage
        if user_id not in ADMIN_USER_IDS:
            if chat_id not in bot_data.user_daily_usage:
                bot_data.user_daily_usage[chat_id] = {}
            bot_data.user_daily_usage[chat_id][user_id] = True
            ALLOWED_GROUPS[chat_id]["remain"] -= 1
        
        # Format success response
        response_msg = (
f"<b>✅Likes send successfully!</b>\n"
        f"<b>Player Nickname:</b> {api_data['PlayerNickname']}\n"
        f"<b>Player UID:</b> {api_data['UID']}\n"
        f"<b>Payer Region:</b> {region.upper()}\n"
        f"<b>Player Likes before:</b> {api_data['LikesbeforeCommand']}\n"
        f"<b>Player Likes after:</b> {api_data['LikesafterCommand']}\n"
        f"<b>Like Given by bot :</b> {api_data['LikesGivenByAPI']}\n"
        )

        await bot.edit_message_text(
    response_msg,
    chat_id=chat_id,
    message_id=processing_msg.message_id,
    parse_mode="HTML"
       )
    
    @bot.message_handler(commands=['remain'])
    async def handle_remain(message):
        chat_id = message.chat.id
        user_id = message.from_user.id
        
        if chat_id not in ALLOWED_GROUPS:
            await bot.reply_to(message, "❌ This bot only works in specific groups.")
            return
            
        group = ALLOWED_GROUPS[chat_id]
        now = datetime.now(pytz.timezone('Asia/Kolkata'))
        
        # Calculate reset time (4 AM IST)
        reset_time = (now + timedelta(days=1)).replace(
            hour=4, minute=0, second=0, microsecond=0
        ) if now.hour >= 4 else now.replace(
            hour=4, minute=0, second=0, microsecond=0
        )
        
        # Format response
        response = (
            f"📊 Limits for {group['name']}:\n\n"
            f"🔄 Remaining: {group['remain']}/{group['initial_remain']}\n"
            f"⏰ Resets at: {reset_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"👤 Your limit: {'∞ (Admin)' if user_id in ADMIN_USER_IDS else '1/1'}\n\n"
            f"Note: Daily reset at 4 AM IST"
        )
        
        await bot.reply_to(message, response)
    
    @bot.message_handler(commands=['off'])
    async def handle_off(message):
        user_id = message.from_user.id
        
        if user_id not in ADMIN_USER_IDS:
            await bot.reply_to(message, "🚫 Only admins can use this command.")
            return
        
        global BOT_ENABLED
        BOT_ENABLED = False
        logger.info(f"Bot disabled by admin {user_id}")
        await bot.reply_to(message, "🔴 Bot has been disabled for all groups.")
    
    @bot.message_handler(commands=['on'])
    async def handle_on(message):
        user_id = message.from_user.id
        
        if user_id not in ADMIN_USER_IDS:
            await bot.reply_to(message, "🚫 Only admins can use this command.")
            return
        
        global BOT_ENABLED
        BOT_ENABLED = True
        logger.info(f"Bot enabled by admin {user_id}")
        await bot.reply_to(message, "🟢 Bot has been enabled for all groups.")
    
    # Function to reset daily limits
    def reset_limits():
        bot_data.user_daily_usage = {}
        for group_id in ALLOWED_GROUPS:
            ALLOWED_GROUPS[group_id]["remain"] = ALLOWED_GROUPS[group_id]["initial_remain"]
        logger.info("Daily limits reset at 4 AM IST")
    
    # Start the bot
    logger.info("🤖 Bot is starting...")
    
    import asyncio
    asyncio.run(bot.polling())

if __name__ == "__main__":
    main()

from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot2 Running ✅"

def run_web():
    app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    t1 = threading.Thread(target=main)
    t1.start()
    
    run_web()
