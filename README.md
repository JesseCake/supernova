The beginnings of a personal locally run AI assistant. 
Aiming for function calling and integration into more complex environments.

Leverages Ollama to run the actual LLM (so will require installation of it separately)

The VAD and transcriber files in the whisper_live folder are from:
https://github.com/AIWintermuteAI/WhisperLive


So far only for Linux (currently runs on my Ubuntu 22.04 laptop)

I'm leveraging Llama3 mostly here, but you can also customise with a modelfile (included here as supernova.modelfile):
`ollama create supernova -f ./supernova.modelfile`
and of course adjust the code to use the model "supernova" in place of llama3