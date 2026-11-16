

import asyncio, io, json, logging, random, time, uuid
import requests
from os import getenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

from dotenv import load_dotenv
load_dotenv()  # reads .env from the current folder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

TOKEN = getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN not set. Add it to .env file")
ALLOWED_USER = 1723596382         # your Telegram user ID
COMFY = "http://127.0.0.1:8188"   # 8188 for portable
WORKFLOW = "basic.json"
PROMPT_NODE = "3"                 # positive CLIPTextEncode node ID
SEED_NODE = "5"                   # KSampler node ID
HTTP_TIMEOUT = 60
COUNT_TIMEOUT = 5                 # seconds to answer "how many images?"
MAX_IMAGES = 4

# TODO(workflow 1/4): WORKFLOW is now only the DEFAULT. The active one lives in
#   ctx.bot_data["workflow"]. Nothing to change here, just don't read WORKFLOW in generate().

# TODO(workflow 2/4): change the signature to  def generate(text: str, workflow: str) -> list[bytes]:
#   and open `workflow` instead of WORKFLOW below.
def generate(text: str) -> list[bytes]:
    # Open workflow file
    with open(WORKFLOW, encoding="utf-8") as f:
        wf = json.load(f)
    
    # Set text and seed
    wf[PROMPT_NODE]["inputs"]["text"] = text
    wf[SEED_NODE]["inputs"]["seed"] = random.randint(0, 2**32)

    # Send request to Comfy Server
    r = requests.post(
        f"{COMFY}/prompt",
        json={"prompt": wf, "client_id": str(uuid.uuid4())},
        timeout=HTTP_TIMEOUT,
    )

    # Wait for result checking for errors.
    r.raise_for_status()
    pid = r.json()["prompt_id"]

    while True:
        hist = requests.get(f"{COMFY}/history/{pid}", timeout=HTTP_TIMEOUT).json()
        # hist is the history from Comfy Server for this pid
        if pid in hist: # If pid shows, the process is done. Poll every 1 second
            break
        time.sleep(1)
    images = []

    # iterate in all outputs generated in this pid
    for out in hist[pid]["outputs"].values():
        for img in out.get("images", []):
            v = requests.get(f"{COMFY}/view", params=img, timeout=HTTP_TIMEOUT) # get image
            v.raise_for_status() # check for errors
            images.append(v.content) # put in the images array
    return images


def check():
    try:
        r = requests.get(f"{COMFY}/prompt",timeout=3)
        return r.status_code == 200
    except requests.RequestException:
        return False

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text("What should I generate for you today?")

async def on_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text("A bot to generate images using ComfyUI Desktop")

async def run_generation(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, prompt: str, count: int):
    try:
        await ctx.bot.send_message(chat_id=chat_id, text="Generating...")
        for _ in range(count):
            # TODO(workflow 3/4): read the active workflow once at the top of run_generation:
            #   wf_name = ctx.bot_data.get("workflow", WORKFLOW)
            #   and pass it: asyncio.to_thread(generate, prompt, wf_name)
            images = await asyncio.to_thread(generate, prompt)  # new random seed on every call
            for data in images:
                await ctx.bot.send_photo(chat_id=chat_id, photo=io.BytesIO(data))
        await ctx.bot.send_message(chat_id=chat_id, text="Done. Send another prompt.")
    except Exception as e:
        logging.exception("generation/send failed")
        await ctx.bot.send_message(chat_id=chat_id, text=f"Error: {e!r}")
    finally:
        ctx.bot_data["busy"] = False

def start_generation(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, prompt: str, count: int):
    # Runs as a background task so the handler returns immediately.
    # PTB processes updates one at a time by default, so an awaited generation would block /help and /status.
    ctx.bot_data["busy"] = True
    ctx.bot_data["task"] = asyncio.create_task(run_generation(ctx, chat_id, prompt, count))

async def on_count_timeout(ctx: ContextTypes.DEFAULT_TYPE):
    prompt = ctx.bot_data.pop("pending", None)
    if prompt is not None:
        start_generation(ctx, ALLOWED_USER, prompt, 1)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER or update.message is None or not update.message.text:
        return
    text = update.message.text
    chat_id = update.message.chat_id

    if ctx.bot_data.get("busy"):
        await update.message.reply_text("Still generating, this prompt was ignored.")
        return

    if ctx.bot_data.get("pending") is not None:
        if text.isdigit() and 1 <= int(text) <= MAX_IMAGES:
            ctx.bot_data.pop("timer").schedule_removal()
            prompt = ctx.bot_data.pop("pending")
            start_generation(ctx, chat_id, prompt, int(text))
        return  # anything that isn't a valid count is discarded

    ctx.bot_data["pending"] = text
    ctx.bot_data["timer"] = ctx.job_queue.run_once(on_count_timeout, when=COUNT_TIMEOUT)
    await update.message.reply_text(
        f"How many images? Send 1-{MAX_IMAGES} within {COUNT_TIMEOUT}s (default 1)."
    )

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled error", exc_info=ctx.error)

async def on_status_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    is_up = await asyncio.to_thread(check)
    await update.message.reply_text(f"Server up: {is_up}")

# TODO(workflow 4/4): new command handler  async def on_workflow(update: Update, ctx: ContextTypes.DEFAULT_TYPE)
#   a) guards: if update.message is None or update.effective_user is None or id != ALLOWED_USER: return
#   b) busy: if ctx.bot_data.get("busy") or ctx.bot_data.get("pending") is not None:
#        reply "Busy, try again later" and return
#   c) no argument: if not ctx.args: reply "Usage: /workflow name" + current one
#        (ctx.bot_data.get("workflow", WORKFLOW)) and return
#   d) name = ctx.args[0]; if not name.endswith(".json"): name += ".json"
#   e) SECURITY: reject paths. if os.path.basename(name) != name: reply a warning and return
#        (blocks "../x.json" and "sub/x.json"). `import os` first, or use `from os.path import basename, isfile`
#   f) if not os.path.isfile(name): reply a warning "not found, keeping <current>" and return
#   g) ctx.bot_data["workflow"] = name; reply "Workflow changed to <name>"
#   NOTE: PROMPT_NODE/SEED_NODE are still for basic.json, a different workflow
#   will probably fail with KeyError. on_error/run_generation will report it.
#   Register it below:  app.add_handler(CommandHandler(["workflow"], on_workflow))
async def health_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    is_up = await asyncio.to_thread(check)
    was_up = ctx.bot_data.get("server_up")
    if was_up == is_up:
        return
    ctx.bot_data["server_up"] = is_up

    text = "SERVER ONLINE" if is_up else "SERVER OFFLINE"
    await ctx.bot.send_message(chat_id=ALLOWED_USER, text=text)
    
app = (
    ApplicationBuilder()
    .token(TOKEN)
    .read_timeout(60)
    .media_write_timeout(240)
    .write_timeout(60)
    .connect_timeout(30)
    .build()
)
app.add_handler(CommandHandler(["start", "imagine"], start))
app.add_handler(CommandHandler(["help"], on_help))
app.add_handler(CommandHandler(["status"], on_status_command))
# TODO(workflow, registration): add here ->  app.add_handler(CommandHandler(["workflow"], on_workflow))
#   Must be before the MessageHandler line below is fine either way, because
#   filters.COMMAND is excluded from it, but keep commands grouped together.
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
app.add_error_handler(on_error)
app.job_queue.run_repeating(health_job,interval=10, first=0)
app.run_polling()