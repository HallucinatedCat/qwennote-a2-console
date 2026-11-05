# a2ble

An unofficial, Windows-friendly Bluetooth LE client for **your own** QwenNote
A2 recorder: scan, connect, read status/battery/firmware, list files, and
start/stop/pause/resume recording.

**Status: experimental and paused.** Nothing here has been tested against real
Bluetooth hardware, and two pieces (the wire frame layout and the AES recipe)
are best-effort reconstructions that still need to be checked against a real
capture. See "Known gaps".

> **Notice.** This is an unofficial, independent project. It is not affiliated
> with or endorsed by the vendor. Use it only with devices you own. It may
> conflict with the vendor's terms of service. It is provided as is, with no
> warranty of any kind; you use it at your own risk.

## Safety notes

- The client only ever sends the commands in `a2ble/commands.py`: GET_RANDOM,
  CONNECT_DEVICE, DISCONNECT_DEVICE, SYNC_DEV_STATUS, QUERY_FW_VERSION,
  SYS_CONTROL (battery read only), GET_FILE_LIST, AUDIO_RECORD_OPT
  (start/stop/pause/resume/status). An optional AUTH step exists but is off by
  default.
- It deliberately does **not** implement RESET_DEVICE, any OTA/firmware
  command, SYS_CONTROL reboot/format/clear-OTA, GRAY_SWITCH, ACTIVE_DEVICE,
  FILE_DELETE, or VENDOR_SET. No code path in this package can produce those
  commands.
- Never share your `device_secret`. It is a credential. This tool never logs
  it, never prints it (`Pairing.__repr__`/`__str__` redact it), and only uses
  it as AES key material in a local calculation.

## Requirements

- Python 3.11 or newer
- A Bluetooth LE adapter (on Windows 10/11 no separate driver is normally
  needed; [bleak](https://github.com/hbldh/bleak) uses the WinRT APIs)
- `bleak` and `pycryptodome` (see `requirements.txt`)
- Your own device's pairing file (next section)

## Pairing file

The client needs a small JSON file with your own device's serial number and
device secret. This tool never fetches it from any cloud service. Export it
from the web console of this project, which reads the same bound-device record
your account already has:

1. Open the console's **Dev tools**.
2. Turn on the **dangerous actions** switch.
3. Choose **Download Bluetooth pairing file** and type `pair` to confirm.

That file already has the right keys. You can also write it by hand:

```json
{
  "sn": "YOUR-DEVICE-SERIAL",
  "device_secret": "YOUR-32-HEX-CHAR-DEVICE-SECRET",
  "did": "any32hexcharsworkhereoromitthis",
  "corp_id": "",
  "name": "My A2"
}
```

- `sn`: the recorder's serial number. Used only for BLE discovery and display.
- `device_secret`: the per-device secret, a 32-hex-character string (16
  bytes). Used as AES key material for the handshake.
- `did` (optional): a per-installation client id (any stable 32-hex-char
  string). It is generated for you if omitted and can be persisted with
  `a2ble.pairing.save_generated_did`. It is not the recorder's identity.
- `corp_id` (optional): the account/corp id; leave blank if your account does
  not use multi-corp binding.

Keep this file private and treat it like a password file. Never commit it or
share it.

Default location:

- Windows: `%LOCALAPPDATA%\a2ble\pairing.json`
- Linux/macOS: `~/.a2ble/pairing.json`

Override with `--pairing <path>` on any command.

## Usage (Windows)

1. Install Python 3.11+ from [python.org](https://www.python.org/downloads/)
   (check "Add python.exe to PATH").
2. Open PowerShell in this folder (`tools\a2-ble`) and create a virtual
   environment:
   ```powershell
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Turn on Bluetooth on your PC and place your pairing file (see above).
4. Scan for your device, then use the address `scan` printed:
   ```powershell
   python -m a2ble scan
   python -m a2ble connect --address AA:BB:CC:DD:EE:FF
   python -m a2ble battery --address AA:BB:CC:DD:EE:FF
   python -m a2ble firmware --address AA:BB:CC:DD:EE:FF
   python -m a2ble files --address AA:BB:CC:DD:EE:FF
   python -m a2ble record start --address AA:BB:CC:DD:EE:FF
   python -m a2ble record stop --address AA:BB:CC:DD:EE:FF
   ```

A normal reconnect sends GET_RANDOM then CONNECT_DEVICE. Pass
`--send-auth-step` only if you specifically want to exercise the AUTH command
shape; the official app uses AUTH only when unbinding a device.

## Checking the unverified parts with one capture

The frame layout and the AES recipe can be checked against real traffic:

1. On your phone, turn on Developer options -> **Enable Bluetooth HCI snoop
   log**, then turn Bluetooth off and on.
2. Open the official app and let it connect to your A2 once.
3. Get `btsnoop_hci.log` (on most phones: `adb bugreport`, then
   `FS/data/misc/bluetooth/logs/btsnoop_hci.log` inside the zip).
4. Run `python -m a2ble analyze btsnoop_hci.log --pairing <your pairing file>`.

It lists every A2 frame with its first header bytes and length, says whether
`framing.py` decodes it, and whether `aes.py` reproduces the token the
official app sent in CONNECT_DEVICE ("match" or "no match"). Serials and tokens
are not printed unless you add `--show-values`.

## Running the tests

No Bluetooth adapter or device is needed:

```sh
python3 -m unittest discover -s tools/a2-ble
```

All tests use synthetic data and an in-memory fake transport, never a real
credential.

## Package layout

```
a2ble/
  pairing.py        pairing.json loading/validation (never logs the secret)
  aes.py            AES-128-CBC-NoPadding, key=iv=hex-decoded device_secret
  framing.py        magic byte + header + JSON body + CRC16 (placeholder layout)
  commands.py       command ids + JSON body builders
  protocol.py       DeviceSession: msg_id correlation, handshake, high-level ops
  ble_transport.py  bleak wrapper: scan FE3C / connect / notify FE1B / write FE1C
  cli.py, __main__.py  the `python -m a2ble ...` command line
test_*.py           unit tests, synthetic data only
```

## Known gaps

- The byte-level frame layout and the exact AES recipe are unverified against
  a real device. `a2ble/framing.py` and `a2ble/aes.py` each have one clearly
  marked constant/recipe to adjust if a capture disagrees.
- Whether GET_RANDOM/CONNECT_DEVICE need a non-empty `corp_id` against real
  firmware is unconfirmed; the field is sent empty by default.
- `a2ble.protocol.DEFAULT_SDK_VER` is a placeholder string; if the device
  validates it, adjust it using a capture.
- File download (audio content) is out of scope; only `GET_FILE_LIST` is
  implemented.
- Never tested against real Bluetooth hardware; everything above the BLE
  transport is unit tested with an in-memory fake device.
