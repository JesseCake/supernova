# Asterisk ARI Interface — Setup Guide

---

## Overview

This setup connects a Grandstream HT802 ATA (or any analogue telephone adaptor really) with a vintage phone to the Supernova
agent via Asterisk running on the Jetson. Calls are handled via Asterisk's ARI
(Asterisk REST Interface), which gives the Python agent direct control over calls
and audio via RTP.

## WARNING WARNING WARNING
THIS IS EXPERIMENTAL AND INTENDED FOR INTERNAL LINKS ONLY
NEVER EVER CONNECT THIS OPEN THE OPEN WEB OR ALLOW THIS TO DO DANGEROUS THINGS ON OPEN PHONES
YOU USE THIS CONNECTOR AND CODE AT YOUR OWN PERIL

Architecture:
  Vintage phone → HT802 ATA → Asterisk (Jetson) → AsteriskInterface (Python, Jetson)
                                                         ↓
                                               CoreProcessor / Whisper / Piper

---

## Part 1 — File placement

### Asterisk config files → /etc/asterisk/
Back up the originals first, then copy:

  sudo cp /etc/asterisk/http.conf /etc/asterisk/http.conf.bak
  sudo cp /etc/asterisk/ari.conf /etc/asterisk/ari.conf.bak
  sudo cp /etc/asterisk/pjsip.conf /etc/asterisk/pjsip.conf.bak
  sudo cp /etc/asterisk/extensions.conf /etc/asterisk/extensions.conf.bak

  sudo cp http.conf ari.conf pjsip.conf extensions.conf /etc/asterisk/
  sudo chown asterisk:asterisk /etc/asterisk/http.conf \
                                /etc/asterisk/ari.conf \
                                /etc/asterisk/pjsip.conf \
                                /etc/asterisk/extensions.conf

### Python files → your project

  asterisk_interface.py → interfaces/asterisk_interface.py
  settings.py           → config/settings.py          (replaces existing — adds AsteriskConfig)
  settings.yaml         → config/settings.yaml        (replaces existing — adds asterisk section)
  main.py               → main.py                     (replaces existing — wires in AsteriskInterface)

Note: settings.py adds the AsteriskConfig dataclass and wires it into AppConfig. If you have
local changes to settings.py, merge carefully rather than blindly replacing — the key additions
are the AsteriskConfig dataclass and the asterisk field in AppConfig and load_config().

---

## Part 2 — Asterisk config files

### http.conf
Enables Asterisk's built-in HTTP server, which ARI runs on top of.

What to edit:
  - Nothing needs changing for the standard setup (Asterisk and Python on same Jetson).
  - bindaddr = 127.0.0.1 → only change if you move the Python agent to a different
    machine. In that case set to the Jetson LAN IP (192.168.20.200) and update
    settings.yaml → asterisk.ari_host to match.
  - bindport = 8088 → only change if 8088 is already in use. Update
    settings.yaml → asterisk.ari_port to match.

Full file should look like:
  [general]
  servername=Asterisk
  enabled=yes
  bindaddr=127.0.0.1
  bindport=8088

---

### ari.conf
Defines the ARI user that the Python agent authenticates with.

What to edit:
  - password = changeme → set a real password. Must match settings.yaml → asterisk.ari_password.
  - The section name [supernova] is the username. If you change it, update
    settings.yaml → asterisk.ari_user to match.

Full file should look like:

  [general]
  enabled = yes
  pretty = yes
  allowed_origins = 127.0.0.1

  [supernova]
  type = user
  read_only = no
  password = yourpassword

---

### pjsip.conf
Defines the SIP transport and the HT802 ATA as an endpoint (extension 100).

Three sections are required per endpoint in PJSIP:
  [100]       — the endpoint (codec, context, which auth + aor to use)
  [100-auth]  — the SIP username and password the HT802 registers with
  [100]       — address of record (tracks where the phone is registered) - yes this appears as a duplicate, but the type sets it apart

What to edit:
  - password = changeme in [100-auth] → set a real password.
    This must match what you enter in the HT802 web UI → SIP Account → SIP Password.
  - permit = 192.168.20.0/24 in [100-acl] → update to match your LAN subnet if different.
    This blocks any registration attempt from outside your local network.
  - context = internal in [100] → leave as-is unless you rename the dialplan context.
  - allow = !all,ulaw → leave as-is. This locks the codec to G.711 µ-law which is
    what the HT802 uses and what our audio pipeline expects.
  - direct_media = no → do not change. This forces audio through Asterisk which is
    required for ARI external media to work.

