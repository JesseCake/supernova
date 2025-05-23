import asyncio
import numpy as np
from wyoming.server import AsyncEventHandler
from wyoming.event import Event
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from whisper_live.vad import VoiceActivityDetector
from whisper_live.transcriber import WhisperModel
from TTS.api import TTS
import re
import uuid
import time 
import threading
from scipy.signal import resample # not great
import resampy
import logging

#for debugging audio:
import os
import soundfile as sf

def save_or_append_audio(filename, audio_int16, rate):
    if os.path.exists(filename):
        # Read existing audio
        existing_audio, _ = sf.read(filename, dtype='int16')
        # Concatenate
        combined_audio = np.concatenate((existing_audio, audio_int16))
    else:
        combined_audio = audio_int16

    # Write full audio back
    sf.write(filename, combined_audio, rate, subtype='PCM_16')

# Setup logging
logging.basicConfig(level=logging.INFO)

class VoiceRemoteInterface(AsyncEventHandler):
    def __init__(self, reader, writer, core_processor, model_path='tiny.en', **kwargs):
        super().__init__(reader, writer, **kwargs)
        self.core_processor = core_processor

        self.session_id = None

        # voice activity detection and transcription:
        self.listening_rate = 16000
        self.vad_detector = VoiceActivityDetector(threshold=0.3, frame_rate=self.listening_rate)
        self.frames_np = np.array([], dtype=np.float32)

        # TTS:
        self.speaking_rate = 16000
        self.tts_sample_rate = 22050  # built into the model so must use
        self.tts = TTS("tts_models/en/vctk/vits", progress_bar=False, )
        self.model_path = model_path
        self.transcriber = WhisperModel(model_path)
        self.speech_speed = 1.0
        self.speech_speaker = 'p376'
        self.sentence_endings = re.compile(r'(?<=[.!?])\s+')
        
        self.recording = False
        self.close_channel_phrase = "finish conversation"

    def add_frames(self, frame_np):
        self.frames_np = np.concatenate((self.frames_np, frame_np), axis=0) if self.frames_np.size > 0 else frame_np.copy()

    async def handle_event(self, event: Event) -> bool:
        """Handle incoming events from the Wyoming client."""
        #print(f"[debug] event.type = {event.type}")
        #print(f"[debug] event.payload = {event.payload}")
        #print(f"[debug] event.data = {event.data}")

        if AudioStart.is_type(event.type):
            print("start")
            self.current_audio = []
            print("trying to send back audio")
            #await self.speak_text("I'm here")
            await self.open_channel()
            await self.handover_channel()

        elif AudioChunk.is_type(event.type):
            # Gather the audio
            if event.payload is not None:
                audio_frame = np.frombuffer(event.payload, dtype=np.int16)
                #print("INT16 Max value:", np.max(audio_frame), "Min value:", np.min(audio_frame))
                audio_frame = audio_frame.astype(np.float32) / 32768.0  # convert to float32 (needed?)
                #print("FLOAT32 Max value:", np.max(audio_frame), "Min value:", np.min(audio_frame))

                if self.vad_detector(audio_frame=audio_frame):
                    #self.current_audio.append(audio_frame)
                    self.add_frames(audio_frame)
                    self.recording = True
                    # TODO: add a filter to allow more vacant frames before we send for transcription
                elif self.recording:
                    print("vad detected silence: sending to transcriber")
                    await self.transcribe_audio()
                    self.recording = False

            else:
                print("[remotevoice] Warning: received audio chunk with no payload!")

        elif AudioStop.is_type(event.type):
            print("stop")
            if not self.current_audio:
                return
            # Concatenate all audio chunks
            full_audio = np.concatenate(self.current_audio)
            # Run your transcription (ASR) logic
            segments, _ = self.transcriber.transcribe(full_audio)
            result_text = " ".join([segment.text for segment in segments]) if segments else ""
            print(f"[remote] Transcription: {result_text}")

            # use/create session id and run text through LLM logic:
            if self.session_id is None:
                self.session_id = str(uuid.uuid4())
                self.core_processor.create_session(self.session_id)
            self.core_processor.process_input(input_text=result_text, session_id=self.session_id, is_voice=True)
            session = self.core_processor.get_session(self.session_id)

            # We'll stream out sentences as they become available
            buffer = ""
            while True:
                if not session['response_queue'].empty():
                    response_chunk = session['response_queue'].get()
                    if response_chunk is None:
                        break
                    else:
                        buffer += response_chunk

                        # only split text up for speaking on complete sentences
                        sentences = self.sentence_endings.split(buffer)
                        for sent in sentences[:-1]: # All but last (could be incomplete)
                            if sent.strip():
                                await self.speak_text(sent.strip())
                        buffer = sentences[-1] # Keep any incomplete sentence in buffer
                else:
                    # Give the core a bit more time if needed (non-blocking)
                    await asyncio.sleep(0.01)
                    # Optionally, add a timeout here to avoid infinite waits

            if buffer.strip():
                await self.speak_text(buffer.strip())
        
        else:
            print(f"Event type: {event.type}")

        return True  # return False to close the connection

    async def stream_audio(self, audio_int16, chunk_samples=1024):
        """Stream int16 audio data to the Wyoming client."""
        for start in range(0, len(audio_int16), chunk_samples):
            chunk = audio_int16[start:start + chunk_samples].tobytes()
            await self.write_event(AudioChunk(audio=chunk, rate=self.speaking_rate, width=2, channels=1).event())

    async def speak_text(self, text):
        """Generate TTS audio from text and stream to the Wyoming client."""
        # print("SPEAKING TEXT: ", text)
        if text.strip():
            # Generate audio using your TTS engine
            tts_output_f32 = self.tts.tts(text, speed=self.speech_speed, speaker=self.speech_speaker)

            # Ensure it's a NumPy array
            tts_output_f32 = np.array(tts_output_f32, dtype=np.float32)

            # resample to the speaking rate
            tts_output_resampled = resampy.resample(tts_output_f32, self.tts_sample_rate, self.speaking_rate)

            # normalise audio before converting to int16
            #peak = np.max(np.abs(tts_output_resampled))
            #if peak > 0:
            #    tts_output_resampled /= peak

            # RMS normalize (more perceptually consistent than peak)
            rms = np.sqrt(np.mean(tts_output_resampled**2))
            target_rms = 0.2  # tweak this: 0.1 is conservative, 0.2 is punchy
            if rms > 0:
                tts_output_resampled *= target_rms / rms

            # Optional gain (careful — can push into distortion)
            tts_output_resampled = np.clip(tts_output_resampled * 1.2, -1.0, 1.0)

            # float → int16 little-endian
            audio_int16 = (np.clip(tts_output_resampled, -1.0, 1.0) * 32767).astype(np.int16)

            # Send AudioStart
            await self.write_event(AudioStart(rate=self.speaking_rate, width=2, channels=1).event())

            await self.stream_audio(audio_int16)
        else:
            print("[remote] Skipped speaking due to empty text.")
    
    async def handover_channel(self):
        await self.write_event(AudioStop().event())

    async def open_channel(self):
        await self.write_event(AudioStart(rate=self.listening_rate, width=2, channels=1).event())
        await self.speak_text("I'm here")

    async def close_channel(self):
        print("Closing voice channel from server...")
        self.session_id = None
        await self.generate_tone(300, 0.2, 0.2)
        # Send AudioStop to indicate we're done (we do this twice to signal we're closing the channel fully)
        await self.write_event(AudioStop().event())
        time.sleep(0.1)
        await self.write_event(AudioStop().event())

    async def generate_tone(self, frequency=440, duration=0.2, volume=0.5):
        """
        Generate a tone with the specified frequency, duration, and volume.

        :param frequency: Frequency of the tone in Hz (default is 440 Hz, which is A4).
        :param duration: Duration of the tone in seconds (default is 1.0 seconds).
        :param volume: Volume of the tone (0.0 to 1.0, default is 0.5).
        """
        sample_rate = self.speaking_rate
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        tone = np.sin(frequency * t * 2 * np.pi)
        audio = tone * (2 ** 15 - 1) / np.max(np.abs(tone))
        audio = audio * volume
        audio = audio.astype(np.int16)

        # stream the audio to the Wyoming client
        await self.stream_audio(audio)

    async def contact_core(self, input):
        # we will send the text to the core, and handle the return from the core

        # initialise session if not already done:
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            self.core_processor.create_session(self.session_id)

        # Run the input processing in a separate thread (so we can leave it running and process the return in chunks
        process_thread = threading.Thread(
            target=self.core_processor.process_input,
            kwargs={"input_text": input, "session_id": self.session_id, "is_voice": True}
        )
        process_thread.start()

        session = self.core_processor.get_session(self.session_id)  # get the session object from the core to interact with

        # buffer for complete sentences:
        buffer = ""

        # Define sentence-ending punctuation
        sentence_endings = re.compile(r'(?<=[.!?])\s+')

        # Stream response chunks incrementally so that we respond more quickly
        while True:
            if not session['response_queue'].empty():
                response_chunk = session['response_queue'].get()
                if response_chunk is None:
                    print('RESPONSE FINISHED')
                    break
                else:
                    buffer += response_chunk

                    # only split text up for speaking on complete sentences
                    sentences = sentence_endings.split(buffer)
                    for sent in sentences[:-1]:  # All but last (could be incomplete)
                        if sent.strip():
                            await self.speak_text(sent.strip())
                    buffer = sentences[-1]  # Keep any incomplete sentence in buffer


        # After loop, ensure all remaining text is spoken
        if buffer.strip():
            await self.speak_text(buffer.strip())

        if session['close_voice_channel'].is_set():
            print("voice: Closing voice channel")
            return True
        else:
            return False
        
    async def transcribe_audio(self):
        if self.frames_np.size > 0:
            print(f"Transcribing {self.frames_np.size} samples")
            try:
                segments, _ = self.transcriber.transcribe(self.frames_np)
                if segments:
                    result_text = " ".join([segment.text for segment in segments])
                    logging.info(f"Transcription result: {result_text}")
                    if self.close_channel_phrase in result_text.lower():
                        await self.close_channel()
                    else:
                        # we have successful transcription, let's send it to the agent and handle return:
                        close_channel = await self.contact_core(result_text)

                        if close_channel:
                            print(f"Transcribe: Closing voice channel")
                            await self.close_channel()
                        else:
                            # generate tone to let us know speaking is finished (but conversation continues):
                            await self.generate_tone(800, 0.1, 0.2)
                            # Handover the channel to the person speaking
                            await self.handover_channel()
                else:
                    logging.info("No transcription result.")
            except Exception as e:
                logging.error(f"Error during transcription: {e}")
            finally:
                self.frames_np = np.array([], dtype=np.float32)
        else:
            logging.info("No audio data to transcribe")

