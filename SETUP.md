# Jetson Smart Chess Board — Full Setup Guide

Run these commands **in order** on the Jetson Orin Nano (JetPack 6).
Everything assumes you are in the project root: `cd ~/jetson_chess`

---

## 1. System packages

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y \
    python3-pip \
    stockfish \
    espeak-ng \
    ntfs-3g \
    exfatprogs \
    i2c-tools \
    git
```

> **exFAT note:** if `exfatprogs` isn't found try `exfat-fuse` instead.

---

## 2. Python packages

```bash
pip install -r requirements.txt --break-system-packages
```

If `rpi_ws281x` fails (it's Raspberry Pi only — the Jetson uses SPI directly
via `spidev`):

```bash
pip install -r requirements.txt --break-system-packages --ignore-requires-python
# or just skip rpi_ws281x — it's not used at runtime, only spidev is
pip install spidev --break-system-packages
```

---

## 3. Environment file

```bash
cp .env.example .env   # or create it fresh:
nano .env
```

Minimum required:

```
FLASK_SECRET_KEY=<generate one — see below>
LICHESS_TOKEN=        # optional, only needed for online mode
OTA_TOKEN=            # optional, enables web dashboard git-pull updates
DISABLE_VOICE=0       # set to 1 to always silence TTS
```

Generate a secret key:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## 4. GPIO permissions

```bash
sudo groupadd -f gpio
sudo usermod -aG gpio $USER

# Jetson GPIO udev rule (comes with JetPack, just link it)
sudo cp /opt/nvidia/jetson-gpio/etc/99-gpio.rules /etc/udev/rules.d/ 2>/dev/null || true
sudo udevadm control --reload-rules && sudo udevadm trigger
```

---

## 5. LED (SPI) permissions

```bash
sudo cp scripts/99-ws281x.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

The chess LEDs use SPI1 MOSI (BOARD pin 19) via `/dev/spidev0.0`.
Make sure SPI is enabled in the Jetson pin-mux / `jetson-io` tool if it
isn't already.

---

## 6. USB hotplug (plug-and-play USB drives)

```bash
# Mount script
sudo cp scripts/chess-usb-mount.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/chess-usb-mount.sh

# udev rule
sudo cp scripts/99-chess-usb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# Fixed mount point
sudo mkdir -p /mnt/usb
```

After this, plugging in any FAT32/exFAT/NTFS USB drive will auto-mount
it at `/mnt/usb` within about a second. Unplugging safely unmounts it.

Check the mount log any time:

```bash
cat /tmp/chess-usb.log
```

---

## 7. OLED I2C

The SSD1306 OLED uses I2C bus 7 (BOARD pins 3/5) by default.
Confirm it's visible:

```bash
sudo i2cdetect -y 7
# Should show 3c at address 0x3C
```

If it's on a different bus, set in `.env`:

```
OLED_I2C_PORT=1
OLED_I2C_ADDR=0x3C
```

---

## 8. Auto-start on boot (systemd)

```bash
sudo cp scripts/chess.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable chess.service
sudo systemctl start chess.service
```

Check it's running:

```bash
sudo systemctl status chess.service
sudo journalctl -u chess.service -f   # live logs
```

---

## 9. Test everything works

```bash
# Test Stockfish
python3 tests/test_stockfish.py

# Test LEDs + buttons (needs physical hardware)
sudo python3 tests/test_leds_buttons.py

# Full mock run (no hardware needed — keyboard simulates buttons)
MOCK_LEDS=1 MOCK_BUTTONS=1 MOCK_OLED=1 MOCK_VOICE=1 MOCK_ONLINE=1 \
    python3 main.py
```

---

## 10. Web dashboard

Once running, open a browser on any device on the same network:

```
http://<jetson-ip>:5000
```

Find the Jetson's IP:

```bash
hostname -I
```

Or it shows on the OLED at startup.

---

## All-in-one first-time setup (copy/paste)

```bash
# Run from the project root as your normal user
set -e

# 1. System packages
sudo apt update
sudo apt install -y python3-pip stockfish espeak-ng ntfs-3g exfatprogs i2c-tools git

# 2. Python packages
pip install -r requirements.txt --break-system-packages

# 3. .env
if [ ! -f .env ]; then
    echo "FLASK_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > .env
    echo "LICHESS_TOKEN=" >> .env
    echo "OTA_TOKEN=" >> .env
    echo "DISABLE_VOICE=0" >> .env
    echo "Created .env — edit it to add LICHESS_TOKEN if you want online play"
fi

# 4. GPIO
sudo groupadd -f gpio
sudo usermod -aG gpio $USER
sudo cp /opt/nvidia/jetson-gpio/etc/99-gpio.rules /etc/udev/rules.d/ 2>/dev/null || true

# 5. LEDs
sudo cp scripts/99-ws281x.rules /etc/udev/rules.d/

# 6. USB hotplug
sudo cp scripts/chess-usb-mount.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/chess-usb-mount.sh
sudo cp scripts/99-chess-usb.rules /etc/udev/rules.d/
sudo mkdir -p /mnt/usb

# Reload udev
sudo udevadm control --reload-rules && sudo udevadm trigger

# 7. Systemd service
sudo cp scripts/chess.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable chess.service

echo ""
echo "✅ Setup complete. Start with:"
echo "   sudo systemctl start chess.service"
echo "   sudo journalctl -u chess.service -f"
```

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| LEDs not working | `ls /dev/spidev*` — SPI must be enabled in jetson-io |
| OLED blank | `sudo i2cdetect -y 7` — confirm address 0x3C |
| Buttons not responding | `groups` — must include `gpio`; log out and back in |
| USB not detected | `cat /tmp/chess-usb.log` — check mount script output |
| Web dashboard won't load | `sudo journalctl -u chess.service -f` — check for port 5000 errors |
| Lichess not connecting | Check `LICHESS_TOKEN` in `.env` is a valid board token |
| Voice silent | `aplay -l` — confirm USB speaker shows as a card |