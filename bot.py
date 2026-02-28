import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
import random
from datetime import datetime
import aiohttp

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
TEMP_DIR = Path(tempfile.gettempdir()) / "cc_scraper_bot"
TEMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
# SCRAPER HELPERS
# ────────────────────────────────────────────────
async def process_file(
    file_path: Path,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_bin: str = None
) -> tuple[list[str], int]:
    results = []

    text = file_path.read_text(encoding="utf-8", errors="replace")

    pattern = re.compile(r'(\d{16})\s*[|]\s*(\d{1,2})\s*[|]\s*(\d{2,4})\s*[|]\s*(\d{3,4})', re.I)
    matches = pattern.findall(text)

    current_yy = datetime.now().year % 100

    for card, mm, yy, cvv in matches:
        if len(card) != 16 or not card.isdigit():
            continue

        mm_clean = mm.zfill(2)
        yy_clean = yy[-2:].zfill(2)
        yy_int = int(yy_clean)

        cvv_clean = cvv.strip()
        if not cvv_clean.isdigit() or len(cvv_clean) not in (3, 4):
            continue

        if yy_int < current_yy - 6:
            continue

        if target_bin and not card.startswith(target_bin):
            continue

        results.append(f"{card}|{mm_clean}|{yy_clean}|{cvv_clean}\n")

    return results, len(results)

# ────────────────────────────────────────────────
# CC GENERATOR + BIN HELPERS
# ────────────────────────────────────────────────
def generate_luhn_card(prefix: str) -> str:
    clean = ''.join(c for c in prefix.upper() if c.isdigit() or c == 'X')
    is_amex = clean.startswith(('34', '37')) or len(clean) == 15
    target_len = 15 if is_amex else 16

    card = []
    for c in prefix.upper():
        if c == 'X':
            card.append(str(random.randint(0, 9)))
        elif c.isdigit():
            card.append(c)

    while len(card) < target_len - 1:
        card.append(str(random.randint(0, 9)))
    if len(card) > target_len - 1:
        card = card[:target_len - 1]

    digits = [int(d) for d in card]
    total = sum(digits[-1::-2]) + sum(sum(divmod(d*2, 10)) for d in digits[-2::-2])
    check_digit = (10 - (total % 10)) % 10
    return ''.join(map(str, card)) + str(check_digit)

async def get_bin_info(bin6: str) -> str:
    if len(bin6) != 6 or not bin6.isdigit():
        return "Invalid BIN format"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(f"https://lookup.binlist.net/{bin6}") as resp:
                if resp.status != 200:
                    return "BIN not found"
                data = await resp.json()

        scheme = data.get("scheme", "Unknown").upper()
        ctype = data.get("type", "Unknown").upper()
        brand = data.get("brand", "Unknown")
        bank = data.get("bank", {}).get("name", "Unknown Bank")
        country = data.get("country", {}).get("name", "Unknown")
        emoji = data.get("country", {}).get("emoji", "")

        return (
            f"Scheme: {scheme}\n"
            f"Type:   {ctype}\n"
            f"Brand:  {brand}\n"
            f"Bank:   {bank}\n"
            f"Country: {country} {emoji}"
        )
    except:
        return "BIN lookup failed"

# ────────────────────────────────────────────────
# COMMANDS
# ────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "CC Tools Bot\n\n"
        "/scrap → extract CCs (txt file only)\n"
        "/gen   → generate 10 cards (text copy-paste)\n"
        "/bin   → BIN info\n"
        "/chk   → basic check (txt files)"
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Send .txt file only.")
        return

    await update.message.reply_text(f"Got {doc.file_name}\nReply with /scrap or /chk")

async def scrap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("Reply to .txt file with /scrap")
        return

    doc = update.message.reply_to_message.document
    if not doc.file_name.lower().endswith('.txt'):
        await update.message.reply_text("Only .txt files")
        return

    file = await doc.get_file()
    temp_path = TEMP_DIR / f"scrap_{doc.file_id}.txt"
    await file.download_to_drive(custom_path=temp_path)

    if temp_path.stat().st_size == 0:
        await update.message.reply_text("Empty file")
        temp_path.unlink(missing_ok=True)
        return

    results, valid_count = await process_file(temp_path, update, context)

    if results:
        out_name = f"extracted_{doc.file_name}"
        out_path = TEMP_DIR / out_name
        out_path.write_text("".join(results))

        await update.message.reply_document(
            document=out_path.open("rb"),
            caption=f"{valid_count} cards extracted"
        )
        out_path.unlink(missing_ok=True)
    else:
        await update.message.reply_text("No valid 16-digit cards found")

    temp_path.unlink(missing_ok=True)

async def gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    input_str = " ".join(context.args).strip() if context.args else ""

    await update.message.reply_text("Generating 10 cards...")

    parts = re.split(r'[\|\/]', input_str.replace(' ', ''))
    card_template = parts[0].strip() if parts else ""
    exp_given = parts[1].strip() if len(parts) > 1 else None
    cvv_given = parts[2].strip() if len(parts) > 2 else None

    if card_template.lower() in ("amex", "americanexpress"):
        card_template = random.choice(["34", "37"]) + ''.join(str(random.randint(0,9)) for _ in range(4))

    if not card_template:
        card_template = str(random.randint(400000, 499999))

    cards = []
    bin6 = ''.join(c for c in card_template if c.isdigit())[:6]
    current_yy = datetime.now().year % 100

    for _ in range(10):
        card = generate_luhn_card(card_template)

        if exp_given:
            exp_clean = re.sub(r'\D', '', exp_given)
            mm = exp_clean[:2].zfill(2)
            yy = exp_clean[-2:].zfill(2)
        else:
            mm = str(random.randint(1, 12)).zfill(2)
            yy = str(random.randint(current_yy + 1, current_yy + 6)).zfill(2)

        is_amex = card.startswith(('34', '37')) or len(card) == 15
        if cvv_given:
            cvv = cvv_given.zfill(4 if is_amex else 3)[:4 if is_amex else 3]
        else:
            cvv = f"{random.randint(0, 9999 if is_amex else 999):0{4 if is_amex else 3}d}"

        cards.append(f"{card}|{mm}|{yy}|{cvv}")

    bin_details = await get_bin_info(bin6)

    output = f"""10 cards generated
BIN: {bin6}  {'(Amex 15-digit)' if len(cards[0].split('|')[0]) == 15 else '(16-digit)'}

{bin_details}
{"\n".join(cards)}
Copy the block above (long press → Copy)"""
    await update.message.reply_text(output, parse_mode="Markdown")

async def bin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /bin 625814  or  /bin 6258143602134128")
        return

    raw = context.args[0]
    bin6 = ''.join(filter(str.isdigit, raw))[:6]

    if len(bin6) != 6:
        await update.message.reply_text("Need 6 digits")
        return

    await update.message.reply_text(f"Looking up {bin6}...")
    info = await get_bin_info(bin6)
    await update.message.reply_text(f"**BIN {bin6}**\n\n{info}")

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    logger = logging.getLogger(__name__)

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN missing!")
        exit(1)

    logger.info("Bot starting...")

    try:
        application = Application.builder().token(token).build()

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("scrap", scrap_command))
        application.add_handler(CommandHandler("gen", gen_command))
        application.add_handler(CommandHandler("bin", bin_command))

        application.add_handler(MessageHandler(filters.Document.TEXT, handle_document))

        logger.info("Starting polling...")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            poll_interval=0.8,
            timeout=25
        )

    except Exception as e:
        logger.exception("Crash")
        raise
