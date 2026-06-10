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

This project is a **fully self-contained smart chess board** powered by the **NVIDIA Jetson Orin Nano**.

It integrates:

* 🧠 Local AI using Stockfish
* 🎮 Physical button-based input
* 🌈 LED move visualization
* 🌐 Optional Lichess online play



## ✨ Features

* ♟️ Full chess gameplay with physical controls
* ⚡ Fast local AI (Stockfish on ARM Cortex-A78AE)
* 🎯 LED-based move highlighting and animations
* 💡 Hint system (visual suggestions)
* 🔌 Powered directly from Jetson (no external PSU)
* 🧩 Clean single-board architecture



## 🧠 Architecture

* Jetson GPIO directly controls:

  * WS2812B LED strips
  * Button inputs

* Python handles:

  * Game logic (`python-chess`)
  * Engine integration (Stockfish)
  * LED rendering and animations



## 🔧 Hardware

### Components

| Component              | Description                          |
| ---------------------- | ------------------------------------ |
| Jetson Orin Nano 8GB   | Main compute platform                |
| WS2812B LEDs (64 + 22) | Chessboard + control panel           |
| Push buttons (×10)     | Input system                         |
| **TXS0108E**           | Logic inverter / signal conditioning |



### ⚡ Power Design

This build intentionally avoids external power:

* ✅ Powered directly from Jetson
* ✅ Simplified wiring
* ⚠️ Reduced LED brightness for stability

> This design prioritizes simplicity and integration over maximum brightness.



### 🔌 GPIO Mapping (Jetson 40-pin Header)

| Function               | Pin                                   |
| ---------------------- | ------------------------------------- |
| Chessboard LED Data    | 32                                    |
| Control Panel LED Data | 33                                    |
| Buttons                | 7, 11, 13, 15, 29, 31, 26, 24, 19, 16 |



## 🖥️ Software Setup

### 1. Install dependencies

```bash
sudo apt update
sudo apt install python3-pip stockfish -y
```

### 2. Install Python packages

```bash
pip install -r requirements.txt --break-system-packages
```

### 3. Setup GPIO permissions

```bash
pip install Jetson.GPIO --break-system-packages

sudo groupadd -f gpio
sudo usermod -aG gpio $USER
sudo cp /opt/nvidia/jetson-gpio/etc/99-gpio.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Log out and back in after this step.


### 4. LED permissions

```bash
sudo cp scripts/99-ws281x.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```


### 5. Optional: Lichess integration

1. Generate token: https://lichess.org/account/oauth/token
2. Enable:

   * `challenge:write`
   * `board:play`

```bash
export LICHESS_TOKEN="your_token_here"
```


## ▶️ Running

### Development (no hardware)

```bash
MOCK_LEDS=1 MOCK_BUTTONS=1 MOCK_ONLINE=1 python3 tests/test_mock_game.py
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


## 🎮 Gameplay

### Input Method

1. Select **column (A–H)**
2. Select **row (1–8)**
3. Repeat for destination
4. Press **OK** to confirm


### Controls

| Action   | Input                |
| -------- | -------------------- |
| Hint     | Press HINT           |
| New Game | Hold HINT + OK       |
| Shutdown | Hold HINT + Button 8 |


### Visual Feedback

| Event     | LED Behavior   |
| --------- | -------------- |
| Move      | Highlight path |
| Capture   | Red flash      |
| Hint      | Cyan move      |
| Error     | Blue + red X   |
| Checkmate | Animation      |


## 🆚 Original vs This Build

| Feature       | Original     | This Project     |
| ------------- | ------------ | ---------------- |
| Architecture  | Pi + Arduino | Jetson only      |
| Communication | USB serial   | Direct GPIO      |
| Power         | External PSU | No external PSU  |
| Logic IC      | 74AHCT125    | **TXS0108E**     |
| Performance   | Moderate     | High             |


## 📁 Project Structure

```
jetson_chess/
├── main.py
├── hardware/
├── chess_engine/
├── online/
├── ui/
├── tests/
└── scripts/
```


## 📜 License

MIT open-source project based on the original Smart Chess Board concept.

