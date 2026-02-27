import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

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
TOKEN = os.getenv("BOT_TOKEN")          # set in Railway variables
TEMP_DIR = Path(tempfile.gettempdir()) / "cc_scraper_bot"
TEMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────
def normalize_exp(exp: str) -> str:
    exp = exp.strip().replace("/", "|").replace(" ", "")
    if "|" not in exp:
        return exp
    month, year = exp.split("|", 1)
    month = month.zfill(2)
    year = year[-2:].zfill(2) if len(year) > 2 else year.zfill(2)
    return f"{month}|{year}"
    

async def process_file(
    file_path: Path,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_bin: str = None
) -> tuple[list[str], int, int]:
    results = []
    rejected_samples = []  # debug help
    
    text = file_path.read_text(encoding="utf-8", errors="replace")
    
    # Quick preview
    preview = text[:1200].replace("`", "'").strip()
    await update.message.reply_text(
        f"Preview (first \~1200 chars):\n\n{preview}\n\nTotal chars: {len(text)}"
    )
    
    # Strict 16-digit card pattern
    pattern = re.compile(
        r'\b(\d{16})\s*[\|\|]\s*(\d{1,2})\s*[\|\|]\s*(\d{2,4})\s*[\|\|]\s*(\d{3,4})\b',
        re.IGNORECASE
    )
    
    matches = pattern.findall(text)
    
    await update.message.reply_text(f"Found {len(matches)} potential 16-digit pipe matches")
    
    processed = 0  # valid ones
    total_potential = len(matches)
    
    current_yy = datetime.now().year % 100
    
    for card, mm, yy, cvv in matches:
        card = card.strip()
        # Enforce exactly 16 digits - no more, no less
        if len(card) != 16 or not card.isdigit():
            if len(rejected_samples) < 3:
                rejected_samples.append(f"Rejected (not 16 digits): {card}|{mm}|{yy}|{cvv}")
            continue
        
        mm_clean = mm.zfill(2)
        
        # Year: always last 2 digits
        yy_clean = yy[-2:].zfill(2)
        
        cvv_clean = cvv.strip()
        if not cvv_clean.isdigit() or len(cvv_clean) not in (3, 4):
            if len(rejected_samples) < 3:
                rejected_samples.append(f"Rejected (bad CVV): {card}|{mm}|{yy}|{cvv}")
            continue
        
        # Optional: skip obviously expired (comment out if you want all)
        yy_int = int(yy_clean)
        if yy_int < current_yy - 6:
            if len(rejected_samples) < 3:
                rejected_samples.append(f"Rejected (old expiry): {card}|{mm}|{yy}|{cvv}")
            continue
        
        if target_bin and not card.startswith(target_bin):
            continue
        
        results.append(f"{card}|{mm_clean}|{yy_clean}|{cvv_clean}\n")
        processed += 1
        
        # Progress every 300 valid or at end
        if processed % 300 == 0 or processed == total_potential:
            pct = round((processed / total_potential) * 100, 1) if total_potential > 0 else 0
            await update.message.reply_text(
                f"Valid 16-digit cards: {processed}/{total_potential} ({pct}%)\n"
                f"After BIN filter: {len(results)}"
            )
            await asyncio.sleep(0.1)
    
    # Show why many were dropped if ratio bad
    if processed < total_potential * 0.2 and rejected_samples:  # less than 20% valid
        await update.message.reply_text(
            "Low valid count — sample rejected lines:\n\n" +
            "\n".join(rejected_samples[:5]) +
            f"\n\n(and {len(rejected_samples)} more similar...)"
        )
    
    if not results:
        await update.message.reply_text(
            "No **16-digit** cards passed validation.\n"
            "→ See rejected samples above.\n"
            "→ If preview shows valid 16-digit lines but nothing extracted → paste 5-10 example lines here."
        )
    
    return results, processed, len(text.splitlines())


async def send_result_file(
    update: Update,
    results: list[str],
    original_filename: str,
    mode: str = "full"
):
    if not results:
        await update.message.reply_text("No valid cards found.")
        return

    out_name = f"extracted_{mode}_{original_filename}"
    out_path = TEMP_DIR / out_name

    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(results)

    await update.message.reply_document(
        document=out_path.open("rb"),
        caption=f"Done! Found {len(results)} cards."
    )

    out_path.unlink(missing_ok=True)  # cleanup


# ────────────────────────────────────────────────
# New dedicated /bin handler
# ────────────────────────────────────────────────
async def bin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1 or not context.args[0].isdigit() or len(context.args[0]) != 6:
        await update.message.reply_text("Usage: /bin 400022   (reply to a .txt file)")
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("You must reply to a .txt file message with /bin XXXXXX")
        return

    target_bin = context.args[0]
    mode = f"bin_{target_bin}"

    doc = update.message.reply_to_message.document
    file = await doc.get_file()
    
    temp_path = TEMP_DIR / f"{doc.file_id}_{doc.file_name}"
    
    await update.message.reply_text(f"Downloading file for BIN {target_bin} scan...")
    await file.download_to_drive(custom_path=temp_path)

    if temp_path.stat().st_size == 0:
        await update.message.reply_text("Downloaded file is empty — upload failed.")
        temp_path.unlink(missing_ok=True)
        return

    await update.message.reply_text("Starting BIN-specific extraction...")

    results, processed, total = await process_file(temp_path, update, context, target_bin=target_bin)

    await send_result_file(update, results, doc.file_name, mode)

    temp_path.unlink(missing_ok=True)


# ────────────────────────────────────────────────
# Handlers
# ────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a .txt file with CCs in format:\n"
        "card|mm|yyyy|cvv   or   card|mm|yy|cvv\n\n"
        "Then reply to that file message with:\n"
        "/scrap          → extract all\n"
        "/bin 400022     → only BIN 400022\n\n"
        "I'll show progress and send you the result file."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Please send a .txt file.")
        return

    await update.message.reply_text(
        f"Received {doc.file_name} ({doc.file_size / 1024:.1f} KB)\n"
        "Now reply to THIS message with /scrap or /bin XXXXXX"
    )


async def scrap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("Reply to a .txt file message with /scrap")
        return

    # No BIN filtering here
    target_bin = None
    mode = "full"

    # ... rest of the function exactly the same (download, process_file with target_bin=None, send)


# ────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────
def main():
    if not TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.TEXT, handle_document))
    application.add_handler(CommandHandler("scrap", scrap_command))   # keep full scrape
    application.add_handler(CommandHandler("bin", bin_command))       # new dedicated BIN command
    # /bin is also handled in scrap_command when args present

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
