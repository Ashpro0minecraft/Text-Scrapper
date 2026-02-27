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
    reject_counts = {"bad_length": 0, "bad_digits": 0, "bad_cvv": 0, "old": 0, "no_bin_match": 0}
    sample_rejects = []

    text = file_path.read_text(encoding="utf-8", errors="replace")

    # Preview (shortened to avoid huge messages)
    preview = text[:800].replace("`", "'").strip()
    await update.message.reply_text(f"Preview snippet:\n{preview}\n\nTotal chars: {len(text)}")

    # Very strict 16-digit only pattern
    pattern = re.compile(r'(\d{16})\s*[|]\s*(\d{1,2})\s*[|]\s*(\d{2,4})\s*[|]\s*(\d{3,4})', re.I)
    matches = pattern.findall(text)

    total_potential = len(matches)
    await update.message.reply_text(f"Found {total_potential} potential 16-digit | matches")

    if total_potential == 0:
        await update.message.reply_text("No matches at all → file does not contain 16-digit|mm|yy|cvv patterns")
        return [], 0, 0

    current_yy = datetime.now().year % 100
    processed = 0

    for i, (card, mm, yy, cvv) in enumerate(matches, 1):
        # Fast skip if not exactly 16
        if len(card) != 16:
            reject_counts["bad_length"] += 1
            continue

        if not card.isdigit():
            reject_counts["bad_digits"] += 1
            if len(sample_rejects) < 3:
                sample_rejects.append(card)
            continue

        mm_clean = mm.zfill(2)

        yy_clean = yy[-2:].zfill(2)
        yy_int = int(yy_clean)

        cvv_clean = cvv.strip()
        if not cvv_clean.isdigit() or len(cvv_clean) not in (3, 4):
            reject_counts["bad_cvv"] += 1
            continue

        # Skip very old if you want (comment out if unwanted)
        if yy_int < current_yy - 6:
            reject_counts["old"] += 1
            continue

        if target_bin and not card.startswith(target_bin):
            reject_counts["no_bin_match"] += 1
            continue

        results.append(f"{card}|{mm_clean}|{yy_clean}|{cvv_clean}\n")
        processed += 1

        # Progress only every 1000 items - prevents spam & rate limits
        if i % 1000 == 0 or i == total_potential:
            valid_pct = round(processed / total_potential * 100, 1) if total_potential > 0 else 0
            await update.message.reply_text(
                f"Scanned {i}/{total_potential} candidates\n"
                f"Valid so far: {processed} ({valid_pct}%)\n"
                f"Kept after BIN filter: {len(results)}"
            )

    # Final summary
    summary = f"Final stats:\nValid extracted: {processed}\nTotal candidates: {total_potential}\n\nRejections:\n"
    for k, v in reject_counts.items():
        if v > 0:
            summary += f"  {k}: {v}\n"

    if sample_rejects:
        summary += f"\nSample bad card numbers: {', '.join(sample_rejects[:3])} ..."

    await update.message.reply_text(summary)

    if not results:
        await update.message.reply_text("Zero cards kept after validation.\nMost likely cause: CVV not 3-4 digits, or year parsing failed, or all rejected by BIN filter.")

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
