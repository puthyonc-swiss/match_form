"""
BKH Match Form Bot
Send a Round PDF → get filled A5 Landscape match forms PDF back
Run: python3 match_form_bot.py
Needs: pip install python-telegram-bot pymupdf playwright
       playwright install chromium
"""

import os, re, tempfile, logging, asyncio, threading
import fitz
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from flask import Flask, request, jsonify
from flask_cors import CORS

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID   = (os.getenv("CHAT_ID") or "").strip()
CHAT_IDS  = [c.strip() for c in CHAT_ID.split(",") if c.strip()]

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ─── PARSE ROUND PDF (ALL PAGES) ─────────────────────────────────────────────

def parse_round_pdf(pdf_path: str):
    doc        = fitz.open(pdf_path)
    round_num  = "?"
    time_start = "--:--"
    event_name = ""
    matches    = []

    for page_num in range(len(doc)):
        page      = doc[page_num]
        full_text = page.get_text("text")
        blocks    = page.get_text("blocks")

        if page_num == 0:
            for block in blocks:
                text = block[4].strip()
                if text and not re.search(r"Round|By:|Team A|Team B|LANE|Pts|vs|#", text, re.IGNORECASE):
                    event_name = text
                    break

            m = re.search(r"Round\s+(\d+).*?Time Start:\s*(\S+)", full_text, re.IGNORECASE)
            if m:
                round_num  = m.group(1)
                time_start = m.group(2)

            m2 = re.search(r"Round\s+(\d+)\s*[·•\-|]\s*(\d{1,2}:\d{2})", full_text)
            if m2:
                round_num  = m2.group(1)
                time_start = m2.group(2)

        for block in blocks:
            text = block[4].strip()
            m = re.match(r"^(\d+)\n(.+?)\nvs\n(.+?)\n(\d+)", text, re.DOTALL)
            if m:
                team_a = m.group(2).strip().replace("\n", " ")
                team_b = m.group(3).strip().replace("\n", " ")
                lane   = m.group(4).strip()
                if team_a.lower() == "team a":
                    continue
                matches.append({"team_a": team_a, "team_b": team_b, "lane": lane})

    return round_num, time_start, event_name, matches


# ─── ASK PLAY TYPE (Step 1) ──────────────────────────────────────────────────

