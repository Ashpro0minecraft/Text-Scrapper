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
    total_lines = 0
    processed = 0

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
        total_lines = len(lines)

    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or "|" not in line:
            continue

        try:
            parts = line.split("|")
            if len(parts) < 4:
                continue

            card = parts[0].strip()
            month = parts[1].strip()
            year = parts[2].strip()
            cvv = parts[3].strip()

            if len(card) < 15 or not card.isdigit():
                continue
            if not cvv.isdigit() or len(cvv) not in (3, 4):
                continue

            exp = normalize_exp(f"{month}|{year}")

            if target_bin:
                if not card.startswith(target_bin):
                    continue

            results.append(f"{card}|{exp}|{cvv}\n")
        except:
            pass

        processed += 1
        if processed % 500 == 0 or processed == total_lines:
            pct = round((processed / total_lines) * 100, 1)
            await update.message.reply_text(
                f"Progress: {processed}/{total_lines} lines ({pct}%)"
            )
            await asyncio.sleep(0.1)  # avoid rate limit

    return results, processed, total_lines


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
        await update.message.reply_text("Reply to a .txt file message with this command.")
        return

    target_bin = None
    mode = "full"

    # Check if /bin XXXXXX
    if context.args:
        if len(context.args) == 1 and context.args[0].isdigit() and len(context.args[0]) == 6:
            target_bin = context.args[0]
            mode = f"bin_{target_bin}"
        else:
            await update.message.reply_text("Usage for BIN filter: /bin 400022")
            return

    doc = update.message.reply_to_message.document
    file = await doc.get_file()
    
    temp_path = TEMP_DIR / f"{doc.file_id}_{doc.file_name}"
    
    await update.message.reply_text("Downloading file...")
    await file.download_to_drive(custom_path=temp_path)

    await update.message.reply_text("Starting extraction...")

    results, processed, total = await process_file(temp_path, update, context, target_bin)

    await send_result_file(update, results, doc.file_name, mode)

    temp_path.unlink(missing_ok=True)


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
    application.add_handler(CommandHandler("scrap", scrap_command))
    # /bin is also handled in scrap_command when args present

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
