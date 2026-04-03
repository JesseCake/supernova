import threading
import asyncio
from core.core import CoreProcessor

# Config
from core.settings import load_config

# Logger
from core.logger import setup_logging, get_logger

# Interfaces
from interfaces.web_interface import WebInterface
from interfaces.speaker_remote_interface import SpeakerRemoteInterface
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

    # Logging
    setup_logging(debug=config.debug.verbose, log_dir='logs')
    log = get_logger('launcher')

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

    # Store loop on core so tools can post coroutines onto it from threads
    core_processor._loop = loop

    # ── Asterisk interface ────────────────────────────────────────────────────
    if config.interfaces.asterisk:
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
                log.warning("Asterisk call event has no endpoint_id")
                return
            asyncio.run_coroutine_threadsafe(
                asterisk.initiate_call(caller_number, announcement),
                loop,
            )

        core_processor.register_event_handler('asterisk', _asterisk_call_handler)
        log.info("Asterisk interface starting", extra={'data': f"{config.asterisk.ari_host}:{config.asterisk.ari_port}"})

    # ── Voice remote interface ────────────────────────────────────────────────
    if config.interfaces.speaker:
        vr = SpeakerRemoteInterface(
            core_processor, 
            transcriber=whisper_model, 
            vad=vad,
            piper_voice=piper_voice,
            )

        # Store loop reference so the scheduler can post initiate_call() onto it
        vr._loop = loop

        # Register interface so tools can reach it generically
        core_processor.register_interface('speaker', vr)

        # Register the voice call handler for scheduled events
        def _voice_call_handler(event):
            endpoint_id  = event.get('endpoint_id', '')
            announcement = event.get('announcement', '')
            if event.get('missed'):
                announcement = f"[Missed while server was offline] {announcement}"
            if not endpoint_id:
                log.warning("Speaker call event has no endpoint_id", extra={'data': f"label={event.get('label')!r}"})
                return
            asyncio.run_coroutine_threadsafe(
                vr.initiate_call(endpoint_id, announcement),
                loop,
            )

        core_processor.register_event_handler('speaker', _voice_call_handler)

        loop.create_task(vr.run(
            host=config.server.remote_voice_host,
            port=config.server.remote_voice_port,
        ))
        log.info("Speaker remote starting", extra={'data': f"{config.server.remote_voice_host}:{config.server.remote_voice_port}"})

    # ── Telegram IM interface ─────────────────────────────────────────────────
    if config.telegram.enabled:
        telegram = TelegramInterface(
            core_processor,
            config=config,
        )
        loop.create_task(telegram.run())

        # Register interface so tools can reach it generically
        core_processor.register_interface('telegram', telegram)
        telegram._loop = loop

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
        log.info("Telegram interface starting")

    # ── Web / Gradio interface ────────────────────────────────────────────────
    if config.interfaces.web:
        def _run_web():
            # Give Gradio its own isolated event loop so it doesn't interfere
            # with the shared main loop above.
            asyncio.set_event_loop(asyncio.new_event_loop())
            WebInterface(core_processor).run()

        web_thread = threading.Thread(target=_run_web, daemon=True)
        web_thread.start()
        log.info("Web interface starting")

    # ── Run forever ───────────────────────────────────────────────────────────
    # All async interfaces are now scheduled as tasks on the shared loop.
    # This call blocks the main thread and drives everything until shutdown.
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        loop.close()