If you add more phones later, add another set of three sections ([101], [101-auth],
[101] - aor) following the same pattern.

Full file could look like:
  [transport-udp]
  type = transport
  protocol = udp
  bind = 0.0.0.0:5060

  ;endpoint for first phone
  [100]
  type = endpoint
  context = internal              ; which dialplan context calls from this phone enter
  allow = !all,ulaw               ; strip all codecs, then allow ulaw (G.711 µ-law) only
  auth = 100-auth                 ; reference to auth section below
  aors = 100                      ; reference to aor section below
  acl = lan-only                  ; access control to limit to LAN
  direct_media = no               ; force all media through Asterisk — required for ARI external media
  rtp_symmetric = yes             ; send RTP to the port we receive from — good practice on LAN
  dtmf_mode=rfc4733               ; This is important so that tones are intercepted properly for passthrough to Asterisk Agent interface to use

  [100-auth]
  type = auth
  auth_type = userpass
  username = 100
  password = 100supernova             ; must match HT802 web UI → SIP Account → SIP Password

  [100]
  type = aor
  max_contacts = 1                ; only allow one registration at a time
  remove_existing = yes           ; if phone re-registers, replace old entry rather than reject


---

### acl.conf

This file controls access. pjsip.conf references access controls of "lan-only" which we'll define. of course you'll need to set your LAN subnet which will probably be different to mine (mine is 192.168.20.x). You must put in this file:

[lan-only]
deny = 0.0.0.0/0.0.0.0
permit = 192.168.20.0/24



---

### extensions.conf
This file is the dialplan — it controls what happens when any number is dialled.
The default file installed by apt is a large sample file full of examples. We replace
it entirely with our own minimal dialplan.

#### How the dialplan works
Each line is either an extension definition or a continuation of the previous one:

  exten => <number/pattern>,<priority>,<application>
  same  => n,<application>          ; "n" means "next priority" — continues the same extension

Patterns use special characters:
  X     matches any digit 0-9
  _1XX  matches any 3-digit number starting with 1 (so 100-199)
  _0.   matches 0 followed by anything (one or more digits)
  _.    matches absolutely anything

Priorities run top to bottom. If a call reaches Hangup(), it ends.
If nothing matches the dialled number, the catch-all _. at the bottom hangs up cleanly.

#### What our dialplan does
  _1XX  — rings the matching PJSIP endpoint directly so phones can call each other.
          The pattern covers 100-199 so any phone you add in that range works
          automatically without touching the dialplan.

  900   — this is the agent entry point. The HT802 is configured (in Part 4) to
          auto-dial 900 on pickup. Asterisk answers the call and hands it to the
          Supernova ARI app via Stasis(supernova). Do not change the app name —
          it must match settings.yaml → asterisk.ari_user and [supernova] in ari.conf.

  _0.   — outside line placeholder. Dials 0 + any number. Currently plays a "not
          available" sound since no VOIP trunk is configured yet. When you add a
          trunk later, replace the Playback line with:
            same => n,Dial(PJSIP/${EXTEN:1}@your-trunk)
          The :1 strips the leading 0 before sending the number to the provider.

  _.    — catch-all. Hangs up anything that doesn't match the above cleanly.

#### What to change
  - Dial(PJSIP/${EXTEN},30) — the 30 is seconds to ring before giving up.
    Increase if you want phones to ring longer before the call fails.
  - Everything else can be left exactly as provided.
  - Remember, if you're anywhere other than the use, change your country code in /etc/asterisk/indications.conf to your country (here in Australia it's 'au')