async def ask_play_type(message, context, matches, round_num, time_start, event_name):
    """Save matches to user_data and ask for វិញ្ញាសារ selection."""
    context.user_data["pending_matches"]    = matches
    context.user_data["pending_round_num"]  = round_num
    context.user_data["pending_time_start"] = time_start
    context.user_data["pending_event_name"] = event_name

    keyboard = [
        [
            InlineKeyboardButton("1 vs 1", callback_data="play_1vs1"),
            InlineKeyboardButton("2 vs 2", callback_data="play_2vs2"),
            InlineKeyboardButton("3 vs 3", callback_data="play_3vs3"),
        ]
    ]
    await message.reply_text(
        "Please select វិញ្ញាសារ (Play Type):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ─── CALLBACK: play type selected → ask format ───────────────────────────────

async def callback_play_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    play_map = {
        "play_1vs1": "1 vs 1",
        "play_2vs2": "2 vs 2",
        "play_3vs3": "3 vs 3",
    }
    context.user_data["selected_play_type"] = play_map[query.data]
    await query.edit_message_text(f"Selected: {context.user_data['selected_play_type']}")

    keyboard = [
        [
            InlineKeyboardButton("រូបមន្តវិលជុំ",  callback_data="fmt_roundrobin"),
            InlineKeyboardButton("Swiss-System", callback_data="fmt_swiss"),
        ]
    ]
    await query.message.reply_text(
        "Please select រូបមន្តប្រកួត (Format):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ─── CALLBACK: format selected → generate PDF ────────────────────────────────

async def callback_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    fmt_map = {
        "fmt_roundrobin": "រូបមន្តវិលជុំ",
        "fmt_swiss":      "Swiss-System",
    }
    selected_format    = fmt_map[query.data]
    selected_play_type = context.user_data.get("selected_play_type", "")
    matches            = context.user_data.get("pending_matches", [])
    round_num          = context.user_data.get("pending_round_num", "?")
    time_start         = context.user_data.get("pending_time_start", "--:--")
    event_name         = context.user_data.get("pending_event_name", "")

    await query.edit_message_text(f"Selected: {selected_format}")
    await query.message.reply_text("Generating match forms...")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "match_forms.pdf")
            await generate_pdf_playwright(
                matches, round_num, time_start, event_name,
                selected_play_type, selected_format, output_path
            )
            with open(output_path, "rb") as f:
                await query.message.reply_document(
                    document=f,
                    filename=f"MatchForms_{event_name}_Round{round_num}.pdf",
                    caption=f"{event_name} - Round {round_num} | Time: {time_start} | {len(matches)} forms | A5 Landscape | Ready to print",
                )
    except Exception as e:
        log.exception("Error generating PDF")
        await query.message.reply_text(f"Error: {e}")


# ─── BUILD HTML ───────────────────────────────────────────────────────────────

def make_html(matches, round_num, time_start, event_name, play_type="", fmt=""):
    pages = ""
    for m in matches:
        ends        = "".join([f"<th>{i}</th>" for i in range(1, 14)])
        score_cells = "".join(["<td></td>" for _ in range(13)])
        pages += f"""
        <div class="page">
            <h2>{event_name}</h2>
            <table class="info">
                <tr>
                    <td class="lbl">ព្រឹត្តការណ៍</td>
                    <td class="val">{event_name}</td>
                    <td class="lbl">ទីលាន</td>
                    <td class="val bold">{m['lane']}</td>
                </tr>
                <tr>
                    <td class="lbl">វិញ្ញាសារ</td>
                    <td class="val">{play_type}</td>
                    <td class="lbl">វគ្គ</td>
                    <td class="val">{round_num}</td>
                </tr>
                <tr>
                    <td class="lbl">រូបមន្តប្រកួត</td>
                    <td class="val">{fmt}</td>
                    <td class="lbl">ម៉ោង</td>
                    <td class="val bold">{time_start}</td>
                </tr>
            </table>
            <table class="score">
                <tr>
                    <td class="team-lbl" colspan="2">ក្រុម A</td>
                    <td class="team-name" colspan="11">{m['team_a']}</td>
                    <td class="pts-lbl">ពិន្ទុ</td>
                </tr>
                <tr>
                    {ends}
                    <td rowspan="2" class="pts-box"></td>
                </tr>
                <tr>
                    {score_cells}
                </tr>
            </table>
            <table class="score">
                <tr>
                    <td class="team-lbl" colspan="2">ក្រុម B</td>
                    <td class="team-name" colspan="11">{m['team_b']}</td>
                    <td class="pts-lbl">ពិន្ទុ</td>
                </tr>
                <tr>
                    {ends}
                    <td rowspan="2" class="pts-box"></td>
                </tr>
                <tr>
                    {score_cells}
                </tr>
            </table>
            <table class="result">
                <tr>
                    <td class="lbl">លទ្ធផល</td>
                    <td class="lbl center">ឈ្នះក្រុម</td>
                    <td class="lbl center">ក្រុម</td>
                    <td class="lbl center">ហត្ថលេខា</td>
                    <td class="lbl center">ហត្ថលេខាអាជ្ញាកណ្តាល</td>
                </tr>
                <tr>
                    <td class="lbl" rowspan="2">ក្រុមឈ្នះ</td>
                    <td rowspan="2"></td>
                    <td class="lbl center">A</td>
                    <td></td>
                    <td rowspan="2"></td>
                </tr>
                <tr>
                    <td class="lbl center">B</td>
                    <td></td>
                </tr>
            </table>
        </div>
        """

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=Battambang:wght@400;700&display=swap');
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Battambang', sans-serif; background: white; }}
.page {{
    width: 210mm; height: 148mm;
    padding: 4mm 5mm;
    page-break-after: always;
    display: flex; flex-direction: column;
    justify-content: space-between;
    overflow: hidden;
}}
h2 {{ text-align: center; font-size: 15pt; font-weight: bold; margin-bottom: 1mm; flex-shrink: 0; }}
table {{ width: 100%; border-collapse: collapse; flex-shrink: 0; }}
td, th {{ border: 1.2px solid black; padding: 1px 4px; vertical-align: middle; text-align: center; }}
table.info td {{ height: 9mm; font-size: 11pt; }}
table.info td:nth-child(1) {{ width: 14%; font-weight: bold; white-space: nowrap; }}
table.info td:nth-child(2) {{ width: 46%; }}
table.info td:nth-child(3) {{ width: 16%; font-weight: bold; white-space: nowrap; }}
table.info td:nth-child(4) {{ width: 24%; font-weight: bold; font-size: 12pt; }}
table.score {{ table-layout: fixed; }}
table.score td, table.score th {{ text-align: center; font-size: 11pt; font-weight: bold; }}
.team-lbl {{ width: 13%; font-size: 11pt; font-weight: bold; text-align: center !important; height: 10mm; }}
.team-name {{ font-size: 12pt; font-weight: bold; text-align: center !important; height: 10mm; }}
.pts-lbl {{ width: 9%; font-size: 10pt; font-weight: bold; text-align: center !important; height: 10mm; }}
.pts-box {{ width: 9%; }}
table.score tr:nth-child(2) td, table.score tr:nth-child(3) td {{ height: 9mm; font-size: 11pt; }}
table.result td {{ height: 9mm; font-size: 10pt; text-align: center; }}
table.result td:nth-child(1) {{ width: 10%; font-weight: bold; }}
table.result td:nth-child(2) {{ width: 28%; font-weight: bold; }}
table.result td:nth-child(3) {{ width: 9%; font-weight: bold; }}
table.result td:nth-child(4) {{ width: 26%; font-weight: bold; }}
table.result td:nth-child(5) {{ width: 27%; font-weight: bold; font-size: 9pt; }}
@page {{ size: 210mm 148mm; margin: 0; }}
</style>
</head><body>{pages}</body></html>"""


# ─── GENERATE PDF VIA PLAYWRIGHT ─────────────────────────────────────────────

async def generate_pdf_playwright(matches, round_num, time_start, event_name, play_type, fmt, output_path):
    html_content = make_html(matches, round_num, time_start, event_name, play_type, fmt)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page    = await browser.new_page()
        await page.set_content(html_content, wait_until="networkidle")
        await page.evaluate("document.fonts.ready")
        await page.pdf(
            path=output_path,
            width="210mm",
            height="148mm",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        await browser.close()


# ─── AUTO MODE: called by the Cloud Function, skips Telegram upload/buttons ──

async def send_match_forms_auto(matches, round_num, time_start, event_name):
    """Build match forms with fixed Play Type/Format and send to every chat in CHAT_IDS."""
    play_type = "3 vs 3"
    fmt       = "Swiss-System"

    bot = Bot(token=BOT_TOKEN)
    status_msgs = {}
    for cid in CHAT_IDS:
        status_msgs[cid] = await bot.send_message(
            chat_id=cid,
            text=f"⏳ Generating match forms for {event_name} - Round {round_num}...",
        )

    async def animate_status():
        dots_cycle = ["", ".", "..", "..."]
        i = 0
        while True:
            await asyncio.sleep(1)
            i = (i + 1) % len(dots_cycle)
            for cid, msg in status_msgs.items():
                try:
                    await bot.edit_message_text(
                        chat_id=cid,
                        message_id=msg.message_id,
                        text=f"⏳ Generating match forms for {event_name} - Round {round_num}{dots_cycle[i]}",
                    )
                except Exception:
                    pass

    animation_task = asyncio.create_task(animate_status())

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "match_forms.pdf")
            await generate_pdf_playwright(
                matches, round_num, time_start, event_name, play_type, fmt, output_path
            )
            animation_task.cancel()
            for cid, msg in status_msgs.items():
                try:
                    await bot.delete_message(chat_id=cid, message_id=msg.message_id)
                except Exception:
                    pass
            for cid in CHAT_IDS:
                with open(output_path, "rb") as f:
                    await bot.send_document(
                        chat_id=cid,
                        document=f,
                        filename=f"MatchForms_{event_name}_Round{round_num}.pdf",
                        caption=f"{event_name} - Round {round_num} | Time: {time_start} | {len(matches)} forms | Auto-generated",
                    )
    finally:
        animation_task.cancel()


# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 សួស្តី! ខ្ញុំជា Match Form Bot\n\n"
        "📄 ផ្ញើ Round PDF មកខ្ញុំ\n"
        "ខ្ញុំនឹងបង្កើត Match Form ហើយផ្ញើ PDF មកវិញ។\n\n"
        "✅ Ready! Send your Round PDF now."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 HOW TO USE:\n\n"
        "1. Send me the Round PDF\n"
        "2. Select Play Type and Format\n"
        "3. I send back filled A5 Landscape match forms PDF\n"
        "4. Print and go!\n\n"
        "Made by Puth Yon Chandara"
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message  = update.message
    document = message.document

    if not document.file_name.lower().endswith(".pdf"):
        await message.reply_text("Please send a PDF file.")
        return

    await message.reply_text("Reading your Round PDF...")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "round.pdf")
            tg_file    = await context.bot.get_file(document.file_id)
            await tg_file.download_to_drive(input_path)

            round_num, time_start, event_name, matches = parse_round_pdf(input_path)

            if not matches:
                await message.reply_text("No matches found. Make sure it is a Round PDF from SwissKH.")
                return

            await message.reply_text(f"{event_name} | Round {round_num} | Time: {time_start} | {len(matches)} matches found.")
            await ask_play_type(message, context, matches, round_num, time_start, event_name)

    except Exception as e:
        log.exception("Error processing PDF")
        await message.reply_text(f"Error: {e}")


async def handle_text(update, context):
    message = update.message
    text    = message.text.strip()
    lines   = [l.strip() for l in text.splitlines() if l.strip()]
    matches = []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            matches.append({"lane": parts[0], "team_a": parts[1], "team_b": ",".join(parts[2:]).strip()})
    if not matches:
        await message.reply_text("Format: lane, TeamA, TeamB (one per line)")
        return
    await message.reply_text(f"{len(matches)} matches found.")
    await ask_play_type(message, context, matches, "bracket", "--:--", "Tournament")


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please send a Round PDF file. Type /help for instructions.")


# ─── WEB SERVER: receives round data directly from the tournament app ───────

flask_app = Flask(__name__)
CORS(flask_app)

@flask_app.route("/generate-round", methods=["POST"])
def generate_round():
    data = request.get_json(force=True)

    round_num  = data.get("round_num", "?")
    time_start = data.get("time_start", "--:--")
    event_name = data.get("event_name", "")
    matches    = data.get("matches", [])   # expects [{ "lane": "1", "team_a": "...", "team_b": "..." }, ...]

    if not matches:
        return jsonify({"ok": False, "error": "No matches provided"}), 400

    def run_async_task():
        asyncio.run(send_match_forms_auto(matches, round_num, time_start, event_name))

    threading.Thread(target=run_async_task).start()
    return jsonify({"ok": True, "message": "Generating and sending match forms..."})


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback_play_type, pattern="^play_"))
    app.add_handler(CallbackQueryHandler(callback_format,    pattern="^fmt_"))
    app.add_handler(MessageHandler(~filters.Document.PDF & ~filters.TEXT, handle_other))

    port = int(os.getenv("PORT", 5000))
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port),
        daemon=True,
    ).start()

    log.info("Match Form Bot is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
