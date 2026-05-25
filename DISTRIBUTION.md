# BiHome Wearable — Distribution guide

This document is for **end users** who received a copy of `BiHome Wearable.exe`
(or the zipped `BiHome Wearable/` folder). If you are a developer reading this,
see `README.md` for setup from source.

---

## What you received

A folder named **`BiHome Wearable/`** containing:

```
BiHome Wearable/
├── BiHome Wearable.exe        ← the application — double-click to launch
└── _internal/                 ← all the bundled libraries — DO NOT modify
    ├── Viewer/
    ├── LabRecorder/
    └── ...
```

**Important**: keep the `_internal/` folder next to the `.exe`.
The app reads bundled assets from inside it. If you move or rename it,
the app won't start.

---

## System requirements

- **Windows 10 / 11** (64-bit)
- **Bluetooth 4.0 or later** (built-in or USB adapter) — required for Polar H10
- **WiFi** — required for EmotiBit (PC and EmotiBit must be on the same network)
- **~500 MB free disk space**
- **Microsoft Visual C++ Redistributable 2019 or later** *(probably already installed)*

### Visual C++ Redistributable

If the app fails to launch with an error like *"VCRUNTIME140.dll not found"* or
*"the application failed to start because its side-by-side configuration is
incorrect"*, you need to install:

> [Microsoft VC++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe)

Download, run, restart. Most Windows installations already have this from
other apps, so try launching BiHome first.

---

## First launch

1. **Double-click `BiHome Wearable.exe`**.
2. Windows will probably show a SmartScreen warning:
   > *"Windows protected your PC — Microsoft Defender SmartScreen prevented an
   > unrecognized app from starting."*

   This is normal for unsigned apps. To proceed:
   - Click **"More info"**
   - Click **"Run anyway"**

   You only need to do this once per machine.

3. The **setup wizard** opens:
   - **Step 1**: how many participants? (1 to 6)
   - **Step 2**: scanning... the app looks for known devices on Bluetooth and WiFi
   - **Step 3**: assign devices to participants

   On the **very first launch** no devices are registered yet. Click
   **`+ Add device…`** to register each Polar H10 (by Bluetooth MAC) and
   EmotiBit (by serial number). They will be remembered for future launches.

4. Once devices are connected, the **viewer** opens automatically: one window
   per participant with the live signals.

---

## Finding your device addresses

You need to know the **MAC address** of each Polar H10 and the
**serial number** of each EmotiBit before you can register them in the
"+ Add device…" dialog.

### Polar H10 — Bluetooth MAC address

The MAC is a 6-pair hex string like `24:AC:AC:04:96:A3`. Two easy ways
to find it:

**Option A — use the in-app BLE scanner (recommended):**
1. Make sure your Polar H10 is **powered on** (LED blinking blue).
2. In the wizard, click **`+ Add device…`** → select **"Polar H10"**.
3. Click **"🔍 Scan nearby BLE"** in that dialog.
4. After ~6 s a list of nearby BLE devices appears.
5. Find the line starting with **"Polar"** and click it — the MAC fills
   in automatically.
6. Type a friendly name (e.g. *"Polar 1"*) and click Save.

**Option B — via Windows Settings:**
1. Open Windows **Settings → Bluetooth & devices → Devices**.
2. If the Polar is not already paired, click **"Add device"** → Bluetooth,
   wait for "Polar H10 XXXXXX" to appear, click it to pair.
3. Click on the paired Polar → **"Device details"** (or right-click →
   **Properties**).
4. Look for **"Bluetooth address"** — it's the 6-pair string.
5. Copy it and paste into the Add Device dialog.

> **Tip:** The 7-character ID printed on the inside of the Polar strap
> (e.g. *0496A33F*) is **NOT** the MAC. The MAC is longer and uses
> colons.

### EmotiBit — serial number

The serial looks like **`MD-V6-0000482`** (prefix `MD-V` followed by
version, dash, and 7 digits).

1. Look at the EmotiBit's PCB or the back of its enclosure — the serial
   is printed there.
2. Type it exactly into the **"+ Add device…"** dialog after selecting
   "EmotiBit".
3. The app validates the prefix to avoid typos; if it complains, double-
   check you typed the dashes correctly.

> **Note:** The EmotiBit must be connected to the **same WiFi network**
> as the PC before BiHome can talk to it. Configure WiFi using the
> EmotiBit Oscilloscope app or the EmotiBit web interface — see the
> [EmotiBit documentation](https://www.emotibit.com/) for setup.

---

## Where your data lives

Per-user state and recordings live in **`%APPDATA%\BiHome\`**, which is:

```
C:\Users\<your-username>\AppData\Roaming\BiHome\
```

Contents:
- `devices.json` — your registered Polar/EmotiBit devices
- `wizard_defaults.json` — last session's wizard choices
- `viewer_settings.json` — window positions, channel layout, etc.
- `acquisition.log` — diagnostic log (overwritten each session)
- `recordings/` — XDF + CSV recordings from the viewer's REC button

This folder is **preserved across app updates** — when a new
`BiHome Wearable.exe` is released and you replace the old one, your
devices and recordings stay.

You can browse there via Windows Explorer to find recordings or to copy
`devices.json` to another machine.

---

## Recording sessions

1. In the viewer's left panel, tick the **REC** checkboxes next to the
   participants you want to record.
2. (Optional) type a filename in the text field — otherwise the current
   timestamp is used.
3. Click **REC** (red). LabRecorder starts in the background and writes
   an XDF file in `%APPDATA%\BiHome\recordings\`.
4. Click **STOP** when done. The app also exports per-stream `.csv` files
   alongside the XDF for quick inspection.

---

## Troubleshooting

| Problem | Most likely cause | What to try |
|---|---|---|
| App won't launch | Missing VC++ Redistributable | Install link above |
| SmartScreen blocks app | Unsigned executable | "More info" → "Run anyway" |
| Polar not detected | Bluetooth off / device off / out of range | Toggle Bluetooth, verify Polar is on (LED) |
| Polar shows "unreachable" after retries | Already connected elsewhere | Close Polar Beat app or other paired apps |
| EmotiBit not detected | Firewall blocks Python on UDP 3131 | First scan will fall back to BrainFlow (~15 s) — wait |
| EmotiBit not detected (after retry) | Not on same WiFi as PC | Verify network in EmotiBit's WiFi config |
| Recording button greyed out | LabRecorderCLI missing | Don't move/rename `_internal/` folder |
| Plot windows don't open | Viewer subprocess failed | Check `acquisition.log` for error message |

To diagnose any issue: open
`%APPDATA%\BiHome\acquisition.log` in Notepad after a session.

---

## Uninstall

There's no installer — to remove BiHome:

1. Delete the **`BiHome Wearable/`** folder
2. (Optional) delete **`%APPDATA%\BiHome\`** to remove devices, settings,
   and recordings

---

## Sharing with colleagues

The exact same `BiHome Wearable/` folder works on any Windows machine
that meets the system requirements. Zip the folder, send it, the
recipient extracts it anywhere, and double-clicks the exe.

They will need to register their own devices on first launch — your
`devices.json` is **not** included in the build (it's per-user state).