#### Full file contents
Copy this exactly into /etc/asterisk/extensions.conf:

  ; ============================================================
  ; extensions.conf — Dialplan
  ; ============================================================

  [globals]
  ; Nothing global needed yet

  ; ============================================================
  ; internal — context for all internal SIP endpoints
  ; ============================================================
  [internal]

  ; --- Direct extension dialling (phones call each other) ---
  ; Any 3-digit extension starting with 1 rings it directly.
  ; Covers 100-199 so any phone you add in that range works automatically.
  exten => _1XX,1,Dial(PJSIP/${EXTEN},30)
  same => n,Hangup()

  ; --- Agent: auto-dials to AI agent on pickup ---
  ; HT802 is configured to dial 900 on pickup
  ; Asterisk answers and hands the call to the Supernova ARI app.
  ; In the app, we add the ability to hand back to next step in dialplan if user dials zero
  ; If Supernova isn't running, we should fall into the next step anyway which will be a dialtone
  exten => 900,1,NoOp(Incoming call to Supernova AI agent)
  same => n,Answer()
  same => n,Stasis(supernova)
  same => n,NoOp(ARI app unavailable - failing over to dial tone)
  same => n,Goto(internal,transfer,1)

  ; dial tone / transfer target
  exten => transfer,1,NoOp(Transferred from Supernova)
  same => n,Playtones(dial)
  same => n,WaitExten(30)
  same => n,StopPlaytones()
  same => n,Hangup()

  ; --- Catch-all: anything else hangs up cleanly ---
  exten => _.,1,Hangup()


---

## Part 3 — Reload and verify Asterisk

### 1. Reload config (no restart needed)
  sudo asterisk -rx "core reload"

  If anything looks wrong, a full restart is cleaner:
  sudo systemctl restart asterisk

### 2. Check PJSIP loaded correctly
  sudo asterisk -rx "pjsip show endpoints"
  # Should show endpoint 100 listed

### 3. Verify ARI is responding
  curl -u supernova:yourpassword http://127.0.0.1:8088/ari/asterisk/info
  # Should return a JSON block with Asterisk version info
  # If you get connection refused, check http.conf and restart Asterisk

### 4. Verify ARI modules are all loaded
  sudo asterisk -rx "module show like res_ari"
  sudo asterisk -rx "module show like res_http_websocket"
  sudo asterisk -rx "module show like res_ari_channels"
  # All should show status: Running

---

## Part 4 — HT802 ATA configuration

Log into the HT802 web UI. Default address is printed on the underside of the unit.
Default credentials: admin / admin

### Basic Settings → Account 1 (SIP Account):
  Account Active:        Yes
  SIP Server:            192.168.20.200      (Jetson LAN IP)
  SIP User ID:           100
  Authenticate ID:       100
  Authenticate Password: yourpassword        (must match pjsip.conf [100-auth] password)
  Name:                  Vintage Phone       (or whatever you like — shows on caller ID)

### Advanced Settings → Call Features:
  To auto-connect to the agent on pickup, use one of these two options:

  Option A — Hotline (simplest):
    Hotline Number: 900
    This dials 900 automatically the moment the handset is lifted.
    Downside: you cannot dial other extensions manually.

  Option B — Dial Plan (most flexible):
    In the Dial Plan field enter:   { 900 | x+ }
    This dials 900 on pickup but still allows you to dial other numbers
    (the x+ pattern matches anything else you dial manually).

### Codec Settings:
  Preferred Vocoder:     PCMU    (this is ulaw / G.711 µ-law)
  2nd Preferred Vocoder: None
  Disable all others

### After making changes:
  Click "Apply" then "Reboot" at the top of the HT802 web UI.
  After reboot, verify registration in Asterisk:
    sudo asterisk -rx "pjsip show contacts"
    # Should show extension 100 with status: Reachable

---

## Part 5 — Python setup

### Install dependency
  pip install aiohttp
  # aiohttp handles ARI HTTP calls and the WebSocket event connection

### Check Python version
  python3 --version
  # If 3.13 or higher, audioop was removed from stdlib. Install the backport:
  pip install audioop-lts

### Update settings.yaml

  # Fill in your ARI credentials:
  asterisk:
    ari_host: "127.0.0.1"        # leave as-is (Asterisk is local)
    ari_port: 8088               # leave as-is unless you changed http.conf
    ari_user: "supernova"        # leave as-is unless you changed the username in ari.conf
    ari_password: "yourpassword" # must match password in ari.conf
    rtp_local_ip: "127.0.0.1"   # leave as-is (RTP stays local on Jetson)

---

## Part 6 — Test a call

1. Start the Python agent as normal (python main.py)
   You should see: [asterisk] Connecting to ARI at 127.0.0.1:8088
   Then:           [asterisk] ARI WebSocket connected.

2. Lift the handset on the vintage phone.
   The HT802 should auto-dial 900.

3. You should hear the agent greet you ("Hello, I'm here.")

4. If you hear nothing or get a fast busy:
   - Check sudo asterisk -rx "pjsip show contacts" — is 100 registered?
   - Check sudo asterisk -rx "ari show apps" — is supernova listed?
   - Check the Python agent log for [asterisk] errors