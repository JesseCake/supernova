# SearXNG Setup on Jetson Orin NX
*Self-hosted search engine for Supernova AI Assistant*

## Why?
The internet in general is becoming unfriendly to "bots" crawling around, though in my opinion they've always been there, but controlled by large companies. If you're like me and have noticed rate limits getting increasingly difficult, then you'll be searching for a way to have your bot still research deeply and find answers. I've found SearXNG to be quite great! These instructions are based on my experience setting this up on the same Jetson device that hosts Supernova for me: my NVIDIA Jetson Orin 16GB.

## Overview

SearXNG is a self-hosted meta-search engine that queries multiple search providers (Google, Bing, DuckDuckGo, etc.) simultaneously. It replaces the DuckDuckGo (DDGS) library, which was prone to rate limiting and returning garbage results under heavy use.

Since it runs on your own hardware there are no rate limits or API keys required. SearXNG is lightweight (~100-200MB RAM) with negligible CPU/GPU footprint at idle, making it well-suited to run alongside Ollama, Whisper, and Home Assistant on the Jetson Orin NX 16GB.

---

## Prerequisites

- Jetson Orin NX 16GB (or similar ARM64 device)
- Docker installed (see Step 1 if not already)
- Internet access from the Jetson

---

## Step 1: Install Docker

The install script auto-detects ARM64 architecture.

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

Verify:

```bash
docker --version
docker compose version
```

---

## Step 2: Create Directory Structure

```bash
mkdir ~/searxng && cd ~/searxng
mkdir config
```

---

## Step 3: Create docker-compose.yml

```bash
cat > ~/searxng/docker-compose.yml << 'EOF'
services:
  searxng:
    image: searxng/searxng
    ports:
      - "8888:8080"
    volumes:
      - ./config:/etc/searxng
    restart: unless-stopped
EOF
```

---

## Step 4: Start Container and Extract Settings

Start the container first so it generates a default settings file, then copy it out to edit:

```bash
docker compose -f ~/searxng/docker-compose.yml up -d

# Wait a few seconds, then copy out the generated settings:
docker cp $(docker ps -q --filter "name=searxng"):/etc/searxng/settings.yml ~/searxng/config/settings.yml
```

> **Note:** Don't create settings.yml manually before first run — SearXNG will ignore it and generate its own. Always let it generate first, then edit the copy.

---

## Step 5: Edit settings.yml

Two changes are required:

1. **Enable JSON format** — so results can be returned as JSON for programmatic use
2. **Disable the bot limiter** — SearXNG blocks requests without `X-Forwarded-For` headers by default, causing 403 errors on local requests

```bash
nano ~/searxng/config/settings.yml
```

Find the `search:` section and add `json` to formats:

```yaml
search:
  formats:
    - html
    - json
```

Find or add the `server:` section and set limiter to false:

```yaml
server:
  limiter: false
```

---

## Step 6: Restart and Test

```bash
docker compose -f ~/searxng/docker-compose.yml restart

curl "http://localhost:8888/search?q=test&format=json" | python3 -m json.tool | head -50
```

You should see a JSON response with a `results` array.

---

## Step 7: Update core.py

Replace `_perform_web_search` to use SearXNG instead of DDGS:

```python
def _perform_web_search(self, query, num_responses):
    self._log("_perform_web_search start", extra=f"q={query} n={num_responses}")
    try:
        response = requests.get(
            "http://localhost:8888/search",
            params={
                "q": query,
                "format": "json",
                "language": "en-AU",
                "safesearch": 1,
            },
            timeout=10,
        )
        data = response.json()
        results = []
        for r in data.get("results", [])[:num_responses]:
            title = r.get("title", "")
            title = title.split("›")[-1].strip() if "›" in title else title
            results.append({
                "title": title[:100],
                "snippet": r.get("content", "")[:200],
                "link": r.get("url"),
            })
        if not results:
            results.append({"error": "No results found"})
        self._log("_perform_web_search end", extra=f"found={len(results)}")
        return results
    except Exception as e:
        self._log("_perform_web_search error", extra=str(e))
        return [{"error": f"Search failed: {e}"}]
```

The `ddgs` import can be removed from `core.py` once confirmed working.

### Also update _wrap_tool_result

Add `ensure_ascii=False` to allow non-ASCII characters (e.g. Japanese) to pass through as raw UTF-8 rather than escaped unicode sequences, saving tokens and improving translation quality:

```python
def _wrap_tool_result(self, name, payload):
    return json.dumps({
        "tool_result": {
            "name": name,
            "content": payload
        }
    }, ensure_ascii=False)
```

---

## Troubleshooting

### 403 Forbidden
Ensure `limiter: false` is set under `server:` in `settings.yml` and restart the container.

### settings.yml created from template / ignored
The container generates its own settings on first run if the file doesn't exist. Always start the container first, copy out the generated file, edit it, then restart.

### Empty or no results
Check that `json` is listed under `search: formats:` in `settings.yml`.

### Check container logs
```bash
docker compose -f ~/searxng/docker-compose.yml logs --tail=50
```

---

## Auto-start on Boot

The `restart: unless-stopped` policy means SearXNG starts automatically after reboot as long as Docker starts on boot:

```bash
sudo systemctl enable docker
```

---

## Notes

- SearXNG queries multiple engines simultaneously — results are generally better than single-provider searches
- No rate limits since it runs on your own hardware
- The SearXNG URL (`http://localhost:8888`) should be added to `AppConfig`/`settings.yaml` rather than hardcoded in `core.py`