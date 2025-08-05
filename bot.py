import os
import re
import asyncio
import logging
import motor.motor_asyncio
import requests
from threading import Thread
from flask import Flask
from pyrogram import Client, filters
from pyrogram.enums import ParseMode

# =======================
# Flask App for Render Health Check
# =======================
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is alive and running!"

def run_flask():
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

# =======================
# Environment Variables
# =======================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")
PAYMENT_LINK = os.getenv("PAYMENT_LINK") or "https://yourpaymentlink.example.com"
BOT_OWNER_NAME = os.getenv("BOT_OWNER_NAME") or "YourName"

if not all([BOT_TOKEN, API_ID, API_HASH, TMDB_API_KEY, MONGODB_URI]):
    logging.critical("CRITICAL: One or more environment variables are missing!")
    raise SystemExit("Missing required environment variables. Bot cannot start.")

# =======================
# Initialize Clients
# =======================
# Pyrogram Client (Intents ছাড়া)
# এটি সবচেয়ে সামঞ্জস্যপূর্ণ (compatible) উপায়
bot = Client(
    "movie_bot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH,
    parse_mode=ParseMode.MARKDOWN
)

# MongoDB Async Client
try:
    mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
    db = mongo_client["moviebot_db"]
    search_log_collection = db["search_logs"]
    logging.info("Successfully connected to MongoDB.")
except Exception as e:
    logging.critical(f"CRITICAL: Could not connect to MongoDB! Error: {e}")
    raise SystemExit("Database connection failed. Bot cannot start.")

# =======================
# Helper Functions
# =======================
def clean_title(raw_text):
    title = raw_text.replace(".", " ").strip()
    return " ".join(word.capitalize() for word in title.split())

def parse_and_rename(filename):
    name_part, _, ext = filename.rpartition(".")
    
    year_match = re.search(r"\b(19|20)\d{2}\b", name_part)
    year = year_match.group(0) if year_match else ""

    quality_match = re.search(r"\b(360p|480p|720p|1080p|2160p|4k)\b", name_part, re.IGNORECASE)
    quality = quality_match.group(0).upper() if quality_match else ""

    tags_to_remove = [quality, year]
    title_raw = name_part
    for tag in tags_to_remove:
        if tag:
            title_raw = re.sub(r'\b' + re.escape(tag) + r'\b', "", title_raw, flags=re.IGNORECASE)

    title = clean_title(title_raw)
    
    parts = [part for part in [title, f"({year})" if year else "", quality] if part]
    new_name = " ".join(parts)
    return new_name if new_name else clean_title(name_part)

async def fetch_movie_details(title, year=None):
    search_url = "https://api.themoviedb.org/3/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "en-US"}
    if year:
        params["year"] = year

    try:
        def do_request():
            return requests.get(search_url, params=params, timeout=10)
        
        res = await asyncio.to_thread(do_request)
        res.raise_for_status()
        data = res.json()

        if data.get("results"):
            movie_id = data["results"][0]["id"]
            details_url = f"https://api.themoviedb.org/3/movie/{movie_id}"
            
            def do_details_request():
                 return requests.get(details_url, params={"api_key": TMDB_API_KEY}, timeout=10)

            details_res = await asyncio.to_thread(do_details_request)
            details_res.raise_for_status()
            return details_res.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"TMDb API request failed: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred during TMDb fetch: {e}")
    return None

def build_caption(details, pretty_name):
    if not details:
        caption = f"🎬 **{pretty_name}**\n\n❌ Details not found.\n\n💰 **Payment / Premium:** [Click Here]({PAYMENT_LINK})"
        return caption, None

    title = details.get("title", pretty_name)
    year = (details.get("release_date") or "")[:4]
    rating = details.get("vote_average", "N/A")
    overview = details.get("overview", "No description available.")
    poster_path = details.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

    try:
        rating_text = f"{float(rating):.1f}/10"
    except (ValueError, TypeError):
        rating_text = "N/A"

    caption = (
        f"🎬 **{title} ({year})**\n\n"
        f"⭐ **Rating:** `{rating_text}`\n"
        f"📝 **Overview:** {overview[:250] + '...' if len(overview) > 250 else overview}\n\n"
        f"💰 **Payment / Premium:** [Click Here]({PAYMENT_LINK})\n\n"
        f"© Bot by {BOT_OWNER_NAME}"
    )
    return caption, poster_url

async def log_search(user_id, query):
    try:
        await search_log_collection.insert_one({
            "user_id": user_id,
            "query": query,
            "timestamp": asyncio.get_event_loop().time()
        })
    except Exception as e:
        logging.error(f"Failed to log search for user {user_id}: {e}")

# =======================
# Pyrogram Message Handler
# =======================
@bot.on_message(filters.text & filters.group & ~filters.bot)
async def handle_movie_request(client, message):
    query = message.text.strip()
    logging.info(f"Received query '{query}' from user {message.from_user.id} in group {message.chat.id}")
    await log_search(message.from_user.id, query)

    pretty_name = parse_and_rename(query + ".mkv")
    year_match = re.search(r"\b(19|20)\d{2}\b", query)
    year = year_match.group(0) if year_match else None
    title_only = re.sub(r'[\.\[\(\{](19|20)\d{2}[\.\]\)\}]?', '', query, flags=re.IGNORECASE).strip()
    
    details = await fetch_movie_details(clean_title(title_only), year)
    caption, poster_url = build_caption(details, pretty_name)

    try:
        if poster_url:
            await message.reply_photo(photo=poster_url, caption=caption)
        else:
            await message.reply_text(text=caption, disable_web_page_preview=True)
        logging.info(f"Successfully replied to query '{query}'")
    except Exception as e:
        logging.error(f"Failed to send reply for query '{query}'. Error: {e}")
        await message.reply_text(text=caption, disable_web_page_preview=True)

# =======================
# Main Execution Block
# =======================
async def main():
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    await bot.start()
    logging.info("Pyrogram client started. Bot is now online.")
    
    await asyncio.Future()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot shutting down...")
