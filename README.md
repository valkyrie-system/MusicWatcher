# MusicWatcher

**MusicWatcher** is a comprehensive music library management tool that scans your local collection, categorizes it, and helps you discover new releases from your favorite artists.

It features a threaded, non-blocking GUI, integration with [MusicBrainz](https://musicbrainz.org), a framework for lyric fetching, and P2P client integration for discovering new music.

---

## Core Features

### Library Scanning 
- Recursively scans your music directory for `.mp3`, `.flac`, `.m4a`, and `.ogg` files.
- **Intelligent Resume:** Stop and resume scans from the last file processed.
- **File Hashing:** Generates and stores SHA256 hashes in `.musicwatcher/directory-hash.json`.

### Categorized View
- Displays your library by **Artist** and **Album**.
- Shows counts of synced (`.lrc`) and plain (`.txt`) lyrics per album.

### MusicBrainz Integration
- Secure OAuth2 login (no manual token pasting).
- Fetches new album/EP releases for artists in your library.
- Saves known releases to `known_releases.json` to avoid duplicates.
- Displays new releases in a dedicated panel—double-click to open the MusicBrainz page.

### P2P Integration
- Auto-detects **Nicotine+** and **Soulseek** on Linux and Windows.
- Supports manual path configuration for other clients.
- **Auto-Search:** Automatically queries supported clients (currently Nicotine+).

### Lyric Fetching Framework
- Fetches missing lyrics, prioritizing synced `.lrc` files.
- Overwrites plain `.txt` lyrics if a synced version is found.
> **Note:** The lyric fetching logic is a placeholder and must be implemented in the `LyricFetcher` class.

---

## First-Time Setup

### Step 1: Register a MusicBrainz Application
1. Visit [https://musicbrainz.org/account/applications](https://musicbrainz.org/account/applications).
2. Click **Register your application**.
3. Fill out the form:
   - **Name:** `MusicWatcher`
   - **Type:** `Installed Application`
   - **Redirect URI:** `http://localhost:9090/oauth_callback`
4. Click **Register** and copy your **Client ID** and **Client Secret**.

### Step 2: Configure MusicWatcher
1. Launch MusicWatcher for the first time (see [Installation](#installation--running)).
2. Select your main music library folder when prompted.
3. Click **Set MB Credentials** and paste your Client ID/Secret.
4. Click **Save**.

### Step 3: Log In
1. Click **Login to MusicBrainz**.
2. Approve access in your browser.
3. You should see `Login Successful!` at `localhost:9090`.
4. Close the tab and return to MusicWatcher.

---

## Installation & Running

### Linux (Recommended)
```bash
chmod +x install_and_run_musicwatcher.sh
./install_and_run_musicwatcher.sh
```
This script:
- Creates a virtual environment (`venv/`)
- Installs dependencies from `requirements.txt`
- Launches the app

### Windows / Manual Setup
1. Install **Python 3.10+**.
2. Create a virtual environment:
   ```bash
   python3 -m venv venv
   ```
3. Activate it:
   - CMD: `venv\Scripts\activate`
   - PowerShell: `./venv/Scripts/Activate.ps1`
   - macOS/Linux: `source venv/bin/activate`
4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
5. Run the app:
   ```bash
   python3 musicwatcher.py
   ```

---

## Requirements

```bash
pip install -r requirements.txt
```
- PyQt6
- requests
- requests-oauthlib
- musicbrainzngs
- mutagen

---

## How to Use

### 1. Scan Your Library
- Click **Start Scan**.
- Progress bar shows completion.
- Artist/Album tree populates.
- Click **Stop Scan** to pause—resumes automatically.

### 2. Fetch Lyrics (Optional)
- Click **Fetch Missing Lyrics** after scanning.
- Prioritizes synced `.lrc` files.

### 3. Find New Releases
- Click **Fetch New Releases**.
- Queries MusicBrainz for your artists.
- Displays albums/EPs in the **New Releases** panel.

### 4. Download or Search
- Double-click a release to open it on MusicBrainz.
- Auto-searches via your configured P2P client.

---

## Configuration Files

| File | Purpose |
|------|----------|
| `.musicwatcher/directory-hash.json` | Stores SHA256 file hashes |
| `known_releases.json` | Prevents duplicate release notifications |
| `musicwatcher_config.json` | Stores user and P2P settings |

---

## Notes

- **Active development:** features may change.
- Lyric fetching framework pending full implementation.
- Feedback, issues, and PRs are welcome.

---

## License

Released under the **MIT License**.  
See [`LICENSE`](LICENSE) for details.

---

## Acknowledgements

- [MusicBrainz](https://musicbrainz.org)
- [Nicotine+](https://nicotine-plus.org/)
- [Soulseek](https://www.slsknet.org/)
- Python open-source community

---

> “Where words fail, music speaks.” — Hans Christian Andersen

