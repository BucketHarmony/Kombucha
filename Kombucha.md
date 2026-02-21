# Kombucha — Platform Spec

**An experiment in Agentic AI Persistence and Presence. AI Embodiment.**

Kombucha is a Waveshare UGV Rover PI ROS2 — a 4-wheel all-terrain rover
serving as the physical body for a persistent Claude Opus agent. The agent
sees through Kombucha's camera, thinks in the cloud, and acts through
Kombucha's motors and servos. The goal is to explore what happens when an
LLM has a continuous, embodied presence in physical space.

---

## 1. Hardware

### 1.1 Chassis
| Spec | Value |
|------|-------|
| Frame | 2mm all-aluminum alloy shell |
| Dimensions | 230 x 252 x 255 mm (L x W x H) |
| Weight | 2,251 g (bare platform) |
| Ground Clearance | 25 mm |
| Payload Capacity | 4 kg |
| Operating Temp | 0 - 40 C |

### 1.2 Drive System
| Spec | Value |
|------|-------|
| Configuration | 4-wheel, 4WD differential steer |
| Motors | 4x geared DC with encoders |
| Motor Voltage | 12V rated |
| Motor Current | <= 1A rated |
| Motor Speed | 250 RPM rated / 333 RPM no-load |
| Max Speed | 1.3 m/s |
| Turning Radius | 0 m (in-place rotation) |

### 1.3 Wheels
| Spec | Value |
|------|-------|
| Diameter | 80 mm |
| Width | 42 mm |
| Type | Soft anti-skid rubber |
| Count | 4 (all driven) |

### 1.4 Power
| Spec | Value |
|------|-------|
| Battery | 3x 18650 lithium cells, 3400 mAh 4C |
| Voltage Range | 9 - 12.6V |
| Runtime | ~1h 30m active / ~5h standby |

### 1.5 Pan-Tilt Head
| Spec | Value |
|------|-------|
| DOF | 2 (pan + tilt) |
| Pan Range | -180 to +180 degrees |
| Tilt Range | -30 to +90 degrees |
| Servos | 2x bus servos (configurable ID) |
| Lighting | High-brightness LED spotlight (PWM) |

---

## 2. Sensors & Peripherals

### 2.1 Camera
| Spec | Value |
|------|-------|
| Model | Realtek 5842 USB UVC camera |
| FOV | 160 degrees (ultra-wide) |
| Max Resolution | 1920x1080 @ 30fps (MJPEG) |
| Working Resolution | 640x480 @ 30fps |
| Format | Motion-JPEG |
| Device | /dev/video0 |
| Driver | uvcvideo |
| Audio | Built-in USB microphone (capture) |

### 2.2 Audio
| Device | Type | Card |
|--------|------|------|
| USB Camera mic | Capture | card 2, device 0 |
| USB PnP Audio | Capture + Playback | card 3, device 0 |
| HDMI 0 | Playback | card 0, device 0 |
| HDMI 1 | Playback | card 1, device 0 |

### 2.3 OLED Display
- 4-line text display on chassis
- Controlled via ESP32 (`{"T":3, "lineNum":N, "Text":"..."}`)

### 2.4 IMU
- On-board ESP32 IMU (accelerometer/gyro)
- Command `{"T":126}` — currently unresponsive (needs investigation)

### 2.5 Buses Available
| Bus | Device |
|-----|--------|
| I2C | /dev/i2c-13, /dev/i2c-14 |
| SPI | /dev/spidev10.0 |
| GPIO | /dev/gpiochip0, gpiochip4, gpiochip10-13 |
| UART/USB | /dev/ttyACM0 (ESP32 @ 115200 baud) |

---

## 3. Compute

### 3.1 Single Board Computer
| Spec | Value |
|------|-------|
| Board | Raspberry Pi 5 |
| CPU | Cortex-A76 quad-core @ 2.4 GHz (ARMv8.2) |
| RAM | 4 GB LPDDR4X |
| Storage | 64 GB microSD (57 GB usable, 46 GB free) |
| OS | Debian 13 (Trixie) |
| Kernel | 6.12.62+rpt-rpi-2712 (PREEMPT) |
| Python | 3.13.5 |
| Hostname | kombucha |

### 3.2 Motor Controller
| Spec | Value |
|------|-------|
| MCU | ESP32 |
| USB Chip | QinHeng CH340 (USB single serial) |
| Connection | /dev/ttyACM0 @ 115200 baud |
| Protocol | JSON over serial (newline-delimited) |
| Manages | Motors, servos, OLED, LEDs, IMU |

---

## 4. Network

