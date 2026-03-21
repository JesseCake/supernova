import asyncio
import aiohttp
import uuid
import threading
from core.precontext import VoiceMode


class TelegramInterface:
    """
    Telegram bot interface for Supernova.
    Receives messages via long-polling and sends responses via Bot API.
    Each Telegram user is identified by their chat_id as endpoint_id.

    Features:
      - Whitelist: only chat_ids configured in telegram_interface.yaml are allowed
      - Session timeout: conversations reset after SESSION_TTL minutes of inactivity
      - /reset command: user can explicitly start a fresh conversation
      - Typing indicator: shown while LLM is generating, refreshed every 4 seconds
    """

    SESSION_TTL = 30 * 60   # seconds of inactivity before session expires

    def __init__(self, core_processor, config):
        self.core_processor = core_processor
        self.config         = config
        self.token          = config.telegram.token
        self.base_url       = f"https://api.telegram.org/bot{self.token}"
        self._offset        = 0
        self._sessions      = {}   # chat_id → session_id
        self._last_active   = {}   # chat_id → loop timestamp of last message
        self._last_typing   = {}   # chat_id → loop timestamp of last typing indicator

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        print("[telegram] Bot started, polling for messages...")
        async with aiohttp.ClientSession() as session:
            self._http = session
            while True:
                try:
                    await self._poll()
                except Exception as e:
                    print(f"[telegram] Poll error: {e}")
                    await asyncio.sleep(5)

    async def _poll(self):
        """Long-poll Telegram for new updates."""
        async with self._http.get(
            f"{self.base_url}/getUpdates",
            params={"offset": self._offset, "timeout": 30},
            timeout=aiohttp.ClientTimeout(total=35),
        ) as r:
            data = await r.json()

        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message", {})

            if not msg:
                continue
            
            chat_id = str(msg["chat"]["id"])
            text    = msg.get("text", "").strip()
            photo   = msg.get("photo")
            caption = msg.get("caption", "").strip()

            if text:
                asyncio.create_task(self._handle_message(chat_id, text))
            elif photo:
                asyncio.create_task(self._handle_photo(chat_id, photo, caption))

    async def _ensure_session(self, chat_id: str):
        """Create or reuse session for chat_id, expiring if idle too long."""
        now = asyncio.get_event_loop().time()

        # Expire if idle too long
        if chat_id in self._sessions:
            idle = now - self._last_active.get(chat_id, 0)
            if idle > self.SESSION_TTL:
                print(f"[telegram] Session expired for {chat_id} ({idle/60:.1f} min idle)")
                await self._reset_session(chat_id)

        self._last_active[chat_id] = now

        # Create new session if needed
        if chat_id not in self._sessions:
            session_id    = str(uuid.uuid4())
            friendly_name = next(
                (ep.friendly_name for ep in self.config.telegram.endpoints.values()
                 if ep.chat_id == chat_id),
                None
            )
            self.core_processor.create_session(session_id)
            core_session = self.core_processor.get_session(session_id)
            if core_session is not None:
                core_session['endpoint_id'] = chat_id
                core_session['interface']   = 'telegram'
                if friendly_name:
                    core_session['speaker'] = friendly_name
            self._sessions[chat_id] = session_id
            print(f"[telegram] New session for {friendly_name or chat_id}: {session_id}")

    # ── Message handling ──────────────────────────────────────────────────────

    async def _handle_message(self, chat_id: str, text: str):
        """Route an incoming message through core."""

        # ── Whitelist ─────────────────────────────────────────────────────────
        # Only allow chat_ids explicitly configured in telegram_interface.yaml.
        # Anyone else is silently ignored.
        allowed = {ep.chat_id for ep in self.config.telegram.endpoints.values()}
        if chat_id not in allowed:
            print(f"[telegram] Ignoring unknown chat_id: {chat_id!r}")
            return

        # ── /reset command ────────────────────────────────────────────────────
        # Clears conversation history and sends a visual separator so the user
        # knows the bot has forgotten the previous conversation.
        if text.lower() in ("/reset", "/start"):
            await self._reset_session(chat_id)
            await self.send_message(
                chat_id,
                "——————————————\n"
                "🔄 Conversation reset. Starting fresh.\n"
                "To clear the chat visually, tap the chat name → Clear History."
            )
            return

        await self._ensure_session(chat_id)
        session_id = self._sessions[chat_id]

        # ── Typing indicator ──────────────────────────────────────────────────
        # Show immediately so the user sees feedback before the first token.
        await self._send_typing(chat_id)
        self._last_typing[chat_id] = now

        # ── LLM processing ────────────────────────────────────────────────────
        thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={
                "input_text": text,
                "session_id": session_id,
                "mode":       VoiceMode.PLAIN,
            },
            daemon=True,
        )
        thread.start()

        # Drain the response queue, refreshing the typing indicator as chunks
        # arrive so Telegram keeps showing it throughout generation.
        core_session = self.core_processor.get_session(session_id)
        response     = []
        while True:
            chunk = await asyncio.to_thread(core_session['response_queue'].get)
            if chunk is None:
                break
            response.append(chunk)
            await self._maybe_send_typing(chat_id)

        full_response = "".join(response).strip()
        if full_response:
            await self.send_message(chat_id, full_response)

    async def _handle_photo(self, chat_id: str, photo: list, caption: str):
        """Download photo and route through core with image content."""
        # Whitelist check
        allowed = {ep.chat_id for ep in self.config.telegram.endpoints.values()}
        if chat_id not in allowed:
            print(f"[telegram] Ignoring unknown chat_id: {chat_id!r}")
            return

        # Get largest size (last in Telegram's array)
        file_id     = photo[-1]["file_id"]
        image_bytes = await self._download_photo(file_id)
        if not image_bytes:
            await self.send_message(chat_id, "Sorry, I couldn't download that image.")
            return

        prompt = caption or "What's in this image?"
        print(f"[telegram] Photo from {chat_id}, prompt: {prompt!r}")

        await self._ensure_session(chat_id)
        session_id = self._sessions[chat_id]

        await self._send_typing(chat_id)
        self._last_typing[chat_id] = asyncio.get_event_loop().time()

        thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={
                "input_text": prompt,
                "session_id": session_id,
                "mode":       VoiceMode.PLAIN,
                "images":     [image_bytes],
            },
            daemon=True,
        )
        thread.start()

        core_session = self.core_processor.get_session(session_id)
        response     = []
        while True:
            chunk = await asyncio.to_thread(core_session['response_queue'].get)
            if chunk is None:
                break
            response.append(chunk)
            await self._maybe_send_typing(chat_id)

        full_response = "".join(response).strip()
        if full_response:
            await self.send_message(chat_id, full_response)

    async def _download_photo(self, file_id: str) -> bytes | None:
        """Download a photo from Telegram, return raw bytes."""
        try:
            async with self._http.get(
                f"{self.base_url}/getFile",
                params={"file_id": file_id},
            ) as r:
                data = await r.json()
            file_path = data["result"]["file_path"]
            async with self._http.get(
                f"https://api.telegram.org/file/bot{self.token}/{file_path}"
            ) as r:
                return await r.read()
        except Exception as e:
            print(f"[telegram] Photo download error: {e}")
            return None

    # ── Session management ────────────────────────────────────────────────────

    async def _reset_session(self, chat_id: str):
        """
        Clear the LLM session and local state for a chat_id.
        Called on /reset, /start, or session timeout.
        """
        session_id = self._sessions.pop(chat_id, None)
        if session_id:
            self.core_processor.sessions.pop(session_id, None)
        self._last_active.pop(chat_id, None)
        self._last_typing.pop(chat_id, None)

    # ── Typing indicator ──────────────────────────────────────────────────────

    async def _send_typing(self, chat_id: str):
        """Send a typing indicator to a chat."""
        try:
            async with self._http.post(
                f"{self.base_url}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            ) as r:
                pass
        except Exception:
            pass

    async def _maybe_send_typing(self, chat_id: str):
        """
        Send a typing indicator only if 4+ seconds have passed since the last
        one. Call this on each incoming chunk to keep the indicator alive
        throughout long responses without hammering the API.
        """
        now  = asyncio.get_event_loop().time()
        last = self._last_typing.get(chat_id, 0.0)
        if now - last >= 4.0:
            await self._send_typing(chat_id)
            self._last_typing[chat_id] = now

    # ── Outbound messaging ────────────────────────────────────────────────────

    async def send_message(self, chat_id: str, text: str):
        """Send a text message to a Telegram chat."""
        try:
            async with self._http.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            ) as r:
                if r.status != 200:
                    print(f"[telegram] Send error: {await r.text()}")
        except Exception as e:
            print(f"[telegram] Send error: {e}")