import asyncio
import functools
import json
import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from names import extract_names
from lookup import lookup_all, twitter_search_url, instagram_search_url
from formatter import apply_substitutions, format_platform

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_data_dir = os.environ.get("DATA_DIR", os.path.dirname(__file__))
CONFIG_PATH = os.path.join(_data_dir, "config.json")

_DEFAULT_CONFIG = {
    "twitter":   {"prefix": "", "suffix": ""},
    "bluesky":   {"prefix": "", "suffix": ""},
    "instagram": {"prefix": "", "suffix": ""},
    "ignored_names": [],
    "allowed_users": [],
}

ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])

# ── Conversation states ────────────────────────────────────────────────────────
(
    CONFIRM_NAMES,
    AWAIT_MANUAL_NAMES,
    AWAIT_HANDLE_INPUT,
    EDIT_CONFIG_VALUE,
    MANAGE_USERS,
    ADD_USER,
) = range(6)


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        save_config(_DEFAULT_CONFIG)
        return dict(_DEFAULT_CONFIG)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Auth ───────────────────────────────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    if user_id == ADMIN_USER_ID:
        return True
    return user_id in load_config().get("allowed_users", [])


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_USER_ID:
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def authorized_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update.effective_user.id):
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


# ── Output sender ──────────────────────────────────────────────────────────────

PLATFORM_EMOJI = {
    "instagram": "📸 INSTAGRAM",
    "twitter":   "🐦 TWITTER",
    "bluesky":   "🦋 BLUESKY",
}


async def send_formatted_output(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = context.user_data["original_text"]
    substitutions = context.user_data.get("substitutions", {})
    config = load_config()

    platform_texts = apply_substitutions(text, substitutions)

    for platform in ["instagram", "twitter", "bluesky"]:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=PLATFORM_EMOJI[platform],
        )
        chunks = format_platform(platform_texts[platform], platform, config)
        for chunk in chunks:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=chunk,
            )


# ── Name confirmation ──────────────────────────────────────────────────────────

def build_name_message(lookup: dict) -> str:
    name = lookup["name"]
    lines = [f"🔍 *{name}*\n"]

    lines.append("🦋 *Bluesky* — pick one:")
    if lookup["bluesky"]:
        for i, actor in enumerate(lookup["bluesky"]):
            lines.append(f"  {i+1}\\. [{actor['handle']}]({actor['url']}) · {actor['display_name']}")
    else:
        lines.append("  _No results found_")

    lines.append("\n🐦 *Twitter*")
    if lookup["twitter"]:
        lines.append(f"  {lookup['twitter']}")
    else:
        lines.append("  _No results found_")

    lines.append("\n📸 *Instagram*")
    if lookup["instagram"]:
        lines.append(f"  {lookup['instagram']}")
    else:
        lines.append("  _No results found_")

    return "\n".join(lines)


def build_name_keyboard(lookup: dict, name_idx: int) -> InlineKeyboardMarkup:
    rows = []

    # Bluesky: one button per result + skip
    for i, actor in enumerate(lookup["bluesky"]):
        rows.append([InlineKeyboardButton(
            f"{i+1}. @{actor['handle']} · {actor['display_name']}",
            callback_data=f"bsky:{name_idx}:{i}"
        )])
    rows.append([InlineKeyboardButton("🦋 Skip Bluesky", callback_data=f"bsky:{name_idx}:skip")])

    # Twitter
    tw_row = []
    if lookup["twitter"]:
        tw_row.append(InlineKeyboardButton("🐦 ✅ Use", callback_data=f"tw:{name_idx}:use"))
        tw_row.append(InlineKeyboardButton("✏️ Correct", callback_data=f"tw:{name_idx}:edit"))
    else:
        tw_row.append(InlineKeyboardButton("🐦 ✏️ Add handle", callback_data=f"tw:{name_idx}:edit"))
    tw_row.append(InlineKeyboardButton("Skip", callback_data=f"tw:{name_idx}:skip"))
    rows.append(tw_row)

    # Instagram
    ig_row = []
    if lookup["instagram"]:
        ig_row.append(InlineKeyboardButton("📸 ✅ Use", callback_data=f"ig:{name_idx}:use"))
        ig_row.append(InlineKeyboardButton("✏️ Correct", callback_data=f"ig:{name_idx}:edit"))
    else:
        ig_row.append(InlineKeyboardButton("📸 ✏️ Add handle", callback_data=f"ig:{name_idx}:edit"))
    ig_row.append(InlineKeyboardButton("Skip", callback_data=f"ig:{name_idx}:skip"))
    rows.append(ig_row)

    return InlineKeyboardMarkup(rows)


def is_resolved(entry: dict) -> bool:
    return all(entry.get(k, "pending") != "pending" for k in ("bluesky", "twitter", "instagram"))


async def show_next_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lookups = context.user_data["lookups"]
    idx = context.user_data.get("current_name_idx", 0)

    if idx >= len(lookups):
        await send_formatted_output(update, context)
        return ConversationHandler.END

    lookup = lookups[idx]
    context.user_data.setdefault("resolved", {})[idx] = {
        "bluesky": "pending",
        "twitter": "pending",
        "instagram": "pending",
    }

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_name_message(lookup),
        parse_mode="Markdown",
        reply_markup=build_name_keyboard(lookup, idx),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return CONFIRM_NAMES


