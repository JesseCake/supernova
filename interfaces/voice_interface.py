import uuid

class VoiceInterface:
    def __init__(self, core_processor):
        self.core_processor = core_processor
        self.session_id = str(uuid.uuid4())  # Generate a unique session ID for the voice session

    def start_listening(self):
        while True:
            input_text = self.listen_and_transcribe()

            # Feed the input to the core processor
            self.core_processor.process_input(input_text, self.session_id)

            # Wait for the response to be finished
            self.core_processor.get_session(self.session_id)['response_finished'].wait()

            # Process the response from the core processor
            response_queue = self.core_processor.get_session(self.session_id)['response_queue']
            while not response_queue.empty():
                response_text = response_queue.get()
                self.speak_text(response_text)
