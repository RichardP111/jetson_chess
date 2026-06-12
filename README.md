# ♟️ Jetson Smart Chess Board

<p align="center">
  <strong>AI-powered smart chess board running entirely on a Jetson Orin Nano</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Jetson%20Orin%20Nano-blue?style=flat-square"/>
  <img src="https://img.shields.io/badge/Python-3.10+-yellow?style=flat-square"/>
  <img src="https://img.shields.io/badge/Engine-Stockfish-green?style=flat-square"/>
  <img src="https://img.shields.io/badge/Hardware-DIY-orange?style=flat-square"/>
</p>

---

## 🚀 Overview

A fully self-contained smart chess board powered by the **NVIDIA Jetson Orin Nano**.



- ♟️ Three game modes: AI (Stockfish), Online (Lichess), Local 2-Player
- 🎯 Pawn promotion selector (Q R B N) via coloured LED quadrants
- ↩️ Undo last move (button combo or web dashboard)
- 📊 Eval bar on OLED and web dashboard
- 🔊 Voice announcements — auto-enabled when a USB speaker is connected
- 💾 USB game save — auto-saves PGN if a USB drive is plugged in
- 🧠 Post-game blunder analysis (top-3 mistakes highlighted)
- 🌐 Web dashboard with move input, custom LED theme builder, PGN download, OTA update
- ⚡ Fast startup (~1 second instead of 64)
- 🧩 Central `config.py` — no more scattered magic numbers

---

## ✨ Features

### Game Modes
| Button | Mode | Description |
|--------|------|-------------|
| 1 | vs AI | Stockfish on-device, configurable difficulty 1-20 |
| 2 | Online | Lichess board API (requires token) |
| 3 | Local 2P | Pass-and-play, no network needed |

### Controls
| Action | Input |
|--------|-------|
| Hint | Press HINT button |
| Undo last move | Hold Btn 8 + press HINT |
| New game | Hold OK + press HINT |
| Promotion choice | Buttons 1-4 when pawn reaches back rank |

### Visual Feedback
| Event | LED |
|-------|-----|
| Move | Fading trail animation |
| Hint | Cyan trail + squares |
| Capture | Red flash × 3 |
| Check | Red pulse from king |
| Promotion Q | White flash |
| Promotion R | Red flash |
| Promotion B | Blue flash |
| Promotion N | Yellow flash |
| Undo | Orange right-to-left wipe |
| Checkmate | Rainbow victory sweep |

---

## 🔧 Hardware

| Component | Description |
|-----------|-------------|
| Jetson Orin Nano 8GB | Main compute platform |
| WS2812B LEDs (64 + 22) | Chessboard + control panel |
| Push buttons (×10) | Input system |
| TXS0108E | Logic level shifter |
| SSD1306 OLED (128×64) | Status display |
| USB speaker (optional) | Voice announcements |
| USB drive (optional) | Game save as PGN |

### GPIO Mapping (Jetson 40-pin Header)
| Function | Pin |
|----------|-----|
| Chessboard LED Data | 32 |
| Control Panel LED Data | 33 |
| Buttons 1-9 | 7, 11, 13, 15, 29, 31, 26, 24, 19 |
| HINT button | 16 |

---

## 🖥️ Software Setup

### 1. Install system packages
```bash
sudo apt update
sudo apt install python3-pip stockfish espeak-ng -y
```

### 2. Install Python packages
```bash
pip install -r requirements.txt --break-system-packages
```

### 3. Setup GPIO permissions
```bash
sudo groupadd -f gpio
sudo usermod -aG gpio $USER
sudo cp /opt/nvidia/jetson-gpio/etc/99-gpio.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### 4. LED permissions
```bash
sudo cp scripts/99-ws281x.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

### 5. Configure .env
```bash
cp .env.example .env
# Edit .env and fill in FLASK_SECRET_KEY (required)
# Add LICHESS_TOKEN if using online play
# Add OTA_TOKEN to enable web dashboard updates
```

Generate a secret key:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## ▶️ Running

### Development (no hardware)
```bash
MOCK_LEDS=1 MOCK_BUTTONS=1 MOCK_OLED=1 MOCK_VOICE=1 MOCK_ONLINE=1 \
  python3 -m pytest tests/test_mock_game.py -v
```

### Hardware test
```bash
sudo python3 tests/test_leds_buttons.py
```

### Stockfish test
```bash
python3 tests/test_stockfish.py
```

### Full system
```bash
sudo python3 main.py
```

### Auto-start on boot
```bash
sudo cp scripts/chess.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable chess.service
sudo systemctl start chess.service
```

---

## 🌐 Web Dashboard

Access at `http://<jetson-ip>:5000` from any device on the same network.

Features:
- Live board with move arrows and eval bar
- Enter moves directly from browser (UCI notation, e.g. `e2e4`)
- Request hints and undo
- New game
- LED theme selector + custom colour picker
- Save game to USB / download PGN
- Voice toggle
- Jetson system stats (CPU temp, load, RAM)
- OTA git-pull + restart (requires `OTA_TOKEN` in `.env`)

---

## 🔊 Voice Announcements

Voice is automatically enabled when a USB speaker is detected. No configuration needed — just plug one in. If no speaker is present, the feature is silently disabled.

Install the TTS engine (if not already via apt):
```bash
sudo apt install espeak-ng -y
# OR
pip install pyttsx3 --break-system-packages
```

To permanently disable voice even with a speaker: set `DISABLE_VOICE=1` in `.env`.

---

## 💾 USB Game Save

If a USB drive is plugged in, games are automatically saved as PGN files after each game ends. Files are stored in a `chess_games/` folder on the drive. You can also trigger a manual save via the web dashboard.

No USB drive? No problem — the feature is silently skipped.

---

## 📁 Project Structure

```
jetson_chess/
├── main.py              
├── config.py            
├── voice.py             
├── usb_storage.py       
├── hardware/
│   ├── leds.py
│   └── buttons.py
├── chess_engine/
│   ├── board_state.py
│   └── stockfish.py
├── online/
│   └── lichess.py
├── ui/
│   ├── display.py
│   └── animations.py
├── oled/
│   └── oled_display.py
├── web/
│   └── server.py
├── tests/
│   ├── test_mock_game.py
│   ├── test_leds_buttons.py
│   └── test_stockfish.py
└── scripts/
    ├── chess.service
    └── 99-ws281x.rules
```

---

## ⚖️ License & Acknowledgements

- **Software:** MIT License
- **Hardware (3D models):** CC BY-NC-SA 4.0

Hardware models are a derivative of the original Smart Chess project by DIYMachines (CC BY-NC-SA 4.0). Modifications: Jetson electronics bay, airflow cutouts for Jetson fan, repositioned OLED mount, updated button panel layout.
