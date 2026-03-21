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
    """

    def __init__(self, core_processor, token: str):
        self.core_processor = core_processor
        self.token          = token
        self.base_url       = f"https://api.telegram.org/bot{token}"
        self._offset        = 0
        self._sessions      = {}   # chat_id → session_id

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
        """Long-poll for new messages."""
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
            if text:
                asyncio.create_task(self._handle_message(chat_id, text))

    async def _handle_message(self, chat_id: str, text: str):
        """Route an incoming message through core."""
        # Create or reuse session for this chat
        if chat_id not in self._sessions:
            session_id = str(uuid.uuid4())
            self.core_processor.create_session(session_id)
            core_session = self.core_processor.get_session(session_id)
            if core_session is not None:
                core_session['endpoint_id'] = chat_id
                core_session['interface']   = 'telegram'
            self._sessions[chat_id] = session_id
        else:
            session_id = self._sessions[chat_id]

        # Run LLM in thread so we don't block the event loop
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

        # Drain response queue and send back
        core_session = self.core_processor.get_session(session_id)
        response     = []
        while True:
            chunk = await asyncio.to_thread(core_session['response_queue'].get)
            if chunk is None:
                break
            response.append(chunk)

        full_response = "".join(response).strip()
        if full_response:
            await self.send_message(chat_id, full_response)

    async def send_message(self, chat_id: str, text: str):
        """Send a message to a Telegram chat."""
        try:
            async with self._http.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            ) as r:
                if r.status != 200:
                    print(f"[telegram] Send error: {await r.text()}")
        except Exception as e:
            print(f"[telegram] Send error: {e}")