# PS2 Disc Dumper v2.1

Created by TrixR4PiGz

Copies a PlayStation 2 game disc from an optical drive to an `.iso` you can load in PCSX2.
PlayStation 1 discs dump to `.bin` + `.cue` (experimental).

Windows only. No extra Python packages are required to run it.

---

## 1. Install Python on Windows

Use the installer from the official site, not the Microsoft Store copy. The Store version often cannot open the optical drive.

1. Go to [https://www.python.org/downloads/windows/](https://www.python.org/downloads/windows/).
2. Download **Python 3.12** or newer (64-bit).
3. Run the installer.
4. On the first screen, tick **Add python.exe to PATH**.
5. Click **Install Now**.

Check it worked. Open **Command Prompt** and type:

```text
python --version
```

You should see something like `Python 3.12.x`. If Windows opens the Store instead, uninstall the Microsoft Store “Python” app and install from python.org again.

Tkinter (the window) is included with the python.org installer. If the program says tkinter is missing, re-run the installer, choose **Modify**, and tick **tcl/tk and IDLE**.

---

## 2. Put the script somewhere simple

1. Create a folder, for example `C:\PS2DiscDumper`.
2. Copy `Ps2_Disc_Dumper.py` into that folder.

Optional files (leave empty if you do not have them):

| File | What it does |
| --- | --- |
| PCSX2 `GameIndex.yaml` | Looks up the game title from the disc serial and names the file |
| Redump PS2 `.dat` | Checks your dump against a known-good hash, and can rename the file |
| `chdman.exe` | Converts the dump to a smaller `.chd` for PCSX2 |

Typical GameIndex location if PCSX2 is installed:

```text
%USERPROFILE%\Documents\PCSX2\resources\GameIndex.yaml
```

`chdman.exe` comes with MAME. You can also drop it in the same folder as the script.

---

## 3. Run it

### Normal way (double-click does not always work)

Open **Command Prompt** in the folder:

1. Open the folder in File Explorer.
2. Click the address bar, type `cmd`, and press Enter.
3. Run:

```text
python Ps2_Disc_Dumper.py
```

A window titled **PS2 Disc Dumper** should open.

### If the drive says “Access denied”

PS1 dumps, and some drives, need administrator rights.

1. Close the window.
2. Click Start, type `cmd`.
3. Right-click **Command Prompt** → **Run as administrator**.
4. Go to the folder and start it:

```text
cd /d C:\PS2DiscDumper
python Ps2_Disc_Dumper.py
```

You can also make a shortcut:

1. Right-click the desktop → **New** → **Shortcut**.
2. Location:

```text
python "C:\PS2DiscDumper\Ps2_Disc_Dumper.py"
```

3. Name it `PS2 Disc Dumper`.
4. Right-click the shortcut → **Properties** → **Advanced** → tick **Run as administrator**.

---

## 4. Dump a disc

1. Insert the game disc and wait until Windows has finished spinning it up.
2. In the window, pick the drive (for example `E:`) and press **Refresh** if it does not appear.
3. The label, type (PS2 / PS1), serial, and title should show up.
4. Choose a **save folder**. Do not use a FAT32 drive for games larger than 4 GB. Use NTFS or exFAT.
5. Check the file name. For multi-disc games, pick **Disc 1**, **Disc 2**, and so on before you start.
6. Options:
   - **Verify** re-reads the disc and compares it to the file. Slower, but safer.
   - **CHD** needs `chdman.exe`.
   - **Auto-rename to Redump name** only happens when a datfile match is found and there were no bad sectors.
   - **Full** is the normal mode. **Quick dump** does fewer retries and skips verify/CHD.
7. Press **Start dump**.
8. When it finishes you get:
   - `Game Name.iso` (or `.bin` + `.cue` for PS1)
   - `Game Name.log`
   - `Game Name.bad` only if some sectors could not be read

Load the `.iso` (or the `.cue` for PS1) in PCSX2 with **System → Load ISO**.

---

## 5. If something goes wrong

| What you see | What to do |
| --- | --- |
| `python` is not recognized | Reinstall from python.org and tick **Add python.exe to PATH**. Open a new Command Prompt. |
| Microsoft Store opens | Uninstall the Store Python app. Use python.org. |
| No optical drive found | The script only lists real CD/DVD drives. A virtual drive sometimes shows up, but a disc image is not a disc. |
| No readable disc | Close File Explorer windows that are sitting on the drive, then press **Refresh**. |
| Access denied | Run Command Prompt as administrator (see section 3). |
| FAT32 / not enough space | Save onto an NTFS or exFAT drive with more free space than the disc size. |
| Unreadable sectors | Clean the disc and try again. The `.bad` file lists the bad sectors. The game may not boot if any remain. |
| Verify FAILED | The disc is dirty or the drive is struggling. Clean it and dump again. |
| chdman not found | Browse to `chdman.exe` under **Optional files**, or leave CHD off. |
| PS3 disc | Not supported. PS3 discs need a Blu-ray drive and a different tool. |

Settings are saved here:

```text
%APPDATA%\PS2DiscDumper\config.json
```

Deleting that file resets the window to defaults.

---

## 6. Optional: make a single .exe (no Python window needed later)

Only do this if you want a program you can double-click without installing Python on another PC.

```text
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --name PS2DiscDumper Ps2_Disc_Dumper.py
```

The program is created at `dist\PS2DiscDumper.exe`. Put `chdman.exe` next to that exe if you want CHD conversion.

---

## What the program does

- Reads the disc sector by sector and writes an ISO (2048-byte sectors) or a raw BIN (2352-byte sectors).
- Retries bad sectors several times before filling them with zeros.
- Can re-read the disc to verify the file.
- Can check a Redump datfile and rename the dump to the official name.
- Writes a log next to every dump.
