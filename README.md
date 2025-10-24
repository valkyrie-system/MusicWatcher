MusicWatcher

MusicWatcher is a comprehensive music library management tool designed to scan your local collection, categorize it, and help you discover new releases from your favorite artists.

It features a threaded, non-blocking GUI, integration with MusicBrainz for release data, a framework for lyric fetching, and integration with P2P clients for finding new music.

Core Features

Library Scanning: Recursively scans your music directory for .mp3, .flac, .m4a, and .ogg files.

Intelligent Resume: Scans can be stopped and will resume from the last file processed.

File Hashing: Generates and stores SHA256 hashes for all files in .musicwatcher/directory-hash.json within your music library.

Categorized View: Displays your library in a tree view, categorized by Artist and Album.

Lyrics Status: The library view shows counts of synced (.lrc) and plain (.txt) lyrics for each album.

MusicBrainz Integration:

Secure OAuth2 login (no manual token pasting).

Fetches new Album/EP releases for all artists found in your library.

Saves known releases to known_releases.json to prevent duplicates.

New Release Panel: New releases are displayed in a separate panel with Artist, Title, and Date. Double-clicking a release opens its MusicBrainz page.

P2P Integration:

Auto-detects Nicotine+ and Soulseek on Linux and Windows.

Allows setting a manual path to any P2P client executable.

Auto-Search: Automatically sends new releases as search queries to detected P2P clients (currently supports Nicotine+).

Lyric Fetching: A framework to fetch missing lyrics. It prioritizes synced .lrc files and will overwrite plain .txt files if a synced version is found.

Note: The actual web-scraping logic is a placeholder and must be implemented in the LyricFetcher class.

!! CRITICAL: First-Time Setup !!

You must follow these steps to use the MusicBrainz integration.

Step 1: Register a MusicBrainz Application

Go to https://musicbrainz.org/account/applications and log in.

Click "Register your application".

Fill out the form:

Name: MusicWatcher (or anything you like).

Type: "Installed Application".

Redirect URI: This is the most important part. You MUST use:

http://localhost:9090/oauth_callback


Agree to the terms and click "Register".

On the next page, you will see your Client ID and Client Secret. Keep this window open.

Step 2: Configure MusicWatcher

Run MusicWatcher for the first time (see installation steps below).

On first launch, the app will ask you to select your music directory. Choose your main music library folder.

In the main window, click the "Set MB Credentials" button.

A dialog will pop up. Copy and paste your Client ID and Client Secret from the MusicBrainz website into these fields.

Click "Save".

Step 3: Log In

Click the "Login to MusicBrainz" button.

Your default web browser will open to the MusicBrainz authorization page.

Click "Allow access".

Your browser will be redirected to a localhost:9090 page. You may see a "Login Successful!" message.

You can close the browser tab and return to MusicWatcher. The app is now authenticated.

Installation & Running

On Linux (Recommended)

The included shell script handles everything for you.

Make the script executable:

chmod +x install_and_run_musicwatcher.sh


Run the script:

./install_and_run_musicwatcher.sh


This script automatically creates a Python virtual environment (venv/), activates it, installs all required packages from requirements.txt, and launches the application.

On Windows / Manual Setup

Make sure you have Python 3.10 or newer installed.

Create a virtual environment:

python3 -m venv venv


Activate the environment:

Windows (CMD): venv\Scripts\activate

PowerShell: .\venv\Scripts\Activate.ps1

macOS/Linux: source venv/bin/activate

Install the required packages:

pip install -r requirements.txt


Run the application:

python3 musicwatcher.py


Requirements

The application requires the following Python packages (which install_and_run_musicwatcher.sh or pip install -r requirements.txt will install):

PyQt6

requests

requests-oauthlib

musicbrainzngs

mutagen

How to Use

Scan Your Library:

Click "Start Scan". The app will scan your music directory, generate hashes, and read tags.

The progress bar will show completion.

Your library will populate the "Artist / Album" tree. You can see file errors (missing tags) or lyric status.

You can click "Stop Scan" at any time. The app will save your position. Clicking "Start Scan" again will resume where you left off.

(Optional) Fetch Lyrics:

Once a scan is complete, click "Fetch Missing Lyrics".

The app will (in the future) search for lyrics for all scanned tracks, prioritizing .lrc files.

Find New Releases:

After a scan, click "Fetch New Releases".

The app will first search MusicBrainz for all your unique artist IDs. This may take time (it respects the 1-second/request API limit). You will see "Found on MB" or "Not Found on MB" in the "Status" column.

Once all artists are identified, it queries for new albums and EPs.

New releases appear in the "New Releases" panel at the bottom.

Download New Music:

Double-click any release in the "New Releases" panel to open its MusicBrainz page in your browser.

If Auto-Search is enabled (in musicwatcher_config.json) and you have a supported P2P client (like Nicotine+), the app will automatically run a search for that release (Artist Album).

You can also click "Launch P2P Client" to open your client manually. If no client is found, click "Set P2P Client Path" to select the .exe or executable file manually.