async def try_advance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data["current_name_idx"]
    resolved = context.user_data["resolved"]

    if is_resolved(resolved[idx]):
        lookups = context.user_data["lookups"]
        name = lookups[idx]["name"]
        r = resolved[idx]
        subs = context.user_data.setdefault("substitutions", {})
        subs[name] = {
            "twitter":   r["twitter"]   if r["twitter"]   != "skip" else None,
            "bluesky":   r["bluesky"]   if r["bluesky"]   != "skip" else None,
            "instagram": r["instagram"] if r["instagram"] != "skip" else None,
        }
        context.user_data["current_name_idx"] = idx + 1
        return await show_next_name(update, context)

    return CONFIRM_NAMES


# ── Callback handler ───────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # No-names prompt
    if data == "detect:yes":
        await query.edit_message_text("Type the names you want to look up, one per line:")
        return AWAIT_MANUAL_NAMES

    if data == "detect:no":
        await query.edit_message_text("Formatting…")
        await send_formatted_output(update, context)
        return ConversationHandler.END

    # Config editing
    if data.startswith("cfg:edit:"):
        _, _, platform, field = data.split(":", 3)
        context.user_data["editing"] = {"platform": platform, "field": field}
        cfg = load_config()
        if platform == "ignored_names":
            current = ", ".join(cfg.get("ignored_names", []))
        else:
            current = cfg.get(platform, {}).get(field, "")
        await query.edit_message_text(
            f"Current: `{current or '(empty)'}`\n\nType the new value:",
            parse_mode="Markdown",
        )
        return EDIT_CONFIG_VALUE

    # Name resolution callbacks
    parts = data.split(":")
    platform_code, name_idx, action = parts[0], int(parts[1]), parts[2]
    lookups = context.user_data["lookups"]
    lookup = lookups[name_idx]
    resolved = context.user_data["resolved"][name_idx]

    if platform_code == "bsky":
        if action == "skip":
            resolved["bluesky"] = "skip"
        else:
            actor = lookup["bluesky"][int(action)]
            resolved["bluesky"] = f"@{actor['handle']}"

    elif platform_code == "tw":
        if action == "skip":
            resolved["twitter"] = "skip"
        elif action == "use":
            url = lookup["twitter"]
            handle = "@" + url.rstrip("/").split("/")[-1]
            resolved["twitter"] = handle
        elif action == "edit":
            context.user_data["editing_handle"] = {"name_idx": name_idx, "platform": "twitter"}
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Search on X", url=twitter_search_url(lookup["name"]))
            ]])
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Type the correct Twitter handle (e.g. @handle), or search first:",
                reply_markup=keyboard,
            )
            return AWAIT_HANDLE_INPUT

    elif platform_code == "ig":
        if action == "skip":
            resolved["instagram"] = "skip"
        elif action == "use":
            url = lookup["instagram"]
            handle = "@" + url.rstrip("/").split("/")[-1]
            resolved["instagram"] = handle
        elif action == "edit":
            context.user_data["editing_handle"] = {"name_idx": name_idx, "platform": "instagram"}
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Search on Instagram", url=instagram_search_url(lookup["name"]))
            ]])
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Type the correct Instagram handle (e.g. @handle), or search first:",
                reply_markup=keyboard,
            )
            return AWAIT_HANDLE_INPUT

    return await try_advance(update, context)


# ── Handle input ───────────────────────────────────────────────────────────────

async def receive_handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lstrip("@")
    handle = f"@{raw}"
    editing = context.user_data["editing_handle"]
    name_idx = editing["name_idx"]
    platform = editing["platform"]

    context.user_data["resolved"][name_idx][platform] = handle
    await update.message.reply_text(f"✅ Set to {handle}")
    return await try_advance(update, context)


# ── Manual name entry ──────────────────────────────────────────────────────────

async def receive_manual_names(update: Update, context: ContextTypes.DEFAULT_TYPE):
    names = [n.strip() for n in update.message.text.strip().splitlines() if n.strip()]
    if not names:
        await update.message.reply_text("No names found. Try again or /cancel.")
        return AWAIT_MANUAL_NAMES

    await update.message.reply_text(f"Looking up {len(names)} name(s)…")
    lookups = await asyncio.gather(*[lookup_all(n) for n in names])
    context.user_data["lookups"] = list(lookups)
    context.user_data["current_name_idx"] = 0
    return await show_next_name(update, context)


# ── Main text receiver ─────────────────────────────────────────────────────────

