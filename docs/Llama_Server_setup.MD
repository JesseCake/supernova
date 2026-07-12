This is the current command I'm using to launch llama-server on the endpoint where it lives:

~/llama.cpp/build/bin/llama-server -hf unsloth/Qwen3.5-9B-GGUF:Q4_K_XL   --jinja -c 393216 --parallel 3 --flash-attn on   -ctk q4_0 -ctv q4_0 --reasoning off   --host 0.0.0.0 --port 8080 -b 8192 -ub 1024   --fit-ctx 360448 --cache-ram 2048 --temperature 0.7 --top-p 0.8 --top-k 20 --min-p 0.0 --presence-penalty 1.5 --repeat-penalty 1.0

There is some tight tuning here as I'm running it on a salvaged AMD BC250 crypto mining rig with shared 16GB RAM between system and GPU.

If you want to disable the image pipeline to save some RAM, add the "--no-mmproj" flag to the server.

TODO: Improve this documentation


#Building llama.cpp

I use the AMD BC250 for my inference, and I like to keep it bleeding edge especially with improvements to Vulkan happening all the time as well as KV cache, so I build like this:

cd ~
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_VULKAN=ON
cmake --build build --config Release -j2 --target llama-server

(I use -j2 because even though we can parallel build across all cores, we run out of RAM easily and crash during build)


#Rebuilding
If I want to update to the latest sources and rebuild I do this:

cd ~/llama.cpp
rm -rf build
git pull
cmake -B build -DGGML_VULKAN=ON
cmake --build build --config Release -j2 --target llama-server

