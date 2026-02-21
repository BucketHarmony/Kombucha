# Deploying to Kombucha (Rover)

## Rover Connection Details

| Field | Value |
|-------|-------|
| **Hostname** | `kombucha` |
| **mDNS** | `kombucha.local` |
| **User** | `bucket` |
| **Auth** | SSH key (no password) |
| **OS** | Debian 13 (trixie), aarch64 |
| **Python** | 3.13.5 |
| **Wi-Fi** | `wlan0`, DHCP (IP changes on reboot) |

> **The router reassigns IP addresses on reboot.** Do NOT rely on a hardcoded IP.
> Always use `kombucha.local` (mDNS/Avahi) to reach the rover.

## Finding the Rover

The rover advertises itself via Avahi/mDNS as `kombucha.local`. Use this
instead of a static IP:

```bash
# Ping to verify it's online and discover current IP
ping kombucha.local

# SSH in
ssh bucket@kombucha.local
```

If `kombucha.local` does not resolve:
1. **Check the rover is powered on** and connected to Wi-Fi.
2. **Check your machine supports mDNS** -- Windows needs Bonjour or the
   "Link-Local Multicast Name Resolution" feature (usually built in to
   Windows 10+). macOS and Linux work out of the box.
3. **Fall back to router admin page** -- log into your router at 192.168.4.1
   and look for a device named `kombucha` in the DHCP leases to find its
   current IP.

## First-Time SSH Setup

If you have never connected from this machine before, or the rover's IP
has changed since your last connection, SSH will warn about an unknown or
changed host key.

### Accept a new host key (first connection to new IP)

```bash
ssh -o StrictHostKeyChecking=accept-new bucket@kombucha.local
```

This tells SSH to accept and permanently save the key for `kombucha.local`.

### Clear a stale host key (IP changed, key mismatch)

If you get `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED`:

```bash
# Remove old entry for the stale IP (example: 192.168.4.226)
ssh-keygen -R 192.168.4.226

# Remove old entry for the hostname
ssh-keygen -R kombucha.local

# Reconnect (accept the new key)
ssh -o StrictHostKeyChecking=accept-new bucket@kombucha.local
```

## Directory Layout on the Rover

```
/home/bucket/
  kombucha/                  # Bridge code and runtime data
    kombucha_bridge.py       # Main agentic tick loop
    story_server.py          # Web dashboard / story viewer
    frames/                  # Captured JPEG frames
    data/                    # Created at runtime
      memory.db              # SQLite memory database
      state.json             # Persisted tick state
      journal/               # Append-only JSONL journals
  ugv_rpi/                   # Waveshare stock control app
    app.py                   # Flask web UI (port 5000)
    ugv-env/                 # Python venv for Waveshare code
  ugv.log                    # Waveshare app log
```

## Hardware Status (confirmed 2026-02-21)

| Subsystem | Status | Notes |
|-----------|--------|-------|
| **Drive motors** | Working | `/dev/ttyAMA0`, speeds -1.3..1.3 m/s |
| **OLED display** | Working | 4 lines, 20 chars each |
| **Gimbal (pan/tilt)** | Working | Pan -180..180, tilt -30..90 |
| **LED lights** | Working | Base + head, 0-255 PWM |
| **Camera** | Working | 640x480 MJPEG, GStreamer backend |
| **Speech (gTTS)** | Working | USB PnP audio, ffplay output |
| **ESP32 feedback** | Working | T:1001 stream: IMU, odometry, voltage |
| **Memory/journal** | Working | SQLite WAL + JSONL backup |
| **Story server** | Working | Local port 8080, SSE live updates |
| **Battery** | ~11.77V | 3S LiPo, ~75% charge at last check |

> **Critical**: The ESP32 is on GPIO UART `/dev/ttyAMA0`, NOT USB serial
> `/dev/ttyACM0`. The CH340 chip at `/dev/ttyACM0` does not reach the motor
> controller. This was the root cause of "commands succeed but nothing moves."

## Deploying Code

From the Windows dev machine (E:\AI\rover):