| Spec | Value |
|------|-------|
| Interface | WiFi (wlan0) |
| MAC | 2c:cf:67:3b:0d:d0 |
| IP | 192.168.4.226/22 |
| SSH | Port 22, user `bucket`, key-based auth |
| Web UI | http://192.168.4.226:5000 (Flask + SocketIO) |
| JupyterLab | http://192.168.4.226:8888 (no auth token) |

---

## 5. ESP32 Serial Command Protocol

All commands are JSON objects sent over serial with a newline terminator.
The `"T"` field is the command type discriminator.

### 5.1 Motion
| T | Command | Parameters | Description |
|---|---------|------------|-------------|
| 0 | Emergency Stop | — | Halt all motors and servos |
| 1 | Speed Control | `L`: left speed, `R`: right speed | Differential drive. Values are floats (m/s). Max ~1.3 |

### 5.2 Pan-Tilt Gimbal
| T | Command | Parameters | Description |
|---|---------|------------|-------------|
| 133 | Gimbal Control | `X`: pan, `Y`: tilt, `SPD`: speed, `ACC`: acceleration | Absolute position. X: -180..180, Y: -30..90 |
| 141 | Gimbal Base Ctrl | `X`, `Y`, `SPD` | Simplified gimbal control |
| 137 | Gimbal Steady | (config-defined) | Stabilization mode |

### 5.3 Display & Lights
| T | Command | Parameters | Description |
|---|---------|------------|-------------|
| 3 | OLED Write | `lineNum`: 0-3, `Text`: string | Write text to OLED line |
| -3 | OLED Default | — | Reset OLED to default display |
| 132 | LED Control | `IO4`: 0-255, `IO5`: 0-255 | PWM control for base + head LEDs |

### 5.4 Servo Management
| T | Command | Parameters | Description |
|---|---------|------------|-------------|
| 501 | Set Servo ID | `raw`: old ID, `new`: new ID | Reassign bus servo ID |
| 502 | Set Servo Mid | `id`: servo ID | Set current position as midpoint |
| 210 | Servo Torque | `id`: servo ID, `cmd`: 0/1 | Lock/unlock servo torque |

### 5.5 Sensor Readback
| T | Command | Notes |
|---|---------|-------|
| 126 | IMU Data | Accelerometer + gyro (not responding currently) |
| 130 | Battery Voltage | (not responding currently) |

### 5.6 Feedback (Rover -> Pi)
The ESP32 sends JSON feedback with these `T` codes:

| T | Meaning |
|---|---------|
| 1003 | Status/telemetry packet |

Feedback fields defined in config: battery voltage (112), CPU temp (107),
CPU load (106), pan angle (109), tilt angle (110), WiFi RSSI (111).

---

## 6. Software Stack

### 6.1 Installed
| Component | Path | Description |
|-----------|------|-------------|
| ugv_rpi | ~/ugv_rpi/ | Waveshare control app (Flask web UI) |
| Python venv | ~/ugv_rpi/ugv-env/ | Python 3.13 virtual environment |
| app.py | ~/ugv_rpi/app.py | Main application (Flask + SocketIO + WebRTC) |
| base_ctrl.py | ~/ugv_rpi/base_ctrl.py | ESP32 serial command layer |
| cv_ctrl.py | ~/ugv_rpi/cv_ctrl.py | Computer vision (OpenCV, mediapipe optional) |
| audio_ctrl.py | ~/ugv_rpi/audio_ctrl.py | Audio capture/playback/TTS |
| os_info.py | ~/ugv_rpi/os_info.py | System telemetry |
| config.yaml | ~/ugv_rpi/config.yaml | All configuration + command codes |

### 6.2 Auto-Start (cron @reboot)
```
@reboot XDG_RUNTIME_DIR=/run/user/1000 ~/ugv_rpi/ugv-env/bin/python ~/ugv_rpi/app.py >> ~/ugv.log 2>&1
@reboot /bin/bash ~/ugv_rpi/start_jupyter.sh >> ~/jupyter_log.log 2>&1
```

### 6.3 Key Python Packages
| Package | Version | Purpose |
|---------|---------|---------|
| Flask | 3.1.1 | Web framework |
| Flask-SocketIO | 5.6.0 | Real-time WebSocket communication |
| opencv (cv2) | 4.10.0 | Computer vision |
| picamera2 | 0.3.34 | Pi camera interface (unused — USB cam) |
| pyserial | 3.5 | ESP32 serial communication |
| aiortc | 1.14.0 | WebRTC for video streaming |
| numpy | 2.2.4 | Numerical computation |
| simplejpeg | 1.8.1 | Fast JPEG encode/decode |
| PyAudio | 0.2.14 | Audio capture/playback |
| gTTS | 2.5.4 | Google text-to-speech |
| pyttsx3 | 2.99 | Offline TTS (currently broken — espeak driver) |
| jupyterlab | 4.5.4 | Interactive notebooks |
| matplotlib | 3.10.8 | Plotting/visualization |
| pillow | 11.1.0 | Image processing |
| imageio | 2.37.2 | Image/video I/O |
| av | 14.2.0 | FFmpeg bindings |