@authorized_only
async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = (message.text or message.caption or "").strip()

    if not text:
        await message.reply_text("Send me some text to format.")
        return ConversationHandler.END

    context.user_data.update({
        "original_text": text,
        "substitutions": {},
        "current_name_idx": 0,
        "lookups": [],
        "resolved": {},
    })

    config = load_config()
    names = extract_names(text, config.get("ignored_names", []))

    if not names:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, add names", callback_data="detect:yes"),
            InlineKeyboardButton("No, just format", callback_data="detect:no"),
        ]])
        await message.reply_text(
            "No names detected. Want to add handles manually?",
            reply_markup=keyboard,
        )
        return CONFIRM_NAMES

    await message.reply_text(f"Found: {', '.join(names)}. Looking up handles…")
    lookups = await asyncio.gather(*[lookup_all(n) for n in names])
    context.user_data["lookups"] = list(lookups)
    return await show_next_name(update, context)


# ── /config command ────────────────────────────────────────────────────────────

@authorized_only
async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    lines = ["⚙️ *Config*\n"]
    rows = []

    for platform, emoji in [("twitter", "🐦"), ("bluesky", "🦋"), ("instagram", "📸")]:
        p = cfg.get(platform, {})
        prefix = p.get("prefix") or "(empty)"
        suffix = p.get("suffix") or "(empty)"
        lines.append(f"{emoji} *{platform.capitalize()}*")
        lines.append(f"  Prefix: `{prefix}`")
        lines.append(f"  Suffix: `{suffix}`\n")
        rows.append([
            InlineKeyboardButton(f"{emoji} Prefix", callback_data=f"cfg:edit:{platform}:prefix"),
            InlineKeyboardButton(f"{emoji} Suffix", callback_data=f"cfg:edit:{platform}:suffix"),
        ])

    ignored = cfg.get("ignored_names", [])
    lines.append(f"🚫 *Ignored names:* `{', '.join(ignored) or '(none)'}`")
    rows.append([InlineKeyboardButton("🚫 Edit ignored names", callback_data="cfg:edit:ignored_names:ignored_names")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return EDIT_CONFIG_VALUE


async def receive_config_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    editing = context.user_data.get("editing", {})
    platform = editing.get("platform")
    field = editing.get("field")
    value = update.message.text.strip()
    cfg = load_config()

    if platform == "ignored_names":
        cfg["ignored_names"] = [n.strip() for n in value.split(",") if n.strip()]
    else:
        cfg.setdefault(platform, {})[field] = value

    save_config(cfg)
    await update.message.reply_text("✅ Saved. Use /config to review.")
    return ConversationHandler.END


# ── /users command (admin only) ───────────────────────────────────────────────

def build_users_message(allowed: list[int]) -> str:
    if not allowed:
        return "👥 *Allowed users*\n\n_(none)_"
    lines = ["👥 *Allowed users*\n"]
    for uid in allowed:
        lines.append(f"• `{uid}`")
    return "\n".join(lines)


def build_users_keyboard(allowed: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🗑 {uid}", callback_data=f"usr:remove:{uid}")] for uid in allowed]
    rows.append([InlineKeyboardButton("➕ Add user", callback_data="usr:add")])
    return InlineKeyboardMarkup(rows)


@admin_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    allowed = cfg.get("allowed_users", [])
    await update.message.reply_text(
        build_users_message(allowed),
        parse_mode="Markdown",
        reply_markup=build_users_keyboard(allowed),
    )
    return MANAGE_USERS


async def handle_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cfg = load_config()
    allowed = cfg.get("allowed_users", [])

    if query.data == "usr:add":
        await query.edit_message_text("Send the Telegram user ID to add:")
        return ADD_USER

    if query.data.startswith("usr:remove:"):
        uid = int(query.data.split(":")[-1])
        allowed = [u for u in allowed if u != uid]
        cfg["allowed_users"] = allowed
        save_config(cfg)

    await query.edit_message_text(
        build_users_message(allowed),
        parse_mode="Markdown",
        reply_markup=build_users_keyboard(allowed),
    )
    return MANAGE_USERS


async def receive_new_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        uid = int(raw)
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid user ID. Try again or /cancel.")
        return ADD_USER
    cfg = load_config()
    allowed = cfg.get("allowed_users", [])
    if uid not in allowed:
        allowed.append(uid)
        cfg["allowed_users"] = allowed
        save_config(cfg)

    await update.message.reply_text(
        build_users_message(allowed),
        parse_mode="Markdown",
        reply_markup=build_users_keyboard(allowed),
    )
    return MANAGE_USERS


# ── /start & /cancel ───────────────────────────────────────────────────────────

@authorized_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me any text and I'll format it for Twitter, Bluesky, and Instagram.\n"
        "Use /config to set prefixes, suffixes, and ignored names."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            CommandHandler("config", config_command),
        ],
        states={
            CONFIRM_NAMES: [
                CallbackQueryHandler(handle_callback),
            ],
            AWAIT_MANUAL_NAMES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_manual_names),
            ],
            AWAIT_HANDLE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_handle_input),
            ],
            EDIT_CONFIG_VALUE: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_config_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    users_conv = ConversationHandler(
        entry_points=[CommandHandler("users", users_command)],
        states={
            MANAGE_USERS: [CallbackQueryHandler(handle_users_callback, pattern="^usr:")],
            ADD_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_user_id)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(users_conv)
    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()
