import threading
import asyncio
from core.core import CoreProcessor

# Config
from core.settings import load_config

# Interfaces
from interfaces.web_interface import WebInterface
from interfaces.voice_remote import VoiceRemoteInterface
from interfaces.asterisk_interface import AsteriskInterface

# Shared inference instances — created once here so no interface loads its own.
# Both Whisper and VAD are passed into whichever interfaces need them.
from faster_whisper import WhisperModel
from whisper_live.vad import VoiceActivityDetector
whisper_model = WhisperModel(model_size_or_path="base.en")
vad           = VoiceActivityDetector(threshold=0.5, frame_rate=16000)


if __name__ == "__main__":

    # ── Config + core ─────────────────────────────────────────────────────────
    config         = load_config()
    core_processor = CoreProcessor(config)

    # ── Shared async event loop ───────────────────────────────────────────────
    # One loop drives all async interfaces (voice_remote, asterisk, future IM etc.).
    # Each async interface is scheduled as a task on this loop.
    # Synchronous interfaces (web/Gradio) run in daemon threads alongside it.
    # The scheduler uses run_coroutine_threadsafe to post back onto this loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # ── Asterisk interface ────────────────────────────────────────────────────
    if config.interfaces.asterisk:
        asterisk = AsteriskInterface(
            core_processor,
            config,
            transcriber=whisper_model,
            vad=vad,
        )
        loop.create_task(asterisk.run())

        # Register handler so scheduled events can initiate Asterisk calls
        def _asterisk_call_handler(event):
            # TODO: implement when Asterisk scheduling is needed
            print(f"[main] asterisk_call event fired: {event.get('label')!r} — not yet implemented")

        core_processor.register_event_handler('asterisk_call', _asterisk_call_handler)
        print(f"[main] asterisk interface starting, connecting to ARI at "
              f"{config.asterisk.ari_host}:{config.asterisk.ari_port}")

    # ── Voice remote interface ────────────────────────────────────────────────
    if config.interfaces.voice_remote:
        vr = VoiceRemoteInterface(core_processor, transcriber=whisper_model, vad=vad)

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

        core_processor.register_event_handler('voice_call', _voice_call_handler)

        loop.create_task(vr.run(
            host=config.server.remote_voice_host,
            port=config.server.remote_voice_port,
        ))
        print(f"[main] voice_remote starting on "
              f"{config.server.remote_voice_host}:{config.server.remote_voice_port}")

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