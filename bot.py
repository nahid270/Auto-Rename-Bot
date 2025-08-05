import os
import re
import logging
import asyncio
import requests
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
import motor.motor_asyncio

# =======================
# Environment Variables
# =======================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")
PAYMENT_LINK = os.getenv("PAYMENT_LINK", "https://yourpaymentlink.example.com")
BOT_OWNER_NAME = os.getenv("BOT_OWNER_NAME", "MovieBot Owner")

if not all([BOT_TOKEN, API_ID, API_HASH, TMDB_API_KEY, MONGODB_URI]):
    raise SystemExit("❌ One or more environment variables are missing.")

# =======================
# Initialize Bot
# =======================
bot = Client(
    "moviebot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH,
    parse_mode=ParseMode.MARKDOWN
)

# =======================
# MongoDB Setup
# =======================
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = mongo_client["moviebot_db"]
search_log_collection = db["search_logs"]

# =======================
# Helper Functions
# =======================
def clean_title(text):
    text = text.replace(".", " ").strip()
    return " ".join(w.capitalize() for w in text.split())

def parse_and_rename(filename):
    name, _, _ = filename.rpartition(".")
    year_match = re.search(r"\b(19|20)\d{2}\b", name)
    year = year_match.group(0) if year_match else ""
    quality_match = re.search(r"\b(360p|480p|720p|1080p|2160p|4k)\b", name, re.I)
    quality = quality_match.group(0).upper() if quality_match else ""
    for tag in [year, quality]:
        name = re.sub(r'\b' + re.escape(tag) + r'\b', '', name, flags=re.I)
    title = clean_title(name)
    return " ".join([title, f"({year})" if year else "", quality]).strip()

async def fetch_movie_details(title, year=None):
    try:
        def get_search():
            return requests.get("https://api.themoviedb.org/3/search/movie", params={
                "api_key": TMDB_API_KEY,
                "query": title,
                "year": year,
                "language": "en-US"
            }, timeout=10)
        response = await asyncio.to_thread(get_search)
        data = response.json()
        if data.get("results"):
            movie_id = data["results"][0]["id"]
            def get_detail():
                return requests.get(f"https://api.themoviedb.org/3/movie/{movie_id}", params={
                    "api_key": TMDB_API_KEY
                }, timeout=10)
            detail_res = await asyncio.to_thread(get_detail)
            return detail_res.json()
    except Exception as e:
        logging.warning(f"TMDb API failed: {e}")
    return None

def build_caption(details, fallback_title):
    if not details:
        return f"🎬 **{fallback_title}**\n\n❌ No details found.\n\n💰 [Payment]({PAYMENT_LINK})", None
    title = details.get("title", fallback_title)
    year = (details.get("release_date") or "")[:4]
    rating = details.get("vote_average", "N/A")
    overview = details.get("overview", "No description available.")
    poster = details.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster}" if poster else None
    try:
        rating_text = f"{float(rating):.1f}/10"
    except:
        rating_text = "N/A"
    caption = (
        f"🎬 **{title} ({year})**\n"
        f"⭐ **Rating:** `{rating_text}`\n"
        f"📝 **Overview:** {overview[:300] + '...' if len(overview) > 300 else overview}\n\n"
        f"💰 [Premium Access]({PAYMENT_LINK})\n\n"
        f"© Bot by {BOT_OWNER_NAME}"
    )
    return caption, poster_url

async def log_search(user_id, query):
    await search_log_collection.insert_one({
        "user_id": user_id,
        "query": query,
        "timestamp": asyncio.get_event_loop().time()
    })

# =======================
# Handlers
# =======================
@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    await message.reply_text(
        f"👋 Hello {message.from_user.mention}!\n\n"
        "Send any movie name in group or here to get info from TMDb 📽️"
    )

@bot.on_message(filters.text & filters.group & ~filters.bot)
async def group_handler(client, message):
    query = message.text.strip()
    await log_search(message.from_user.id, query)
    pretty_name = parse_and_rename(query + ".mkv")
    year_match = re.search(r"\b(19|20)\d{2}\b", query)
    year = year_match.group(0) if year_match else None
    title_cleaned = re.sub(r'[\.\[\(\{](19|20)\d{2}[\.\]\)\}]?', '', query, flags=re.I).strip()
    details = await fetch_movie_details(clean_title(title_cleaned), year)
    caption, poster_url = build_caption(details, pretty_name)
    try:
        if poster_url:
            await message.reply_photo(photo=poster_url, caption=caption)
        else:
            await message.reply_text(text=caption, disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"Reply failed: {e}")
        await message.reply_text(text=caption)

# =======================
# Start the Bot
# =======================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bot.run()
