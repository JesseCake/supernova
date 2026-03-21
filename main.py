import threading
import asyncio
from core.core import CoreProcessor

# Config
from core.settings import load_config

# Interfaces
from interfaces.web_interface import WebInterface
from interfaces.voice_remote import VoiceRemoteInterface
from interfaces.asterisk_interface import AsteriskInterface
from interfaces.telegram_interface import TelegramInterface

# Shared inference instances — created once here so no interface loads its own.
# Both Whisper and VAD are passed into whichever interfaces need them.
from faster_whisper import WhisperModel
from whisper_live.vad import VoiceActivityDetector
from piper import PiperVoice

if __name__ == "__main__":

    # ── Config + core ─────────────────────────────────────────────────────────
    config         = load_config()
    core_processor = CoreProcessor(config)

    # Shared inference instances — created once, passed into all interfaces
    whisper_model = WhisperModel(model_size_or_path="base.en")
    vad           = VoiceActivityDetector(threshold=0.5, frame_rate=16000)
    piper_voice   = PiperVoice.load(
        config.voice.model_path,
        use_cuda=config.voice.use_cuda,
    )

    # ── Shared async event loop ───────────────────────────────────────────────
    # One loop drives all async interfaces (voice_remote, asterisk, future IM etc.).
    # Each async interface is scheduled as a task on this loop.
    # Synchronous interfaces (web/Gradio) run in daemon threads alongside it.
    # The scheduler uses run_coroutine_threadsafe to post back onto this loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # ── Asterisk interface ────────────────────────────────────────────────────
    if config.asterisk.enabled:
        asterisk = AsteriskInterface(
            core_processor,
            config,
            transcriber=whisper_model,
            vad=vad,
            piper_voice = piper_voice,
        )
        loop.create_task(asterisk.run())

        # Register handler so scheduled events can initiate Asterisk calls
        def _asterisk_call_handler(event):
            announcement  = event.get('announcement', '')
            caller_number = event.get('endpoint_id', '')
            if event.get('missed'):
                announcement = f"[Missed while offline] {announcement}"
            if not caller_number:
                print(f"[main] asterisk_call event has no endpoint_id")
                return
            asyncio.run_coroutine_threadsafe(
                asterisk.initiate_call(caller_number, announcement),
                loop,
            )

        core_processor.register_event_handler('asterisk', _asterisk_call_handler)
        print(f"[main] asterisk interface starting, connecting to ARI at "
              f"{config.asterisk.ari_host}:{config.asterisk.ari_port}")

    # ── Voice remote interface ────────────────────────────────────────────────
    if config.interfaces.voice_remote:
        vr = VoiceRemoteInterface(
            core_processor, 
            transcriber=whisper_model, 
            vad=vad,
            piper_voice=piper_voice,
            )

        # Store loop reference so the scheduler can post initiate_call() onto it
        vr._loop = loop

        # Store vr on core so tools can reach it if needed
        core_processor.voice_remote = vr

        # Register the voice call handler for scheduled events
        def _voice_call_handler(event):
            endpoint_id  = event.get('endpoint_id', '')
            announcement = event.get('announcement', '')
            if event.get('missed'):
                announcement = f"[Missed while server was offline] {announcement}"
            if not endpoint_id:
                print(f"[main] voice_call event has no endpoint_id: {event.get('label')!r}")
                return
            asyncio.run_coroutine_threadsafe(
                vr.initiate_call(endpoint_id, announcement),
                loop,
            )

        core_processor.register_event_handler('voice_remote', _voice_call_handler)

        loop.create_task(vr.run(
            host=config.server.remote_voice_host,
            port=config.server.remote_voice_port,
        ))
        print(f"[main] voice_remote starting on "
              f"{config.server.remote_voice_host}:{config.server.remote_voice_port}")

    # ── Telegram IM interface ─────────────────────────────────────────────────
    if config.telegram.enabled:
        telegram = TelegramInterface(
            core_processor,
            token = config.telegram.token,
        )
        loop.create_task(telegram.run())

        def _telegram_handler(event):
            chat_id      = event.get('endpoint_id', '')
            announcement = event.get('announcement', '')
            if not chat_id:
                return
            asyncio.run_coroutine_threadsafe(
                telegram.send_message(chat_id, announcement),
                loop,
            )

        core_processor.register_event_handler('telegram', _telegram_handler)
        core_processor.register_presence_check('telegram',
            lambda endpoint_id: True
        )
        print(f"[main] telegram interface starting")
        
    # ── Web / Gradio interface ────────────────────────────────────────────────
    if config.interfaces.web:
        def _run_web():
            # Give Gradio its own isolated event loop so it doesn't interfere
            # with the shared main loop above.
            asyncio.set_event_loop(asyncio.new_event_loop())
            WebInterface(core_processor).run()

        web_thread = threading.Thread(target=_run_web, daemon=True)
        web_thread.start()
        print(f"[main] web interface starting")

    # ── Run forever ───────────────────────────────────────────────────────────
    # All async interfaces are now scheduled as tasks on the shared loop.
    # This call blocks the main thread and drives everything until shutdown.
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("\n[main] shutting down...")
    finally:
        loop.close()