import asyncio
import aiohttp
import uuid
import threading
from core.interface_mode import InterfaceMode
from core.session_state import KEY_INTERFACE_MODE, get_response_queue

from core.logger import get_logger
log = get_logger('telegram')


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
        self._sessions      = {}   # chat_id → session_id (current active session)
        self._session_stack = {}   # chat_id → [session_id, ...] (suspended sessions)
        self._last_active   = {}   # chat_id → loop timestamp of last message
        self._last_typing   = {}   # chat_id → loop timestamp of last typing indicator
        self._chat_locks: dict = {}   # chat_id → asyncio.Lock

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        log.info("Bot started, polling for messages")
        async with aiohttp.ClientSession() as session:
            self._http = session
            while True:
                try:
                    await self._poll()
                except Exception as e:
                    log.error("Poll error", exc_info=True)
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
                log.info("Session expired", extra={'data': f"chat_id={chat_id} idle={idle/60:.1f}min"})
                await self._expire_session(chat_id)

        self._last_active[chat_id] = now

        # Create new session if needed
        if chat_id not in self._sessions:
            session_id    = str(uuid.uuid4())
            friendly_name = next(
                (ep.friendly_name for ep in self.config.telegram.endpoints.values()
                 if ep.chat_id == chat_id),
                None
            )
            core_session = self.core_processor.create_session(session_id)
            if core_session is not None:
                core_session[KEY_INTERFACE_MODE]    = InterfaceMode.GENERAL
                core_session['endpoint_id']         = chat_id
                core_session['interface']           = InterfaceMode.GENERAL.value
                loop = asyncio.get_event_loop()
                core_session['immediate_send'] = lambda text, _loop=loop, _chat_id=chat_id: \
                    asyncio.run_coroutine_threadsafe(
                        self.send_message(_chat_id, text),
                        _loop,
                    )
                # Note: immediate_send_only is NOT set — main LLM response
                # still goes through the queue drain in _handle_message.
                # immediate_send is only for ToolBase.speak() pre-tool text.
                if friendly_name:
                    core_session['speaker'] = friendly_name
            self._sessions[chat_id] = session_id
            log.info("New session", extra={'data': f"{friendly_name or chat_id} session={session_id}"})

    # ── Message handling ──────────────────────────────────────────────────────

    async def _handle_message(self, chat_id: str, text: str):
        """Route an incoming message through core."""

        # ── Whitelist ─────────────────────────────────────────────────────────
        # Only allow chat_ids explicitly configured in telegram_interface.yaml.
        # Anyone else is silently ignored.
        allowed = {ep.chat_id for ep in self.config.telegram.endpoints.values()}
        if chat_id not in allowed:
            log.warning("Ignoring unknown chat_id", extra={'data': f"{chat_id!r}"})
            return
        
        async with self._get_lock(chat_id):

            # ── /reset command ────────────────────────────────────────────────────
            # Clears conversation history and sends a visual separator so the user
            # knows the bot has forgotten the previous conversation.
            if text.lower() in ("/reset", "/start"):
                await self._expire_session(chat_id)
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
            now = asyncio.get_event_loop().time()
            await self._send_typing(chat_id)
            self._last_typing[chat_id] = now

            # ── LLM processing ────────────────────────────────────────────────────
            thread = threading.Thread(
                target=self.core_processor.process_input,
                kwargs={
                    "input_text": text,
                    "session_id": session_id,
                },
                daemon=True,
            )
            thread.start()

            # Drain the response queue, refreshing the typing indicator as chunks
            # arrive so Telegram keeps showing it throughout generation.
            core_session = self.core_processor.get_session(session_id)
            response     = []
            while True:
                chunk = await asyncio.to_thread(get_response_queue(core_session).get)
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
            log.warning("Ignoring unknown chat_id", extra={'data': f"{chat_id!r}"})
            return
        
        async with self._get_lock(chat_id):

            # Get largest size (last in Telegram's array)
            file_id     = photo[-1]["file_id"]
            image_bytes = await self._download_photo(file_id)
            if not image_bytes:
                await self.send_message(chat_id, "Sorry, I couldn't download that image.")
                return

            prompt = caption or ""
            log.info("Photo received", extra={'data': f"chat_id={chat_id} prompt={prompt!r}"})

            await self._ensure_session(chat_id)
            session_id = self._sessions[chat_id]

            await self._send_typing(chat_id)
            self._last_typing[chat_id] = asyncio.get_event_loop().time()

            thread = threading.Thread(
                target=self.core_processor.process_input,
                kwargs={
                    "input_text": prompt,
                    "session_id": session_id,
                    "images":     [image_bytes],
                },
                daemon=True,
            )
            thread.start()

            core_session = self.core_processor.get_session(session_id)
            response     = []
            while True:
                chunk = await asyncio.to_thread(get_response_queue(core_session).get)
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
            log.error("Photo download error", exc_info=True)
            return None

    # ── Session management ────────────────────────────────────────────────────

    async def _expire_session(self, chat_id: str):
        """
        Expire a session due to inactivity. If it's a relay session,
        notify both Dean (relay target) and Jesse (caller) before closing.
        """
        session_id = self._sessions.get(chat_id)
        if session_id:
            session = self.core_processor.get_session(session_id)
            if session and session.get('relay_caller_session_id'):
                # This is a relay session that timed out
                log.info("Relay session expired",
                         extra={'data': f"chat_id={chat_id} session={session_id}"})

                # Tell Dean it expired
                await self.send_message(chat_id,
                    "The request has timed out — no reply needed.")

                # Tell Jesse that Dean didn't respond
                caller_session_id = session.get('relay_caller_session_id')
                target_user       = session.get('relay_target_user', 'unknown')
                question          = session.get('relay_question', '')
                target_friendly   = target_user

                if hasattr(self.core_processor, 'presence_registry'):
                    target_friendly = self.core_processor.presence_registry\
                        .get_friendly_name(target_user)
                    self.core_processor.presence_registry.mark_unavailable(
                        target_user, 'telegram', ttl=300
                    )

                if caller_session_id:
                    import threading
                    thread = threading.Thread(
                        target  = self.core_processor.process_input,
                        kwargs  = {
                            'input_text': (
                                f"[RELAY TIMEOUT]\n"
                                f"{target_friendly} did not respond to your question "
                                f"'{question}' in time. The relay has been cancelled. "
                                f"Let the user know naturally."
                            ),
                            'session_id': caller_session_id,
                        },
                        daemon = True,
                    )
                    thread.start()

                # Restore any suspended session for Dean
                self.pop_session(chat_id)

        await self._reset_session(chat_id)

    async def _reset_session(self, chat_id: str):
        """
        Clear the LLM session and local state for a chat_id.
        Called on /reset, /start, or after _expire_session.
        """
        session_id = self._sessions.pop(chat_id, None)
        if session_id:
            self.core_processor.close_session(session_id)
        self._session_stack.pop(chat_id, None)
        self._last_active.pop(chat_id, None)
        self._last_typing.pop(chat_id, None)

    def push_session(self, chat_id: str) -> str | None:
        """
        Suspend the current session for chat_id by pushing it onto the stack.
        Returns the suspended session_id, or None if no session was active.
        Called by contact_user when initiating a relay to this user.
        """
        session_id = self._sessions.pop(chat_id, None)
        if session_id:
            self._session_stack.setdefault(chat_id, []).append(session_id)
            log.info("Session pushed to stack",
                     extra={'data': f"chat_id={chat_id} session={session_id}"})
        return session_id

    def pop_session(self, chat_id: str, context_note: str = None) -> str | None:
        """
        Resume the most recently suspended session for chat_id.
        Optionally injects a context note into the resumed session's history
        so Supernova knows what happened while it was paused.
        Returns the resumed session_id, or None if stack was empty.
        Called by reply_to_caller when the relay completes.
        """
        stack = self._session_stack.get(chat_id, [])
        if not stack:
            return None
        session_id = stack.pop()
        if not stack:
            self._session_stack.pop(chat_id, None)
        self._sessions[chat_id] = session_id

        # Inject context note into resumed session history
        if context_note:
            session = self.core_processor.get_session(session_id)
            if session:
                from core.session_state import get_history
                get_history(session).append({
                    'role':    'system',
                    'content': context_note,
                })

        log.info("Session popped from stack",
                 extra={'data': f"chat_id={chat_id} session={session_id}"})
        return session_id

    def set_relay_session(self, chat_id: str, session_id: str):
        """
        Set a relay session as the active session for a chat_id.
        Called by contact_user after creating the relay session.
        """
        import time
        self._sessions[chat_id]    = session_id
        self._last_active[chat_id] = time.monotonic()
        log.info("Relay session set active",
                 extra={'data': f"chat_id={chat_id} session={session_id}"})

    def _get_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

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
                    log.error("Send error", extra={'data': await r.text()})
        except Exception as e:
            log.error("Send error", exc_info=True)

    def send_relay_message(self, endpoint_id: str, message: str):
        """
        Deliver a relay opening message to a user on this interface.
        endpoint_id for Telegram is the chat_id.
        Called by contact_user generically across interfaces.
        """
        loop = getattr(self, '_loop', None)
        if loop is None:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                log.error("No event loop available for send_relay_message")
                return
        import asyncio
        asyncio.run_coroutine_threadsafe(
            self.send_message(endpoint_id, message),
            loop,
        )