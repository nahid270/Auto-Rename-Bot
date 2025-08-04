import os
import re
import asyncio
import logging
import motor.motor_asyncio
import requests
from pyrogram import Client, filters
from flask import Flask, request, jsonify

# =======================
# Environment variables
# =======================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))  # Telegram API ID (Telegram apps > my.telegram.org)
API_HASH = os.getenv("TELEGRAM_API_HASH")        # Telegram API HASH
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Ex: https://yourapp.onrender.com/webhook
PAYMENT_LINK = os.getenv("PAYMENT_LINK") or "https://yourpaymentlink.example.com"

if not all([BOT_TOKEN, API_ID, API_HASH, TMDB_API_KEY, MONGODB_URI, WEBHOOK_URL]):
    raise Exception("Please set all required environment variables!")

# =======================
# Initialize Pyrogram client
# =======================
app = Flask(__name__)
bot = Client("movie_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# =======================
# Initialize MongoDB client (Async)
# =======================
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = mongo_client["moviebot_db"]
search_log_collection = db["search_logs"]

# =======================
# Movie renaming and parsing function
# =======================
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
    if re.search(r"\bBEN\b", name_part, re.IGNORECASE):
        lang = "Bengali"
    elif re.search(r"\bHINDI\b", name_part, re.IGNORECASE):
        lang = "Hindi"
    elif re.search(r"\bENG\b", name_part, re.IGNORECASE):
        lang = "English"

    dub = ""
    if re.search(r"\bDUB\b", name_part, re.IGNORECASE):
        dub = "Dubbed"

    extras = []
    for token in re.findall(r"[A-Za-z0-9\-]{2,}", name_part):
        upper = token.upper()
        if upper in {quality.upper(), source.upper(), "BEN", "HINDI", "ENG", "DUB"}:
            continue
        if re.fullmatch(r"(19|20)\d{2}", token):
            continue
        extras.append(upper)

    title_raw = name_part
    if year:
        title_raw = name_part.split(year)[0]
    for piece in [quality, source, "BEN", "HINDI", "ENG", "DUB"]:
        title_raw = re.sub(re.escape(piece), "", title_raw, flags=re.IGNORECASE)
    for extra in extras:
        title_raw = re.sub(re.escape(extra), "", title_raw, flags=re.IGNORECASE)
    title = clean_title(title_raw)

    parts = []
    if title:
        parts.append(title)
    if year:
        parts.append(f"({year})")
    if quality:
        parts.append(quality)
    if source:
        parts.append(source)
    if lang:
        parts.append(lang)
    if dub:
        parts.append(dub)
    if extras:
        parts.extend(extras)

    new_name = " ".join(parts)
    if ext:
        new_name = f"{new_name}.{ext}"
    return new_name

# =======================
# Fetch movie details from TMDb API
# =======================
def fetch_movie_details(title, year=None):
    try:
        search_url = f"https://api.themoviedb.org/3/search/movie"
        params = {
            "api_key": TMDB_API_KEY,
            "query": title,
        }
        if year:
            params["year"] = year
        res = requests.get(search_url, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
        if data.get("results"):
            movie = data["results"][0]
            movie_id = movie["id"]
            details_url = f"https://api.themoviedb.org/3/movie/{movie_id}"
            details_params = {
                "api_key": TMDB_API_KEY,
                "language": "en-US",
            }
            details_res = requests.get(details_url, params=details_params, timeout=10)
            details_res.raise_for_status()
            details = details_res.json()
            return details
    except Exception as e:
        print(f"TMDb fetch error: {e}")
    return None

# =======================
# Build caption message
# =======================
def build_caption(movie_details, pretty_name):
    if not movie_details:
        return f"🎬 {pretty_name}\n\n❌ Details not found.\n\n💰 Payment: {PAYMENT_LINK}"

    title = movie_details.get("title", pretty_name)
    year = movie_details.get("release_date", "")[:4]
    rating = movie_details.get("vote_average", "N/A")
    overview = movie_details.get("overview", "No description available.")
    lang = movie_details.get("original_language", "").upper()
    poster_path = movie_details.get("poster_path", "")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

    caption = f"""🎬 **{title} ({year})**

⭐ Rating: {rating} / 10  
📝 Overview: {overview}

💰 Payment / Premium: [Click Here]({PAYMENT_LINK})

\n\n© Bot by YourName"""

    return caption, poster_url

# =======================
# MongoDB: log user search
# =======================
async def log_search(user_id, query):
    await search_log_collection.insert_one({
        "user_id": user_id,
        "query": query,
        "timestamp":  asyncio.get_event_loop().time()
    })

# =======================
# Pyrogram message handler
# =======================
@bot.on_message(filters.text & ~filters.edited)
async def handle_movie_request(client, message):
    query = message.text.strip()
    pretty_name = parse_and_rename(query + ".mp4")  # add .mp4 to help parsing

    # Log search
    await log_search(message.from_user.id, query)

    # Try extract title & year for TMDb fetch
    title_only = re.sub(r"\.(19|20)\d{2}\b.*", "", query)  # before year
    year_match = re.search(r"(19|20)\d{2}", query)
    year = year_match.group(0) if year_match else None

    # Fetch details
    details = await asyncio.to_thread(fetch_movie_details, title_only, year)

    caption, poster_url = build_caption(details, pretty_name)

    if poster_url:
        await message.reply_photo(poster_url, caption=caption, parse_mode="markdown")
    else:
        await message.reply_text(caption, parse_mode="markdown")

# =======================
# Flask Webhook setup
# =======================
@app.route("/webhook", methods=["POST"])
def webhook_handler():
    update = request.get_json(force=True)
    asyncio.run(bot.process_new_updates([update]))
    return jsonify({"status": "ok"})

# =======================
# Set webhook on start
# =======================
async def set_webhook():
    await bot.set_webhook(WEBHOOK_URL)

# =======================
# Main entry point
# =======================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(bot.start())
    loop.run_until_complete(set_webhook())
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
    loop.run_until_complete(bot.idle())