### 6.4 NOT Installed
- **ROS2** — ugv_ws workspace not deployed
- **mediapipe** — no Python 3.13/aarch64 wheel exists
- **LiDAR driver** — no LiDAR hardware connected (use_lidar: false)

---

## 7. Modifications from Stock

Changes made during setup on 2025-02-21:

1. **app.py** — Serial port changed from `/dev/ttyAMA0` to `/dev/ttyACM0` (USB serial, not GPIO UART)
2. **cv_ctrl.py** — mediapipe imports made optional (`HAS_MEDIAPIPE` flag, `if mp:` guards on all usage)
3. **audio_ctrl.py** — pyttsx3 init wrapped in try/except (espeak driver fails on this system)
4. **requirements_core.txt** — Custom requirements file for Python 3.13 compatibility (excluded mediapipe, relaxed av version)
5. **venv ownership** — Changed from root to `bucket:bucket`
6. **SSH key** — Passwordless SSH configured from workstation

---

## 8. Web Control Interface (Port 5000)

The Flask app provides a full browser-based control UI with:

- **Live video** — WebRTC or MJPEG stream from USB camera
- **Drive controls** — Joystick-style speed control (left/right differential)
- **Pan-tilt controls** — Gimbal position via on-screen controls
- **CV modes** — Motion detection, face detection, object detection, color tracking, auto-follow
- **Detection reactions** — None, capture, record
- **LED control** — Off, auto, always-on
- **Photo/video capture** — Snapshot and recording
- **Zoom** — 1x, 2x, 4x digital zoom
- **Audio** — Playback and TTS
- **System info** — CPU temp, RAM usage, battery voltage, WiFi RSSI

---

## 9. Agentic AI Architecture (Planned)

The intent is to give Claude Opus a persistent, embodied presence through
Kombucha. Inspired by the Bitters prototype (Petoi Bittle X + Claude), but
with significantly more capable hardware.

### 9.1 Prior Art: Bitters Bridge
The existing `bitters_bridge.py` implements a tight `SEE -> THINK -> ACT`
loop on a Raspberry Pi Zero 2 W controlling a Petoi Bittle X robot dog:

- **SEE**: Capture JPEG frame from Pi Camera
- **THINK**: POST frame + state to Claude API, receive JSON with observation/goal/reasoning/actions
- **ACT**: Execute serial motor commands, save state, wait for LLM-specified tick interval
- **PERSIST**: state.json holds goal, observation, tick count across restarts
- **NARRATE**: Each tick produces a thought/observation that feeds a story server

### 9.2 Kombucha Advantages over Bitters
| Dimension | Bitters (Bittle) | Kombucha (UGV Rover) |
|-----------|-----------------|---------------------|
| Compute | Pi Zero 2 W, 416 MB RAM | Pi 5, 4 GB RAM |
| Camera | OV5647 (Pi Camera) | 1080p USB wide-angle |
| Locomotion | Quadruped (12-DOF) | 4-wheel 4WD + pan-tilt head |
| Terrain | Flat surfaces only | All-terrain, 4kg payload |
| Battery | ~30 min | ~90 min active |
| Audio | None | Mic input + speaker output |
| Display | None | OLED 4-line text |
| Serial Protocol | ASCII skill codes | Structured JSON commands |
| Speed | Walking pace | Up to 1.3 m/s |
| Range | Indoor tabletop | Indoor/outdoor floor-level |

### 9.3 Open Questions
- What does AI persistence look like over hours/days vs. minutes?
- How does embodiment change when the agent can speak and hear?
- What goals emerge when the agent has real terrain to navigate?
- How should the agent handle battery awareness and self-preservation?
- Can the agent develop a spatial model of its environment over time?
- What is the right tick rate for a wheeled platform vs. a walker?
- How should the story/narrative layer adapt to longer-lived sessions?

---

## 10. Access Quick Reference

```bash
# SSH
ssh bucket@192.168.4.226

# Web UI
open http://192.168.4.226:5000

# JupyterLab
open http://192.168.4.226:8888

# Logs
ssh bucket@192.168.4.226 "tail -f ~/ugv.log"

# Restart app
ssh bucket@192.168.4.226 "pkill -f app.py; sleep 2; cd ~/ugv_rpi && ugv-env/bin/python app.py >> ~/ugv.log 2>&1 &"

# Send raw ESP32 command (when app.py is NOT running)
# python3 -c "import serial,json; s=serial.Serial('/dev/ttyACM0',115200); s.write(json.dumps({'T':1,'L':0.5,'R':0.5}).encode()+b'\n')"
```
