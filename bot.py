import os
import re
import asyncio
import logging
import motor.motor_asyncio
import requests
from pyrogram import Client, filters
from flask import Flask  # Flask ইম্পোর্ট করা হয়েছে

# =======================
# Flask App ইনিশিয়ালাইজ করা হচ্ছে
# =======================
app = Flask(__name__)

# Render Health Check এর জন্য একটি রুট
@app.route('/')
def index():
    return "Bot is alive!"

def run_flask_app():
    # Render দ্বারা নির্ধারিত PORT এ Flask অ্যাপ চালানো হবে
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

# =======================
# Environment variables
# =======================
# (এই অংশটি অপরিবর্তিত)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")
PAYMENT_LINK = os.getenv("PAYMENT_LINK") or "https://yourpaymentlink.example.com"
BOT_OWNER_NAME = os.getenv("BOT_OWNER_NAME") or "YourName"

if not all([BOT_TOKEN, API_ID, API_HASH, TMDB_API_KEY, MONGODB_URI]):
    raise Exception("Please set all required environment variables!")

# =======================
# Pyrogram & MongoDB Client
# =======================
# (এই অংশটি অপরিবর্তিত)
bot = Client("movie_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = mongo_client["moviebot_db"]
search_log_collection = db["search_logs"]

# =======================
# Helper Functions (parse, fetch, build, log)
# =======================
# (আপনার সব helper ফাংশন এখানে অপরিবর্তিত থাকবে)
def clean_title(raw):
    title = raw.replace(".", " ").strip()
    return " ".join(word.capitalize() for word in title.split())

def parse_and_rename(filename):
    name_part, dot, ext = filename.rpartition(".")
    ext = ext if ext else ""

    year_match = re.search(r"\b(19|20)\d{2}\b", name_part)
    year = year_match.group(0) if year_match else ""

    quality_match = re.search(r"\b(360p|480p|720p|1080p|2160p|4k)\b", name_part, re.IGNORECASE)
    quality = quality_match.group(0).upper() if quality_match else ""

    source_match = re.search(r"\b(HDRip|WEBRip|BluRay|DVDRip|WEB-DL|HDR|BRRip)\b", name_part, re.IGNORECASE)
    source = source_match.group(0) if source_match else ""

    lang = ""
    if re.search(r"\b(BEN|BENGALI)\b", name_part, re.IGNORECASE):
        lang = "Bengali"
    elif re.search(r"\b(HIN|HINDI)\b", name_part, re.IGNORECASE):
        lang = "Hindi"
    elif re.search(r"\b(ENG|ENGLISH)\b", name_part, re.IGNORECASE):
        lang = "English"

    dub = ""
    if re.search(r"\b(DUB|DUBBED)\b", name_part, re.IGNORECASE):
        dub = "Dubbed"

    title_raw = name_part
    if year:
        title_raw = title_raw.split(year)[0]
    
    tags_to_remove = [quality, source, "BEN", "BENGALI", "HIN", "HINDI", "ENG", "ENGLISH", "DUB", "DUBBED"]
    for tag in tags_to_remove:
        if tag:
            title_raw = re.sub(r'\b' + re.escape(tag) + r'\b', "", title_raw, flags=re.IGNORECASE)
            
    title = clean_title(title_raw)

    parts = [part for part in [title, f"({year})" if year else "", quality, source, lang, dub] if part]
    new_name = " ".join(parts)
    if ext:
        new_name = f"{new_name}.{ext}"
    return new_name

async def fetch_movie_details(title, year=None):
    try:
        search_url = f"https://api.themoviedb.org/3/search/movie"
        params = {"api_key": TMDB_API_KEY, "query": title, "language": "en-US"}
        if year:
            params["year"] = year
            
        def do_request():
            return requests.get(search_url, params=params, timeout=10)

        res = await asyncio.to_thread(do_request)
        res.raise_for_status()
        data = res.json()

        if data.get("results"):
            movie = data["results"][0]
            movie_id = movie["id"]
            details_url = f"https://api.themoviedb.org/3/movie/{movie_id}"
            details_params = {"api_key": TMDB_API_KEY, "language": "en-US"}
            
            def do_details_request():
                return requests.get(details_url, params=details_params, timeout=10)
            details_res = await asyncio.to_thread(do_details_request)

            details_res.raise_for_status()
            return details_res.json()
    except Exception as e:
        logging.error(f"TMDb fetch error: {e}")
    return None

def build_caption(movie_details, pretty_name):
    if not movie_details:
        return f"🎬 **{pretty_name}**\n\n❌ Details not found.\n\n💰 Payment: {PAYMENT_LINK}", None

    title = movie_details.get("title", pretty_name)
    year = (movie_details.get("release_date") or "")[:4]
    rating = movie_details.get("vote_average", "N/A")
    overview = movie_details.get("overview", "No description available.")
    poster_path = movie_details.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

    try:
        rating_text = f"{float(rating):.1f}/10"
    except (ValueError, TypeError):
        rating_text = "N/A"

    caption = f"""🎬 **{title} ({year})**

⭐ **Rating:** `{rating_text}`
📝 **Overview:** {overview[:250] + '...' if len(overview) > 250 else overview}

💰 **Payment / Premium:** [Click Here]({PAYMENT_LINK})

\n\n© Bot by {BOT_OWNER_NAME}"""
    return caption, poster_url

async def log_search(user_id, query):
    try:
        await search_log_collection.insert_one({
            "user_id": user_id,
            "query": query,
            "timestamp":  asyncio.get_event_loop().time()
        })
    except Exception as e:
        logging.error(f"Failed to log search for user {user_id}: {e}")

# =======================
# Pyrogram Message Handler
# =======================
# (এই অংশটি অপরিবর্তিত)
@bot.on_message(filters.text & filters.group)
async def handle_movie_request(client, message):
    query = message.text.strip()
    await log_search(message.from_user.id, query)
    pretty_name = parse_and_rename(query + ".mkv")
    year_match = re.search(r"\b(19|20)\d{2}\b", query)
    year = year_match.group(0) if year_match else None
    title_only = re.sub(r'[\(\[\{]?(19|20)\d{2}[\)\]\}]?', '', query, flags=re.IGNORECASE)
    title_only = re.sub(r'\b(360p|480p|720p|1080p|2160p|4k|HDRip|WEBRip|BluRay|DVDRip|WEB-DL|HDR|BRRip|BEN|HINDI|ENG|DUB)\b', '', title_only, flags=re.IGNORECASE)
    title_only = clean_title(title_only)
    details = await fetch_movie_details(title_only, year)
    caption, poster_url = build_caption(details, pretty_name)
    try:
        if poster_url:
            await message.reply_photo(photo=poster_url, caption=caption)
        else:
            await message.reply_text(text=caption, disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"Failed to send message for query '{query}': {e}")
        await message.reply_text(text=caption, disable_web_page_preview=True)

# =======================
# Main entry point (Web Service এর জন্য পরিবর্তিত)
# =======================
async def main():
    # Pyrogram বট এবং Flask অ্যাপ একসাথে চালানো হবে
    # Flask অ্যাপটি একটি আলাদা থ্রেডে চলবে যাতে এটি Pyrogram কে ব্লক না করে
    from threading import Thread
    
    flask_thread = Thread(target=run_flask_app)
    flask_thread.daemon = True
    flask_thread.start()

    await bot.start()
    logging.info("Pyrogram client started successfully!")
    
    # প্রোগ্রামটিকে অনির্দিষ্টকালের জন্য চলতে দেওয়া হবে
    await asyncio.Future()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    try:
        logging.info("Starting bot...")
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot shutdown requested.")