```bash
# Deploy the bridge
scp E:\AI\rover\kombucha_bridge.py bucket@kombucha.local:~/kombucha/

# Deploy the story server
scp E:\AI\rover\story_server.py bucket@kombucha.local:~/kombucha/

# Verify
ssh bucket@kombucha.local "ls -la ~/kombucha/*.py"
```

## Running the Bridge

The Waveshare `app.py` `@reboot` cron has been **removed** to prevent it
from competing with the bridge for the serial port. If app.py is running
for any reason, kill it first.

```bash
# SSH into the rover
ssh bucket@kombucha.local

# Kill the Waveshare app if running
pkill -f app.py

# Run the bridge
cd ~/kombucha
python3 kombucha_bridge.py

# Or in debug mode (no serial/hardware, camera + LLM still live)
python3 kombucha_bridge.py --debug
```

### Running in the Background

```bash
# Start detached with nohup
cd ~/kombucha
nohup python3 kombucha_bridge.py >> ~/kombucha.log 2>&1 &

# Follow the log
tail -f ~/kombucha.log
```

## Running the Story Server

The story server runs on your **local dev machine** (not the rover). It
syncs data from the rover via rsync/scp in the background.

```bash
# From the dev machine
python story_server.py

# With custom port
python story_server.py --port 9090

# Without Pi sync (local data only)
python story_server.py --no-sync
```

Open `http://localhost:8080` in a browser to view the dashboard.

## Restarting the Waveshare App

After you're done with the bridge, restart the stock control app:

```bash
ssh bucket@kombucha.local "pkill -f app.py; sleep 2; cd ~/ugv_rpi && XDG_RUNTIME_DIR=/run/user/1000 nohup ugv-env/bin/python app.py >> ~/ugv.log 2>&1 &"
```

- **Web UI**: http://kombucha.local:5000
- **JupyterLab**: http://kombucha.local:8888

## Troubleshooting

### Rover not responding to ping/SSH

- Check power -- the Pi 5 draws significant current; ensure the battery
  or power supply is adequate.
- Check Wi-Fi -- the rover may have dropped off the network. Physical
  access may be needed to check `wpa_supplicant` status.
- The `@reboot` cron job starts `app.py` on boot. If the Pi just rebooted,
  give it 30-60 seconds to fully start.

### "Host key verification failed"

The rover's DHCP IP changed. See [Clear a stale host key](#clear-a-stale-host-key-ip-changed-key-mismatch) above.

### Serial port busy

If `kombucha_bridge.py` reports the serial port is in use:

```bash
# Check what's using it
ssh bucket@kombucha.local "fuser /dev/ttyAMA0"

# Kill the offending process (usually app.py)
ssh bucket@kombucha.local "pkill -f app.py"
```

### Camera not found

The USB camera can drop off the bus after force-killing processes.
Check with:

```bash
ssh bucket@kombucha.local "v4l2-ctl --list-devices"
```

If no camera is listed, physically reseat the USB cable.

### Nothing moves / no OLED / no lights

The ESP32 motor controller is on **`/dev/ttyAMA0`** (GPIO UART), **not**
`/dev/ttyACM0` (USB-serial CH340). If the bridge sends commands with "ok"
results but nothing physically happens, check which serial port is configured:

```bash
ssh bucket@kombucha.local "grep SERIAL_PORT ~/kombucha/kombucha_bridge.py"
# Should show: /dev/ttyAMA0
```

To verify the ESP32 is reachable:

```bash
ssh bucket@kombucha.local 'python3 -c "
import serial, json
s = serial.Serial(\"/dev/ttyAMA0\", 115200, timeout=1)
import time; time.sleep(0.5)
if s.in_waiting:
    print(s.readline().decode(errors=\"replace\"))
s.close()
"'
# Should print T:1001 feedback JSON with voltage, IMU, odometry
```

### Checking battery voltage

```bash
ssh bucket@kombucha.local 'python3 -c "
import serial, json, time
s = serial.Serial(\"/dev/ttyAMA0\", 115200, timeout=1)
time.sleep(0.5)
if s.in_waiting:
    d = json.loads(s.readline())
    print(f\"Battery: {d.get(\"v\", 0)/100:.2f}V\")
s.close()
"'
```

3S LiPo reference: Full ~12.6V, Nominal ~11.1V, Low ~10.5V.
