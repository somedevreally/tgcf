"""The module responsible for operating tgcf in live mode."""

import logging
import os
import sys
from typing import Union, Optional

from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message

from tgcf import config, const
from tgcf import storage as st
from tgcf.bot import get_events
from tgcf.config import CONFIG, get_SESSION
from tgcf.plugins import apply_plugins
from tgcf.utils import clean_session_files, send_message


async def new_message_handler(event: Union[Message, events.NewMessage, events.Album]) -> None:
    """Process new incoming messages, including albums."""
    chat_id = event.chat_id

    if chat_id not in config.from_to:
        return
    logging.info(f"New message or album received in {chat_id}")

    dest = config.from_to.get(chat_id)
    event_uid = st.EventUid(event)

    # Handle storage cleanup
    length = len(st.stored)
    exceeding = length - const.KEEP_LAST_MANY
    if exceeding > 0:
        for key in st.stored:
            del st.stored[key]
            break

    if isinstance(event, events.Album.Event):
        # Handle albums
        st.stored[event_uid] = {}
        if CONFIG.show_forwarded_from:
            # Forward with header if setting is enabled
            for d in dest:
                fwded_msgs = await event.forward_to(d)
                st.stored[event_uid].update({d: fwded_msgs[0] if fwded_msgs else None})
        else:
            # Manually send the album without the header
            media_files = []
            captions = []
            for message in event.messages:
                if message.media:
                    media_files.append(message.media)
                    captions.append(message.text)
            if media_files:
                for d in dest:
                    # Send the album as a group
                    fwded_msgs = await event.client.send_file(
                        d,
                        media_files,
                        caption=captions,
                        album=True
                    )
                    st.stored[event_uid].update({d: fwded_msgs[0] if fwded_msgs else None})
        # Apply plugins to captions (if needed)
        for message in event.messages:
            tm = await apply_plugins(message)
            if tm and tm.text != message.text:
                for d in dest:
                    fwded_msg = st.stored[event_uid].get(d)
                    if fwded_msg and hasattr(fwded_msg, 'edit'):
                        await fwded_msg.edit(tm.text)
    elif not event.message.grouped_id:  # Process non-album messages
        message = event.message
        tm = await apply_plugins(message)
        if not tm:
            return
        st.stored[event_uid] = {}
        # Handle replies
        if event.is_reply:
            reply_to_uid = st.EventUid(st.DummyEvent(chat_id, event.reply_to_msg_id))
            reply_fwded = st.stored.get(reply_to_uid)
            if reply_fwded:
                for d in dest:
                    # Set tm.reply_to to the forwarded message ID in the destination chat
                    tm.reply_to = reply_fwded.get(d).id if reply_fwded.get(d) else None
                    fwded_msg = await send_message(d, tm)
                    st.stored[event_uid].update({d: fwded_msg})
                    tm.reply_to = None  # Reset for the next destination
        else:
            for d in dest:
                fwded_msg = await send_message(d, tm)
                st.stored[event_uid].update({d: fwded_msg})


async def edited_message_handler(event) -> None:
    """Handle message edits."""
    message = event.message
    chat_id = event.chat_id

    if chat_id not in config.from_to:
        return

    logging.info(f"Message edited in {chat_id}")

    event_uid = st.EventUid(event)
    fwded_msgs = st.stored.get(event_uid)

    if fwded_msgs:
        for _, msg in fwded_msgs.items():
            if config.CONFIG.live.delete_on_edit == message.text:
                if msg:
                    await msg.delete()
                await message.delete()
            else:
                tm = await apply_plugins(message)
                if tm and msg and tm.text:
                    await msg.edit(tm.text)
        return

    dest = config.from_to.get(chat_id)
    for d in dest:
        tm = await apply_plugins(message)
        if tm:
            await send_message(d, tm)


async def deleted_message_handler(event):
    """Handle message deletes."""
    chat_id = event.chat_id
    if chat_id not in config.from_to:
        return

    logging.info(f"Message deleted in {chat_id}")

    event_uid = st.EventUid(event)
    fwded_msgs = st.stored.get(event_uid)
    if fwded_msgs:
        for _, msg in fwded_msgs.items():
            if msg:
                await msg.delete()


ALL_EVENTS = {
    "new": (new_message_handler, events.NewMessage()),
    "album": (new_message_handler, events.Album()),
    "edited": (edited_message_handler, events.MessageEdited()),
    "deleted": (deleted_message_handler, events.MessageDeleted()),
}


async def start_sync() -> None:
    """Start tgcf live sync."""
    clean_session_files()

    SESSION = get_SESSION()
    client = TelegramClient(
        SESSION,
        CONFIG.login.API_ID,
        CONFIG.login.API_HASH,
        sequential_updates=CONFIG.live.sequential_updates,
    )
    if CONFIG.login.user_type == 0:
        if CONFIG.login.BOT_TOKEN == "":
            logging.warning("Bot token not found, but login type is set to bot.")
            sys.exit()
        await client.start(bot_token=CONFIG.login.BOT_TOKEN)
    else:
        await client.start()
    config.is_bot = await client.is_bot()
    logging.info(f"config.is_bot={config.is_bot}")
    command_events = get_events()

    await config.load_admins(client)

    ALL_EVENTS.update(command_events)

    for key, val in ALL_EVENTS.items():
        if config.CONFIG.live.delete_sync is False and key == "deleted":
            continue
        client.add_event_handler(*val)
        logging.info(f"Added event handler for {key}")

    if config.is_bot and const.REGISTER_COMMANDS:
        await client(
            functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code="en",
                commands=[
                    types.BotCommand(command=key, description=value)
                    for key, value in const.COMMANDS.items()
                ],
            )
        )
    config.from_to = await config.load_from_to(client, config.CONFIG.forwards)
    await client.run_until_disconnected()
