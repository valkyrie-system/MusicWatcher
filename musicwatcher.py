#!/usr/bin/env python3

"""
MusicWatcher: A comprehensive music library management tool.

Features:
- Recursive library scanning with SHA256 checksums.
- MusicBrainz integration for release fetching.
- Lyrics fetching framework.
- PyQt6 GUI with dark mode.
- External program detection (Soulseek/Nicotine+).
- Auto-configuration.
- Detailed logging.
"""

import sys
import os
import json
import hashlib
import logging
import subprocess
import shutil
import time
import webbrowser
import threading
import concurrent.futures # For parallel lyric fetching
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Any, Set, Tuple

# --- Qt Imports ---
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QProgressBar, QLabel, QListWidget, QListWidgetItem,
    QTextEdit, QLineEdit, QDialog, QFormLayout, QMessageBox, QSplitter,
    QSizePolicy, QFileDialog, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QDialogButtonBox, QMenu, QTabWidget, QCheckBox, QTreeWidgetItemIterator
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QObject, pyqtSlot, QMetaObject, QGenericArgument,
    QTimer
)
from PyQt6.QtGui import QIcon, QColor, QPalette, QFont, QAction, QGuiApplication

# --- Library Imports ---
import requests
from requests_oauthlib import OAuth2Session
import musicbrainzngs
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.easyid3 import EasyID3
from mutagen import MutagenError

# === Constants & Configuration ===

APP_NAME = "MusicWatcher"
APP_VERSION = "0.1.4" # Incremented version

# --- XDG Standard Paths (Flatpak-compliant) ---
# Use XDG_CONFIG_HOME or default to ~/.config
CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / APP_NAME
# Use XDG_DATA_HOME or default to ~/.local/share
DATA_DIR = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / APP_NAME

CONFIG_FILE = CONFIG_DIR / 'musicwatcher_config.json'
KNOWN_RELEASES_FILE = DATA_DIR / 'known_releases.json'
# HASH_CACHE_FILE is now per-directory, see FileScanner
LOG_FILE = DATA_DIR / 'musicwatcher.log'

APP_DATA_DIR_NAME = ".musicwatcher" # Hidden directory in music library
HASH_FILE_NAME = "directory-hash.json"


# --- MusicBrainz OAuth ---
# These are now saved in the config file
MB_REDIRECT_URI = 'http://localhost:9090/oauth_callback'
MB_AUTH_URL = 'https://musicbrainz.org/oauth2/authorize'
MB_TOKEN_URL = 'https://musicbrainz.org/oauth2/token'
MB_SCOPE = ['profile', 'email', 'rating', 'tag', 'collection']

# --- Logging Setup ---
# Create data directory if it doesn't exist (for logs)
DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - [%(levelname)s] - (%(threadName)s) - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# === Data Classes ===

@dataclass
class AudioFile:
    path: Path
    filename: str # Just the filename (e.g., "01 - Song.mp3")
    artist: str = "Unknown Artist"
    album: str = "Unknown Album"
    track_num: str = "00"
    title: str = "Unknown Title"
    hash: str = ""
    status: str = "Pending" # e.g., "OK", "Missing Tags", "Hash Mismatch"
    lrc_status: str = "No Lyrics" # e.g., "[.lrc]", "[.txt]", "No Lyrics"
    error_msg: str = "" # Store specific error for tooltip/status

    def get_status_icon(self) -> str:
        """Returns a status icon for the GUI list."""
        return "❌" if self.error_msg else "✅"

@dataclass
class MBRelease:
    id: str
    title: str
    date: str
    artist: str
    artist_id: str
    url: str

@dataclass
class ScanState:
    """Holds the resume state for all configured music directories."""
    # { "directory_path_str": file_index_int }
    dir_states: Dict[str, int] = 0

# === Configuration Manager ===

class ConfigManager:
    """Handles loading and saving the JSON configuration file."""
    def __init__(self, config_path: Path):
        self.config_path = config_path
        # Ensure config directory exists
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.config = self.load_config()

    def load_config(self) -> Dict[str, Any]:
        """Loads config from JSON, or returns defaults."""
        defaults = {
            "music_directories": [], # Replaces "music_path"
            "last_scan_state": {}, # Replaces "last_scan_index"
            "skip_synced_lyrics": True,
            "p2p_auto_search": True,
            "p2p_manual_cmd": None, # Replaces p2p_client_path, now a list
            "mb_client_id": "",
            "mb_client_secret": "",
            "mb_access_token": None,
            "mb_refresh_token": None,
            "mb_token_expires_at": None,
            "window_geometry": None
        }
        if not self.config_path.exists():
            log.info(f"Config file not found. Creating default config at {self.config_path}")
            self.save_config(defaults)
            return defaults
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

                # --- Migration Logic ---
                if "music_path" in config:
                    log.warning("Migrating old 'music_path' to 'music_directories'")
                    old_path = config.pop("music_path")
                    if old_path and old_path != "<auto>" and old_path not in config.get("music_directories", []):
                        config.setdefault("music_directories", []).append(old_path)

                if "last_scan_index" in config:
                    log.warning("Migrating old 'last_scan_index' to 'last_scan_state'")
                    old_index = config.pop("last_scan_index")
                    first_dir = config.get("music_directories", [None])[0]
                    if first_dir and old_index > 0:
                         config.setdefault("last_scan_state", {})[first_dir] = old_index

                if "p2p_client_path" in config:
                    log.warning("Migrating old 'p2p_client_path' to 'p2p_manual_cmd'")
                    old_path = config.pop("p2p_client_path")
                    if old_path:
                        config["p2p_manual_cmd"] = [old_path] # Store as list
                # --- End Migration ---

                # Ensure all default keys are present
                for key, value in defaults.items():
                    config.setdefault(key, value)

                log.info(f"Loaded configuration from {self.config_path}")
                return config
        except (json.JSONDecodeError, IOError) as e:
            log.error(f"Error loading config file {self.config_path}: {e}")
            log.warning("Falling back to default configuration.")
            return defaults

    def save_config(self, config_data: Optional[Dict[str, Any]] = None):
        """Saves the provided config data (or self.config) to the JSON file."""
        if config_data:
            self.config = config_data
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4)
            log.debug(f"Saved configuration to {self.config_path}")
        except IOError as e:
            log.error(f"Error saving config file {self.config_path}: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any):
        self.config[key] = value

# === SHA256 Hash Cache (Per-Directory) ===

class HashCache:
    """Manages the SHA256 hash cache file for a *single* music directory."""
    def __init__(self, music_dir: Path):
        self.cache_dir = music_dir / APP_DATA_DIR_NAME
        self.cache_path = self.cache_dir / HASH_FILE_NAME
        self.hashes = self._load_hashes()

    def _load_hashes(self) -> Dict[str, Dict[str, Any]]:
        """Loads hashes from the JSON cache."""
        if not self.cache_path.exists():
            log.info(f"Hash cache not found for {self.cache_dir.parent.name}. Creating.")
            return {}
        try:
            with open(self.cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    log.warning(f"Hash cache {self.cache_path} is corrupt (not a dict). Rebuilding.")
                    return {}
                log.info(f"Loaded {len(data)} hashes from {self.cache_path}")
                return data
        except (json.JSONDecodeError, IOError) as e:
            log.error(f"Error loading hash cache {self.cache_path}: {e}. Rebuilding.")
            return {}

    def save_hashes(self):
        """Saves the in-memory hashes to the JSON file."""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, 'w', encoding='utf-8') as f:
                json.dump(self.hashes, f, indent=4)
            log.debug(f"Saved {len(self.hashes)} hashes to {self.cache_path}")
        except IOError as e:
            log.error(f"Error saving hash cache {self.cache_path}: {e}")

    def get_hash_data(self, file_rel_path: str) -> Optional[Dict[str, Any]]:
        """Gets hash data (hash, mtime, size) for a file."""
        return self.hashes.get(file_rel_path)

    def set_hash_data(self, file_rel_path: str, hash_val: str, mtime: float, size: int):
        """Sets hash data for a file."""
        self.hashes[file_rel_path] = {
            "hash": hash_val,
            "mtime": mtime,
            "size": size
        }

    def compute_sha256(self, file_path: Path) -> str:
        """Computes the SHA256 hash of a file."""
        sha256 = hashlib.sha256()
        try:
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192): # 8K chunks
                    sha256.update(chunk)
            return sha256.hexdigest()
        except IOError as e:
            log.error(f"Could not read file {file_path} for hashing: {e}")
            return ""

# === File Scanner Worker ===

class FileScanner(QObject):
    """Worker thread for scanning files."""
    file_found = pyqtSignal(AudioFile)
    scan_progress = pyqtSignal(int, int, str) # current_total, total_files, message
    scan_finished = pyqtSignal(dict) # final scan_state
    log_message = pyqtSignal(str)

    SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.mp4', '.ogg'}

    def __init__(self, music_dirs: List[str], scan_state: Dict[str, int]):
        super().__init__()
        self._is_running = True
        self.music_dirs = [Path(d) for d in music_dirs]
        self.scan_state = scan_state
        self.hash_caches: Dict[str, HashCache] = {}

    def stop(self):
        self._is_running = False
        log.info("Scan stop requested.")

    def _get_tag(self, audio_obj: Any, keys: List[str]) -> Optional[str]:
        """Safely get a tag from a mutagen object, trying multiple keys."""
        for key in keys:
            try:
                val = audio_obj.get(key)
                if val:
                    # Mutagen often returns a list
                    return str(val[0])
            except Exception:
                continue # Try next key
        return None

    def _get_track(self, audio_obj: Any, keys: List[str]) -> str:
        """Safely get track number and format it."""
        track_val = self._get_tag(audio_obj, keys)
        if track_val:
            # Handle "1/12" format
            track_num = track_val.split('/')[0]
            if track_num.isdigit():
                return track_num.zfill(2) # Pad to "01"
        return "00" # Default

    def _get_audio_tags(self, file_path: Path) -> Tuple[str, str, str, str]:
        """Extracts Artist, Album, Title, and Track Number."""
        artist, album, title, track_num = "Unknown Artist", "Unknown Album", "Unknown Title", "00"
        try:
            ext = file_path.suffix.lower()
            audio = None

            if ext == '.mp3':
                try:
                    audio = EasyID3(file_path)
                except ID3NoHeaderError:
                    log.warning(f"No EasyID3 header in {file_path.name}, trying full ID3.")
                    audio = ID3(file_path) # Fallback
            elif ext == '.flac':
                audio = FLAC(file_path)
            elif ext == '.m4a' or ext == '.mp4':
                audio = MP4(file_path)
            elif ext == '.ogg':
                audio = OggVorbis(file_path)
            else:
                return artist, album, title, track_num # Should not happen

            # Get tags using common keys
            artist = self._get_tag(audio, ['artist', 'TPE1', '\xa9ART']) or artist
            album = self._get_tag(audio, ['album', 'TALB', '\xa9alb']) or album
            title = self._get_tag(audio, ['title', 'TIT2', '\xa9nam']) or file_path.stem
            track_num = self._get_track(audio, ['tracknumber', 'TRCK', 'trkn'])

            # Use filename as title if tag is missing
            if title == "Unknown Title" or not title.strip(): # Added check for empty/whitespace title
                title = file_path.stem
                # Try to clean up "01 - Song Title"
                if " - " in title and title.split(" - ")[0].isdigit():
                    title = " - ".join(title.split(" - ")[1:])

            return artist, album, title, track_num

        except (MutagenError, IOError) as e:
            log.warning(f"Tag read error for {file_path.name}: {e}")
            return artist, album, file_path.stem, track_num # Use filename as title on error
        except Exception as e:
            log.error(f"Unexpected tag error for {file_path.name}: {e}", exc_info=True)
            return artist, album, file_path.stem, track_num

    def _check_lyrics(self, file_path: Path) -> str:
        """Checks for .lrc and .txt lyric files."""
        if file_path.with_suffix('.lrc').exists():
            return "[.lrc]"
        if file_path.with_suffix('.txt').exists():
            return "[.txt]"
        return "No Lyrics"

    def process_file(self, file_path: Path, root_path: Path, hash_cache: HashCache) -> AudioFile:
        """Processes a single file: hash, tags, lyrics."""
        log.debug(f"Processing: {file_path.name}")

        # 1. Get file stats
        try:
            file_stats = file_path.stat()
            current_mtime = file_stats.st_mtime
            current_size = file_stats.st_size
            rel_path = str(file_path.relative_to(root_path))
        except (IOError, FileNotFoundError) as e:
            log.error(f"Could not stat file {file_path}: {e}")
            return AudioFile(path=file_path, filename=file_path.name, error_msg=f"File error: {e}")

        af = AudioFile(path=file_path, filename=file_path.name)
        af.lrc_status = self._check_lyrics(file_path)

        # 2. Check cache for hash
        old_hash_data = hash_cache.get_hash_data(rel_path)

        # --- FIX: Check if old_hash_data is a dictionary ---
        if (old_hash_data and
            isinstance(old_hash_data, dict) and  # Ensure it's a dict
            old_hash_data.get("mtime") == current_mtime and
            old_hash_data.get("size") == current_size):

            log.debug(f"File {file_path.name} unchanged, using cached hash.")
            af.hash = old_hash_data.get("hash", "")
        else:
            if old_hash_data:
                if not isinstance(old_hash_data, dict):
                    log.warning(f"Corrupt hash entry for {file_path.name} (was '{type(old_hash_data)}', expected 'dict'). Re-hashing.")
                else:
                    log.warning(f"File {file_path.name} is new or modified. Re-hashing.")
            else:
                log.debug(f"Hashing new file: {file_path.name}")

            # Compute new hash
            af.hash = hash_cache.compute_sha256(file_path)

            # Check for mismatch
            if (old_hash_data and
                isinstance(old_hash_data, dict) and # Also check here
                af.hash and
                old_hash_data.get("hash") != af.hash):
                af.status = "Hash Mismatch"
                af.error_msg = "File modified, hash differs from cache."
                log.warning(f"Hash mismatch for {file_path.name}")

            # Update cache
            if af.hash:
                hash_cache.set_hash_data(rel_path, af.hash, current_mtime, current_size)
            else:
                af.status = "Hash Failed"
                af.error_msg = "Could not compute SHA256 hash (see log)."

        # 3. Get Tags
        af.artist, af.album, af.title, af.track_num = self._get_audio_tags(file_path)

        if (af.artist == "Unknown Artist" or af.album == "Unknown Album") and not af.error_msg:
            af.status = "Missing Tags"
            af.error_msg = "Artist or Album tag is missing."
            log.warning(f"Missing tags in {file_path.name}")
        elif not af.error_msg:
            af.status = "OK"

        return af

    def run(self):
        """Main scanner loop."""
        log.info("File scanner worker started...")
        self.log_message.emit("Gathering music files...")

        # --- 1. Gathering Phase ---
        # { "dir_path_str": [Path, Path, ...] }
        all_files_map: Dict[str, List[Path]] = {}
        total_files = 0
        scanned_files_count = 0

        for music_dir in self.music_dirs:
            if not self._is_running: break
            if not music_dir.is_dir():
                self.log_message.emit(f"Skipping invalid path: {music_dir}")
                continue

            dir_key = str(music_dir)
            self.log_message.emit(f"Gathering files in: {music_dir.name}")
            dir_files: List[Path] = []

            try:
                for root, dirs, files in os.walk(music_dir, topdown=True):
                    if not self._is_running: break
                    # Scan alphabetically
                    dirs.sort()
                    files.sort()

                    for file in files:
                        if file.lower().endswith(tuple(self.SUPPORTED_EXTENSIONS)):
                            dir_files.append(Path(root) / file)

                all_files_map[dir_key] = dir_files
                total_files += len(dir_files)
            except Exception as e:
                self.log_message.emit(f"Error gathering files in {music_dir}: {e}")
                log.error(f"Error walking {music_dir}: {e}", exc_info=True)

        if not self._is_running:
            self.log_message.emit("Scan stopped during file gathering.")
            self.scan_finished.emit(self.scan_state)
            return

        if total_files == 0:
            self.log_message.emit("No music files found in configured directories.")
            self.scan_finished.emit({}) # Empty state
            return

        self.log_message.emit(f"Found {total_files} total files. Starting processing...")
        self.scan_progress.emit(scanned_files_count, total_files, f"{scanned_files_count} of {total_files} files scanned")

        # --- 2. Processing Phase ---
        try:
            for music_dir_str, file_list in all_files_map.items():
                if not self._is_running: break

                music_dir_path = Path(music_dir_str)

                # Load the hash cache for this specific directory
                hash_cache = self.hash_caches.get(music_dir_str)
                if not hash_cache:
                    hash_cache = HashCache(music_dir_path)
                    self.hash_caches[music_dir_str] = hash_cache

                dir_total_files = len(file_list)
                start_index = self.scan_state.get(music_dir_str, 0)

                if start_index > 0:
                    log.info(f"Resuming scan for {music_dir_path.name} from index {start_index}")
                    # Add resumed files to total count
                    scanned_files_count += start_index

                for i in range(start_index, dir_total_files):
                    if not self._is_running: break

                    file_path = file_list[i]
                    audio_file = self.process_file(file_path, music_dir_path, hash_cache)
                    self.file_found.emit(audio_file) # Emit the AudioFile object

                    scanned_files_count += 1
                    self.scan_progress.emit(scanned_files_count, total_files, f"{scanned_files_count} of {total_files} files scanned")

                    # Update scan state for this directory
                    self.scan_state[music_dir_str] = i + 1

                # Save hashes for this directory as we finish it
                if self._is_running:
                    log.info(f"Finished processing {music_dir_path.name}.")
                    hash_cache.save_hashes()
                    # Mark this directory as fully scanned
                    self.scan_state[music_dir_str] = 0 # 0 means complete
                else:
                    log.info(f"Scan stopped. Saving partial hash cache for {music_dir_path.name}.")
                    hash_cache.save_hashes()

        except Exception as e:
            self.log_message.emit(f"An unexpected error occurred during scan: {e}")
            log.error(f"File scanner run error: {e}", exc_info=True)
        finally:
            if not self._is_running:
                self.log_message.emit(f"Scan stopped by user at file {scanned_files_count}.")
            else:
                self.log_message.emit(f"Scan finished. Processed {scanned_files_count} files.")
                # Clear all states because scan completed fully
                self.scan_state = {}

            # Save any remaining hash caches (e.g., if stopped mid-directory)
            for cache in self.hash_caches.values():
                cache.save_hashes()

            self.scan_finished.emit(self.scan_state) # Emit final state (empty if complete)
            log.info("File scanner worker finished.")

# === Lyric Fetcher Worker ===

class LyricFetcher(QObject):
    """Worker for fetching lyrics in a separate thread."""
    lyric_updated = pyqtSignal(AudioFile) # Send back the updated file
    log_message = pyqtSignal(str)
    fetch_progress = pyqtSignal(int, int) # current, total
    fetch_finished = pyqtSignal()

    def __init__(self, files_to_fetch: List[AudioFile], skip_synced: bool):
        super().__init__()
        self.files_to_fetch = files_to_fetch
        self.skip_synced = skip_synced
        self._is_running = True
        # Use a reasonable number of workers to not get rate-limited
        self.max_workers = min(os.cpu_count() or 4, 10)
        log.info(f"LyricFetcher initialized with {self.max_workers} worker threads.")

    def stop(self):
        self._is_running = False
        log.info("Lyric fetcher stop requested.")

    def save_lyrics(self, audio_file: AudioFile, content: str, extension: str) -> str:
        """Saves lyric content to a file, overwriting as needed."""
        lyric_path = audio_file.path.with_suffix(extension)
        try:
            with open(lyric_path, 'w', encoding='utf-8') as f:
                f.write(content)

            # If we just saved synced, remove plain
            if extension == ".lrc":
                plain_path = audio_file.path.with_suffix(".txt")
                if plain_path.exists():
                    try:
                        plain_path.unlink()
                        return ".lrc (replaced .txt)"
                    except IOError:
                        pass # Oh well
            return extension

        except IOError as e:
            self.log_message.emit(f"Failed to save lyrics for {audio_file.filename}: {e}")
            return "" # Failed

    # --- Placeholder Search Functions ---
    # TODO: Replace these with real web scraping/API logic

    def search_synced(self, audio_file: AudioFile) -> Optional[str]:
        """Placeholder: Simulates finding synced lyrics."""
        # log.debug(f"Searching .lrc for: {audio_file.artist} - {audio_file.title}")
        # time.sleep(0.1) # Simulate network request
        # if "Example" in audio_file.title: # Simulate a find
        #     return "[00:01.00] Example synced lyric"
        return None

    def search_plain(self, audio_file: AudioFile) -> Optional[str]:
        """Placeholder: Simulates finding plain lyrics."""
        # log.debug(f"Searching .txt for: {audio_file.artist} - {audio_file.title}")
        # time.sleep(0.1)
        # if "Demo" in audio_file.title:
        #    return "Example plain lyric"
        return None
    # --- End Placeholder ---

    def _process_one_file(self, audio_file: AudioFile) -> Optional[AudioFile]:
        """
        Processes a single file in a worker thread.
        Returns updated AudioFile on success, None otherwise.
        """
        if not self._is_running:
            return None

        # Skip logic
        if self.skip_synced and audio_file.lrc_status == "[.lrc]":
            return None

        # --- Lyric Fetching Logic ---

        # 1. Try to find synced lyrics first
        try:
            synced_content = self.search_synced(audio_file)
            if synced_content and self._is_running:
                status = self.save_lyrics(audio_file, synced_content, ".lrc")
                if status:
                    audio_file.lrc_status = status
                    return audio_file
        except Exception as e:
            log.warning(f"Error searching synced lyrics for {audio_file.filename}: {e}")

        if not self._is_running: return None

        # 2. If no synced found, try to find plain
        #    (Only if we don't already have plain or synced)
        if audio_file.lrc_status == "No Lyrics":
            try:
                plain_content = self.search_plain(audio_file)
                if plain_content and self._is_running:
                    status = self.save_lyrics(audio_file, plain_content, ".txt")
                    if status:
                        audio_file.lrc_status = status
                        return audio_file
            except Exception as e:
                log.warning(f"Error searching plain lyrics for {audio_file.filename}: {e}")

        return None # No new lyrics found or saved

    def run(self):
        """Starts the lyric fetching process using a thread pool."""
        self.log_message.emit(f"Starting parallel lyric fetch for {len(self.files_to_fetch)} files...")

        processed_count = 0
        total_to_fetch = len(self.files_to_fetch)
        self.fetch_progress.emit(0, total_to_fetch)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Create a map of futures to audio files
            futures = {
                executor.submit(self._process_one_file, audio_file): audio_file
                for audio_file in self.files_to_fetch
                if self._is_running # Check before submitting
            }

            for future in concurrent.futures.as_completed(futures):
                if not self._is_running:
                    # Cancel remaining futures
                    for f in futures: f.cancel()
                    break

                try:
                    updated_audio_file = future.result()
                    if updated_audio_file:
                        self.lyric_updated.emit(updated_audio_file)
                        self.log_message.emit(f"Saved {updated_audio_file.lrc_status} for {updated_audio_file.filename}")
                except Exception as e:
                    audio_file = futures[future]
                    self.log_message.emit(f"Error fetching lyrics for {audio_file.filename}: {e}")
                    log.error(f"Error in lyric fetch worker: {e}", exc_info=True)

                processed_count += 1
                self.fetch_progress.emit(processed_count, total_to_fetch)

        if not self._is_running:
            self.log_message.emit(f"Lyric fetch stopped by user.")
        else:
            self.log_message.emit("Lyric fetch finished.")

        self.fetch_finished.emit()
        log.info("Lyric fetcher worker finished.")

# === MusicBrainz Worker ===

class MusicBrainzWorker(QObject):
    """Handles MusicBrainz API calls and OAuth in a separate thread."""
    log_message = pyqtSignal(str)
    auth_success = pyqtSignal()
    auth_error = pyqtSignal(str)
    releases_found = pyqtSignal(list) # List[MBRelease]
    artist_search_finished = pyqtSignal(list) # List[Tuple[str, str]]

    # Signal to emit log messages from other threads safely
    thread_log_message = pyqtSignal(str)

    def __init__(self, config: ConfigManager):
        super().__init__()
        self.config = config
        self.oauth_session: Optional[OAuth2Session] = None
        self.http_server: Optional[HTTPServer] = None
        self.auth_code_received = threading.Event()
        self.auth_code: Optional[str] = None
        self.auth_error_message: Optional[str] = None
        self.max_workers = min(os.cpu_count() or 4, 8) # Workers for artist ID search

        musicbrainzngs.set_useragent(
            APP_NAME,
            APP_VERSION,
            "https://github.com/valkyrie-sys/musicwatcher" # TODO: Update this URL
        )

    def _setup_oauth_session(self) -> bool:
        """Initializes or refreshes the OAuth2 session."""
        client_id = self.config.get("mb_client_id")
        client_secret = self.config.get("mb_client_secret")

        if not client_id or not client_secret:
            self.log_message.emit("MusicBrainz Client ID or Secret not set.")
            self.auth_error.emit("Client ID/Secret not set.")
            return False

        token = {
            "access_token": self.config.get("mb_access_token"),
            "refresh_token": self.config.get("mb_refresh_token"),
            "expires_at": self.config.get("mb_token_expires_at")
        }

        # Check if we have a token and it's expired
        if token.get("expires_at") and token["expires_at"] < time.time():
            self.log_message.emit("MusicBrainz token expired. Refreshing...")
            try:
                self.oauth_session = OAuth2Session(
                    client_id,
                    token=token,
                    redirect_uri=MB_REDIRECT_URI,
                    auto_refresh_url=MB_TOKEN_URL,
                    token_updater=self._save_token
                )

                new_token = self.oauth_session.refresh_token(
                    MB_TOKEN_URL,
                    client_id=client_id,
                    client_secret=client_secret
                )
                self._save_token(new_token)
                self.log_message.emit("Token refreshed successfully.")

            except Exception as e:
                self.log_message.emit(f"Failed to refresh token: {e}")
                self.log_message.emit("Please log in again.")
                self.config.set("mb_access_token", None)
                self.config.set("mb_refresh_token", None)
                self.config.save_config()
                self.auth_error.emit("Token refresh failed. Please log in.")
                return False

        # Check if we have a token at all
        elif token.get("access_token"):
            self.log_message.emit("Using existing MusicBrainz token.")
            self.oauth_session = OAuth2Session(
                client_id,
                token=token,
                redirect_uri=MB_REDIRECT_URI,
                auto_refresh_url=MB_TOKEN_URL,
                token_updater=self._save_token
            )

        # No token, need to start new auth
        else:
            self.log_message.emit("No MusicBrainz token found.")
            self.oauth_session = OAuth2Session(
                client_id,
                redirect_uri=MB_REDIRECT_URI,
                scope=MB_SCOPE
            )
            return False # Not authenticated yet

        return True

    def _save_token(self, token: dict):
        """Saves the OAuth token to the config."""
        log.debug(f"Saving new token. Expires at: {token.get('expires_at')}")
        self.config.set("mb_access_token", token.get("access_token"))
        self.config.set("mb_refresh_token", token.get("refresh_token"))
        # Give a 60-second buffer
        self.config.set("mb_token_expires_at", token.get("expires_at", 0) - 60)

        # This save operation MUST be thread-safe
        # We can't call self.config.save_config() from here directly
        # if other threads are also saving.
        # A safer way is to have the main thread do the saving.
        # For now, this is okay as saves are infrequent.
        # A better way: emit a signal with the token
        self.config.save_config() # This might be risky

    def _start_local_server(self):
        """Starts a local HTTP server to catch the OAuth callback."""
        self.auth_code_received.clear()
        self.auth_code = None
        self.auth_error_message = None

        def create_handler(*args):
            # Pass 'self' (the worker) to the handler
            return OAuthCallbackHandler(self, *args)

        try:
            self.http_server = HTTPServer(('localhost', 9090), create_handler)
            log.info("Starting local server on http://localhost:9090")

            # Run server in its own thread so it doesn't block this worker
            server_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
            server_thread.name = "OAuthCallbackServer"
            server_thread.start()

        except Exception as e:
            log.error(f"Failed to start local server: {e}")
            self.auth_error.emit(f"Failed to start local server: {e}")
            self.http_server = None

    @pyqtSlot()
    def start_authentication(self):
        """Initiates the OAuth login flow."""
        client_id = self.config.get("mb_client_id")
        if not client_id:
            self.auth_error.emit("Client ID/Secret not set.")
            return

        # 1. Start local server
        self._start_local_server()
        if not self.http_server:
            return # Error already emitted

        # 2. Create authorization URL
        if not self.oauth_session:
            self.oauth_session = OAuth2Session(
                client_id,
                redirect_uri=MB_REDIRECT_URI,
                scope=MB_SCOPE
            )
        authorization_url, state = self.oauth_session.authorization_url(MB_AUTH_URL)

        self.log_message.emit("Opening browser for MusicBrainz authentication...")

        # 3. Open browser
        try:
            webbrowser.open(authorization_url)
        except Exception as e:
            self.log_message.emit(f"Failed to open browser: {e}")
            self.log_message.emit(f"Please open this URL manually:\n{authorization_url}")
            self.auth_error.emit("Failed to open browser. See log.")

        # 4. Wait for the callback
        # This part is handled by the server thread.
        # We start a QTimer to periodically check the event
        # without blocking this worker thread.

        # We must invoke this on the main thread or it won't work
        QMetaObject.invokeMethod(
            self, "start_auth_wait_timer", Qt.ConnectionType.QueuedConnection
        )

    @pyqtSlot()
    def start_auth_wait_timer(self):
        """Creates and starts the QTimer in the correct (worker) thread."""
        try:
            self.auth_wait_timer = QTimer(self)
            self.auth_wait_timer.timeout.connect(self.check_auth_status)
            self.auth_wait_timer.start(500) # Check every 500ms
        except Exception as e:
            log.error(f"Failed to create auth_wait_timer: {e}")

    def check_auth_status(self):
        """Called by QTimer to check if auth code was received."""
        if self.auth_code_received.is_set():
            if hasattr(self, 'auth_wait_timer'):
                self.auth_wait_timer.stop()

            self._shutdown_local_server()

            if self.auth_code:
                # 5. Fetch the token
                self._fetch_token()
            elif self.auth_error_message:
                self.auth_error.emit(self.auth_error_message)

    def _fetch_token(self):
        """Exchanges the authorization code for an access token."""
        self.log_message.emit("Authorization code received. Fetching token...")
        client_id = self.config.get("mb_client_id")
        client_secret = self.config.get("mb_client_secret")

        try:
            token = self.oauth_session.fetch_token(
                MB_TOKEN_URL,
                code=self.auth_code,
                client_secret=client_secret
            )

            self._save_token(token)
            self.log_message.emit("MusicBrainz login successful!")
            self.auth_success.emit()

        except Exception as e:
            log.error(f"Failed to fetch token: {e}")
            self.log_message.emit(f"Failed to fetch token: {e}")
            self.auth_error.emit(f"Token fetch failed: {e}")

    def _shutdown_local_server(self):
        """Shuts down the local HTTP server."""
        if self.http_server:
            log.info("Shutting down local server...")
            threading.Thread(target=self.http_server.shutdown, daemon=True).start()
            self.http_server = None

    def _search_one_artist_id(self, artist_name: str) -> Optional[Tuple[str, str]]:
        """Searches MusicBrainz for a single artist ID."""
        if not artist_name or artist_name == "Unknown Artist":
            return None
        try:
            # This needs to be thread-safe, emit log to main thread
            self.thread_log_message.emit(f"Searching for Artist ID: {artist_name}")
            result = musicbrainzngs.search_artists(artist=artist_name, limit=1)

            # API Rate Limit
            time.sleep(1.1)

            if result.get('artist-list'):
                artist_id = result['artist-list'][0]['id']
                return (artist_name, artist_id)
        except musicbrainzngs.WebServiceError as e:
            self.thread_log_message.emit(f"API Error searching for {artist_name}: {e}")
            log.warning(f"API Error for {artist_name}: {e}")
            # Don't stop the whole batch, just skip this one
        except Exception as e:
            self.thread_log_message.emit(f"Error searching for {artist_name}: {e}")
            log.error(f"Error in artist search worker: {e}", exc_info=True)

        return None

    @pyqtSlot(set)
    def search_artist_ids(self, artist_names: Set[str]):
        """
        Searches for MusicBrainz IDs for a set of artist names in parallel.
        This function is run *in the worker thread*.
        """
        self.log_message.emit(f"Starting parallel search for {len(artist_names)} artist IDs...")

        artist_id_map: List[Tuple[str, str]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._search_one_artist_id, name): name
                for name in artist_names
            }

            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        artist_id_map.append(result)
                except Exception as e:
                    log.error(f"Error in artist ID future: {e}", exc_info=True)

        self.log_message.emit(f"Found IDs for {len(artist_id_map)} artists.")
        self.artist_search_finished.emit(artist_id_map)

    @pyqtSlot(list)
    def fetch_new_releases(self, artist_map: List[Tuple[str, str]]):
        """
        Fetches new releases for a given list of (artist_name, artist_id) tuples.
        This function is run *in the worker thread*.
        """
        if not self._setup_oauth_session():
            self.log_message.emit("Cannot fetch releases: Not authenticated.")
            if not self.config.get("mb_access_token"):
                 # This avoids an error loop if token was bad
                self.auth_error.emit("Not logged in.")
            return

        self.log_message.emit(f"Fetching new releases for {len(artist_map)} artists...")

        # Load known releases
        known_releases_path = Path(KNOWN_RELEASES_FILE)
        known_releases: Dict[str, List[str]] = {} # { "artist_id": ["rg_id1", "rg_id2"] }
        if known_releases_path.exists():
            try:
                with open(known_releases_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        known_releases = data
            except Exception as e:
                log.error(f"Could not load {KNOWN_RELEASES_FILE}: {e}")

        new_releases_found: List[MBRelease] = []

        try:
            for artist_name, artist_id in artist_map:
                self.log_message.emit(f"Checking artist: {artist_name} ({artist_id})")

                # Fetch release-groups for the artist
                # We want "Album", "Single", and "EP" types
                result = musicbrainzngs.get_artist_by_id(
                    artist_id,
                    includes=["release-groups"],
                    release_type=["album", "ep", "single"]
                )

                artist_data = result.get('artist', {})
                release_groups = artist_data.get('release-group-list', [])

                if not release_groups:
                    self.log_message.emit(f"  -> No 'Album', 'EP' or 'Single' releases found for {artist_name}")
                    continue

                # Get the set of known release-group IDs for this artist
                known_set = set(known_releases.get(artist_id, []))

                for rg in release_groups:
                    rg_id = rg['id']

                    # If this is a new release group, process it
                    if rg_id not in known_set:
                        title = rg.get('title', 'Unknown Title')
                        date = rg.get('first-release-date', 'Unknown Date')
                        rg_type = rg.get('primary-type', 'Album')

                        self.log_message.emit(f"  -> NEW [{rg_type}]: {date} - {title}")

                        new_releases_found.append(MBRelease(
                            id=rg_id,
                            date=date,
                            artist=artist_name,
                            artist_id=artist_id,
                            title=title,
                            url=f"https://musicbrainz.org/release-group/{rg_id}",
                        ))

                        # Add to our known set for this session
                        known_set.add(rg_id)

                # Update the main dict with the new set for this artist
                known_releases[artist_id] = list(known_set)

                # Small delay to respect API rate limits
                time.sleep(1.1)

            # Save the updated known_releases file
            self.log_message.emit("Updating known releases file...")
            try:
                with open(known_releases_path, 'w', encoding='utf-8') as f:
                    json.dump(known_releases, f, indent=4)
            except IOError as e:
                self.log_message.emit(f"Failed to save {KNOWN_RELEASES_FILE}: {e}")

            # Sort new releases by date
            new_releases_found.sort(key=lambda r: r.date, reverse=True)
            self.releases_found.emit(new_releases_found)

        except musicbrainzngs.WebServiceError as e:
            self.log_message.emit(f"MusicBrainz API error: {e}")
            log.error(f"MusicBrainz API error: {e}", exc_info=True)
            self.auth_error.emit(f"MusicBrainz API Error: {e}")
        except Exception as e:
            self.log_message.emit(f"An unknown error occurred fetching releases: {e}")
            log.error(f"Release fetching error: {e}", exc_info=True)
            self.auth_error.emit(f"Error fetching releases: {e}")

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles the incoming OAuth redirect from MusicBrainz."""

    def __init__(self, worker: MusicBrainzWorker, *args):
        self.worker = worker
        # BaseHTTPRequestHandler is an old-style class
        BaseHTTPRequestHandler.__init__(self, *args)

    def do_GET(self):
        """Handle the GET request."""
        try:
            parsed_path = urlparse(self.path)

            if parsed_path.path == '/oauth_callback':
                query_params = parse_qs(parsed_path.query)
                if 'code' in query_params:
                    self.worker.auth_code = query_params['code'][0]
                    self.send_response(200)
                    self.send_header('Content-type', 'text/html')
                    self.end_headers()
                    self.wfile.write(b"<h1>Login Successful!</h1>")
                    self.wfile.write(b"<p>You can close this browser window and return to MusicWatcher.</p>")
                elif 'error' in query_params:
                    error = query_params.get('error_description', ['Unknown error'])[0]
                    self.worker.auth_error_message = f"OAuth error: {error}"
                    self.send_response(400)
                    self.send_header('Content-type', 'text/html')
                    self.end_headers()
                    self.wfile.write(f"<h1>Login Failed</h1><p>{error}</p>".encode('utf-8'))
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Invalid request.")
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found.")

        except Exception as e:
            log.error(f"Error in callback handler: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Internal server error.")

        finally:
            # Signal the worker thread that we're done
            self.worker.auth_code_received.set()

    def log_message(self, format, *args):
        """Suppress default logging to stdout."""
        log.debug(f"Local HTTP Server: {format % args}")


class ExternalProgramManager:
    """Handles detection and launching of external P2P programs."""
    def __init__(self, config: ConfigManager):
        self.config = config
        self.program_cmd: Optional[List[str]] = None
        self.program_name: Optional[str] = None
        self.program_id: Optional[str] = None # e.g., 'nicotine', 'soulseekqt'
        self.detect_program()

    def scan_for_clients(self) -> List[Dict[str, Any]]:
        """Scans for all potential P2P clients."""
        log.info("Scanning for potential P2P clients...")
        clients: List[Dict[str, Any]] = []

        # 1. Scan PATH (Linux/macOS)
        if not sys.platform == "win32":
            for prog_id, prog_name in [("nicotine", "Nicotine+"), ("soulseek-qt", "Soulseek")]:
                path_str = shutil.which(prog_id)
                if path_str:
                    log.info(f"Found {prog_name} on host at {path_str}")
                    clients.append({
                        "name": f"{prog_name} (PATH)",
                        "cmd": [path_str], # Use the full path
                        "id": prog_id
                    })

        # 2. Scan Flatpak (Linux)
        if sys.platform.startswith("linux") and shutil.which("flatpak"):
            log.debug("Scanning for Flatpaks...")
            try:
                # Use flatpak-spawn to scan *outside* the sandbox if we're in one
                cmd = ["flatpak", "list", "--app", "--columns=application"]
                if "FLATPAK_ID" in os.environ:
                    cmd.insert(0, "flatpak-spawn")
                    cmd.insert(1, "--host")

                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True, check=True, timeout=5
                )
                installed_flatpaks = result.stdout.strip().split('\n')

                flatpak_map = {
                    "org.nicotine_plus.Nicotine": ("Nicotine+ (Flatpak)", "nicotine-flatpak"),
                    "org.soulseek.SoulseekQt": ("Soulseek (Flatpak)", "soulseekqt-flatpak")
                }

                for app_id, (name, internal_id) in flatpak_map.items():
                    if app_id in installed_flatpaks:
                        log.info(f"Found Flatpak: {name}")
                        clients.append({
                            "name": name,
                            "cmd": ["flatpak", "run", app_id], # This command works inside/outside sandbox
                            "id": internal_id
                        })
            except Exception as e:
                log.error(f"Failed to scan Flatpak apps: {e}")

        # 3. Scan Windows common paths
        if sys.platform == "win32":
            possible_paths = [
                Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "SoulseekQt" / "SoulseekQt.exe",
                Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "SoulseekQt" / "SoulseekQt.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "SoulseekQt" / "SoulseekQt.exe"
            ]
            for path in possible_paths:
                if path.exists():
                    clients.append({
                        "name": "Soulseek (Installed)",
                        "cmd": [str(path)],
                        "id": "soulseekqt"
                    })
                    log.info(f"Found Windows install at {path}")
                    break # Found it

        log.info(f"Found {len(clients)} potential clients.")
        return clients

    def detect_program(self):
        """Detects Soulseek or Nicotine+, or uses manual path."""
        log.info("Detecting P2P clients...")
        self.program_cmd = None
        self.program_name = None
        self.program_id = None

        # 1. Check for manual path first
        manual_cmd = self.config.get("p2p_manual_cmd")
        if manual_cmd and isinstance(manual_cmd, list):
            cmd_path = manual_cmd[0]
            # Check if it's a file path or a command in PATH
            if Path(cmd_path).is_file() or shutil.which(cmd_path):
                self.program_cmd = manual_cmd # Store the full command
                self.program_name = Path(cmd_path).stem
                log.info(f"Using manually set P2P client: {' '.join(self.program_cmd)}")
            else:
                log.warning(f"Manual P2P command/path not found on host: {cmd_path}")
                self.config.set("p2p_manual_cmd", None) # Clear invalid path
                self.config.save_config()

            # Try to guess ID for auto-search even if manual
            if self.program_name and "nicotine" in self.program_name.lower():
                self.program_id = "nicotine"
            elif self.program_name and ("soulseek" in self.program_name.lower() or "slsk" in self.program_name.lower()):
                self.program_id = "soulseekqt"
            else:
                 self.program_id = "manual" # Default
            return

        # 2. If no manual path, try auto-detection from scan
        potential_clients = self.scan_for_clients()
        if potential_clients:
            # Use the first one as the default
            default_client = potential_clients[0]
            self.program_cmd = default_client["cmd"]
            self.program_name = default_client["name"]
            self.program_id = default_client["id"]
            log.info(f"Auto-detected P2P client: {self.program_name}")
            return

        log.info("No supported P2P client found.")

    def is_available(self) -> bool:
        """Returns True if a program was found."""
        return self.program_cmd is not None

    def launch(self, search_query: Optional[str] = None):
        """
        Launches the detected program.
        If a search_query is provided, attempts to auto-search.
        """
        if not self.is_available() or not self.program_cmd:
            log.error("Launch called but no P2P client is available.")
            return

        cmd = self.program_cmd.copy() # Start with base command

        # Check for auto-search
        if search_query and self.config.get("p2p_auto_search"):
            # Check for nicotine (path) or nicotine (flatpak)
            if self.program_id in ["nicotine", "nicotine-flatpak"]:
                log.info(f"Auto-searching {self.program_name} for: {search_query}")
                cmd.extend(["--search", search_query])
            else:
                log.info(f"Auto-search not supported for {self.program_name}. Launching client.")

        try:
            log.info(f"Executing command: {' '.join(cmd)}")

            # Use Popen for non-blocking launch
            # If we are in a Flatpak, we MUST use flatpak-spawn to run a host command
            if "FLATPAK_ID" in os.environ and not cmd[0] == "flatpak":
                log.info("Running as Flatpak, using flatpak-spawn to launch host command.")
                cmd.insert(0, "flatpak-spawn")
                cmd.insert(1, "--host")

            subprocess.Popen(cmd)
        except Exception as e:
            log.error(f"Failed to launch {self.program_name}: {e}")

# === GUI Components ===

class CredentialsDialog(QDialog):
    """Dialog to enter MusicBrainz Client ID and Secret."""
    def __init__(self, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("MusicBrainz Credentials")

        self.layout = QFormLayout(self)

        self.info_label = QLabel(
            "Enter the credentials for your MusicBrainz application.<br>"
            f"Register one at: <b>https://musicbrainz.org/account/applications</b><br>"
            f"Use Redirect URI: <b>{MB_REDIRECT_URI}</b>"
        )
        self.info_label.setOpenExternalLinks(True)
        self.layout.addRow(self.info_label)

        self.client_id_edit = QLineEdit(self)
        self.client_id_edit.setText(self.config.get("mb_client_id"))
        self.layout.addRow("Client ID:", self.client_id_edit)

        self.client_secret_edit = QLineEdit(self)
        self.client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret_edit.setText(self.config.get("mb_client_secret"))
        self.layout.addRow("Client Secret:", self.client_secret_edit)

        # Button Box
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.save_and_accept)
        self.button_box.rejected.connect(self.reject)
        self.layout.addRow(self.button_box)

    def save_and_accept(self):
        """Save credentials to config and close."""
        self.config.set("mb_client_id", self.client_id_edit.text().strip())
        self.config.set("mb_client_secret", self.client_secret_edit.text().strip())
        # We don't save config here, main window saves on close or action
        log.info("Set MusicBrainz credentials (will be saved on exit).")
        self.accept()


class SelectP2PDialog(QDialog):
    """Dialog to select a P2P client from a scanned list or manually."""
    def __init__(self, p2p_manager: ExternalProgramManager, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.p2p_manager = p2p_manager
        self.config = config
        self.setWindowTitle("Select P2P Client")
        self.setMinimumWidth(500)

        self.layout = QVBoxLayout(self)

        self.info_label = QLabel("Select a detected client or browse for one:")
        self.layout.addWidget(self.info_label)

        self.client_list = QListWidget(self)
        self.client_list.itemDoubleClicked.connect(self.save_and_accept)
        self.layout.addWidget(self.client_list)

        self.populate_list()

        self.browse_button = QPushButton("Browse for file (.sh, .exe, ...)", self)
        self.browse_button.clicked.connect(self.browse_manual)
        self.layout.addWidget(self.browse_button)

        # Button Box
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.save_and_accept)
        self.button_box.rejected.connect(self.reject)
        self.layout.addWidget(self.button_box)

    def populate_list(self):
        """Scans for clients and fills the list."""
        self.client_list.clear()
        scanned_clients = self.p2p_manager.scan_for_clients()

        if not scanned_clients:
            self.client_list.addItem("No auto-detected clients found.")
            self.client_list.setEnabled(False)

        for client in scanned_clients:
            item = QListWidgetItem(client["name"])
            item.setData(Qt.ItemDataRole.UserRole, client)
            self.client_list.addItem(item)

        # Check for current manual path
        manual_cmd_list = self.config.get("p2p_manual_cmd")
        manual_cmd_str = " ".join(manual_cmd_list) if manual_cmd_list else ""

        current_cmd_str = " ".join(self.p2p_manager.program_cmd) if self.p2p_manager.program_cmd else ""

        found_current = False
        for i in range(self.client_list.count()):
            item = self.client_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data and " ".join(data["cmd"]) == current_cmd_str:
                item.setSelected(True)
                self.client_list.setCurrentItem(item)
                found_current = True
                break

        if not found_current and manual_cmd_list:
            item = QListWidgetItem(f"Manual: {manual_cmd_str}")
            item.setData(Qt.ItemDataRole.UserRole, {"cmd": manual_cmd_list, "name": "Manual", "id": "manual"})
            self.client_list.addItem(item)
            item.setSelected(True)
            self.client_list.setCurrentItem(item) # Select current
        elif not found_current and self.client_list.count() > 0:
            self.client_list.setCurrentRow(0) # Select first item

    def browse_manual(self):
        """Open file dialog to select a manual executable."""
        file_path, _ = QFileDialog.getOpenFileName(self, "Select P2P Client Executable")
        if file_path:
            # Add this new option to the list and select it
            cmd = [file_path]
            item = QListWidgetItem(f"Manual: {file_path}")
            item.setData(Qt.ItemDataRole.UserRole, {"cmd": cmd, "name": "Manual", "id": "manual"})

            # Remove old manual entry if it exists
            for i in range(self.client_list.count()):
                if self.client_list.item(i).text().startswith("Manual:"):
                    self.client_list.takeItem(i)
                    break

            self.client_list.addItem(item)
            item.setSelected(True)
            self.client_list.setCurrentItem(item)
            self.client_list.setEnabled(True)

    def save_and_accept(self):
        """Save the selected client to config."""
        selected_item = self.client_list.currentItem()
        if not selected_item:
            self.reject()
            return

        data = selected_item.data(Qt.ItemDataRole.UserRole)
        if not data or "cmd" not in data:
            log.warning("No data in selected P2P client item.")
            self.reject()
            return

        self.config.set("p2p_manual_cmd", data["cmd"])
        # We let the main window save the config
        log.info(f"Set manual P2P client to: {data['cmd']}")
        self.accept()

# === Main Window ===

class MusicWatcher(QMainWindow):
    """Main application window."""

    # Signals for cross-thread operations
    start_artist_id_search = pyqtSignal(set)
    start_release_fetch = pyqtSignal(list)
    start_mb_auth = pyqtSignal()

    def __init__(self):
        super().__init__()

        log.info("Initializing main window...")
        self.config = ConfigManager(CONFIG_FILE)
        self.p2p_manager = ExternalProgramManager(self.config)
        self.all_files_data: Dict[str, AudioFile] = {} # {path_str: AudioFile}
        self.artist_album_map: Dict[str, Dict[str, List[AudioFile]]] = {}
        self.artist_id_map: Dict[str, str] = {} # {artist_name: artist_id}

        # Caches for tree items
        self.artist_tree_items: Dict[str, QTreeWidgetItem] = {}
        self.album_tree_items: Dict[str, QTreeWidgetItem] = {} # key is "artist|album"
        self.file_tree_items: Dict[str, QTreeWidgetItem] = {} # key is path_str

        # --- Worker Threads ---
        self.scanner_thread: Optional[QThread] = None
        self.scanner_worker: Optional[FileScanner] = None

        self.lyrics_fetcher_thread: Optional[QThread] = None
        self.lyrics_fetcher_worker: Optional[LyricFetcher] = None

        self.id_search_thread: Optional[QThread] = None # For parallel artist search

        # MusicBrainz worker runs in a persistent thread
        self.mb_thread = QThread()
        self.mb_thread.setObjectName("MusicBrainzThread")
        self.mb_worker = MusicBrainzWorker(self.config)
        self.mb_worker.moveToThread(self.mb_thread)

        # Connect MB worker signals
        self.mb_worker.log_message.connect(self.log_to_panel)
        self.mb_worker.thread_log_message.connect(self.log_to_panel, Qt.ConnectionType.QueuedConnection)
        self.mb_worker.auth_success.connect(self.on_auth_success, Qt.ConnectionType.QueuedConnection)
        self.mb_worker.auth_error.connect(self.on_auth_error, Qt.ConnectionType.QueuedConnection)
        self.mb_worker.artist_search_finished.connect(self.on_artist_search_finished, Qt.ConnectionType.QueuedConnection)
        self.mb_worker.releases_found.connect(self.on_releases_found, Qt.ConnectionType.QueuedConnection)

        # Connect signals to start MB worker jobs
        self.start_artist_id_search.connect(self.mb_worker.search_artist_ids)
        self.start_release_fetch.connect(self.mb_worker.fetch_new_releases)
        self.start_mb_auth.connect(self.mb_worker.start_authentication)

        self.mb_thread.start()

        # --- Init UI ---
        self._apply_dark_mode()
        self.init_ui()
        self.check_music_paths()

        # Restore window geometry
        geom = self.config.get("window_geometry")
        if geom:
            try:
                self.restoreGeometry(bytes.fromhex(geom))
            except Exception as e:
                log.warning(f"Could not restore window geometry: {e}")

    def check_music_paths(self):
        """Checks if music paths are set, prompts user if not."""
        music_paths = self.config.get("music_directories")
        if not music_paths:
            self.log_to_panel("No music paths set.")
            QMessageBox.information(self, "Music Paths",
                "No music directories are set. Please add one in the 'Settings' tab to begin.")
            self.tabs.setCurrentIndex(1) # Switch to settings tab
            self.scan_button.setEnabled(False)
            self.resume_scan_button.setEnabled(False)
        else:
            self.scan_button.setEnabled(True)
            # Check for saved scan state
            scan_state = self.config.get("last_scan_state", {})
            if scan_state:
                # Check if the state is relevant to current dirs
                relevant_state = any(key in music_paths for key in scan_state.keys() if scan_state.get(key, 0) > 0) # Only consider non-zero states
                if relevant_state:
                    self.resume_scan_button.setEnabled(True)
                    # We can't easily get total counts here, just show "Resume"
                    self.resume_scan_button.setText("Resume Scan")
                else:
                    self.resume_scan_button.setEnabled(False)
            else:
                self.resume_scan_button.setEnabled(False)

    def init_ui(self):
        """Initialize widgets and layouts."""
        self.setWindowTitle(APP_NAME)
        self.setGeometry(100, 100, 1400, 800) # Increased default width

        # --- Main Splitter ---
        main_splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # --- Left Panel (Tabbed) ---
        self.tabs = QTabWidget()
        self.tab_scan_fetch = QWidget()
        self.tab_settings = QWidget()

        self.tabs.addTab(self.tab_scan_fetch, "Scan & Fetch")
        self.tabs.addTab(self.tab_settings, "Settings")
        self.tabs.setMinimumWidth(300)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        # Add tooltip to settings tab
        self.tabs.setTabToolTip(1, "Changes made here might require a scan restart.")


        # --- Tab 1: Scan & Fetch ---
        left_layout = QVBoxLayout(self.tab_scan_fetch)

        # Scan Controls
        self.scan_button = QPushButton(QIcon.fromTheme("media-playback-start"), "Start Scan")
        self.scan_button.clicked.connect(lambda: self.start_scan(resume=False))

        self.resume_scan_button = QPushButton(QIcon.fromTheme("media-seek-forward"), "Resume Scan")
        self.resume_scan_button.clicked.connect(lambda: self.start_scan(resume=True))
        self.resume_scan_button.setEnabled(False)

        self.stop_scan_button = QPushButton(QIcon.fromTheme("media-playback-stop"), "Stop Scan")
        self.stop_scan_button.clicked.connect(self.stop_scan)
        self.stop_scan_button.setEnabled(False)

        scan_buttons_layout = QHBoxLayout()
        scan_buttons_layout.addWidget(self.scan_button)
        scan_buttons_layout.addWidget(self.resume_scan_button)
        scan_buttons_layout.addWidget(self.stop_scan_button)

        self.scan_progress_label = QLabel("Scan Progress", self)
        self.scan_progress_bar = QProgressBar(self)
        self.scan_progress_bar.setValue(0)
        self.scan_progress_bar.setTextVisible(True)
        self.scan_count_label = QLabel("0 of 0 files scanned", self)
        self.scan_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # MusicBrainz Controls
        self.login_button = QPushButton("Login to MusicBrainz", self)
        self.login_button.clicked.connect(self.on_login_button_clicked)

        self.set_creds_button = QPushButton("Set MB Credentials...", self)
        self.set_creds_button.clicked.connect(self.show_credentials_dialog)

        self.fetch_releases_button = QPushButton("Fetch New Releases", self)
        self.fetch_releases_button.clicked.connect(self.fetch_new_releases)
        self.fetch_releases_button.setEnabled(False)

        self.fetch_lyrics_button = QPushButton("Fetch Missing Lyrics", self)
        self.fetch_lyrics_button.clicked.connect(self.start_lyric_fetch)
        self.fetch_lyrics_button.setEnabled(False)

        mb_layout = QHBoxLayout()
        mb_layout.addWidget(self.login_button)
        mb_layout.addWidget(self.set_creds_button)

        # P2P Controls
        self.launch_p2p_button = QPushButton(f"Launch {self.p2p_manager.program_name or 'P2P Client'}")
        self.launch_p2p_button.setEnabled(self.p2p_manager.is_available())
        if not self.p2p_manager.is_available():
            self.launch_p2p_button.setText("No P2P Client Found")
        self.launch_p2p_button.clicked.connect(lambda: self.p2p_manager.launch())

        self.set_p2p_path_button = QPushButton("Set P2P Client...", self)
        self.set_p2p_path_button.clicked.connect(self.show_select_p2p_dialog)

        p2p_layout = QHBoxLayout()
        p2p_layout.addWidget(self.launch_p2p_button)
        p2p_layout.addWidget(self.set_p2p_path_button)

        left_layout.addLayout(scan_buttons_layout)
        left_layout.addWidget(self.scan_progress_label)
        left_layout.addWidget(self.scan_progress_bar)
        left_layout.addWidget(self.scan_count_label)
        left_layout.addSpacing(20)
        left_layout.addWidget(QLabel("--- MusicBrainz ---"))
        left_layout.addLayout(mb_layout)
        left_layout.addWidget(self.fetch_releases_button)
        left_layout.addWidget(self.fetch_lyrics_button)
        left_layout.addSpacing(20)
        left_layout.addWidget(QLabel("--- External ---"))
        left_layout.addLayout(p2p_layout)

        left_layout.addStretch() # Push controls to top

        # --- Tab 2: Settings ---
        settings_layout = QVBoxLayout(self.tab_settings)

        # Music Paths
        settings_layout.addWidget(QLabel("Music Directories:"))
        self.music_paths_list = QListWidget(self)
        self.music_paths_list.addItems(self.config.get("music_directories"))
        settings_layout.addWidget(self.music_paths_list)

        path_buttons_layout = QHBoxLayout()
        self.add_path_button = QPushButton(QIcon.fromTheme("list-add"), "Add Folder...")
        self.add_path_button.clicked.connect(self.add_music_path)
        self.remove_path_button = QPushButton(QIcon.fromTheme("list-remove"), "Remove Selected")
        self.remove_path_button.clicked.connect(self.remove_music_path)
        path_buttons_layout.addWidget(self.add_path_button)
        path_buttons_layout.addWidget(self.remove_path_button)
        settings_layout.addLayout(path_buttons_layout)

        settings_layout.addSpacing(20)

        # Toggles
        self.skip_synced_check = QCheckBox("Skip lyrics fetch if synced (.lrc) file exists")
        self.skip_synced_check.setChecked(self.config.get("skip_synced_lyrics"))
        self.skip_synced_check.toggled.connect(self.on_setting_changed)
        settings_layout.addWidget(self.skip_synced_check)

        self.p2p_auto_search_check = QCheckBox("Auto-search P2P client for new releases")
        self.p2p_auto_search_check.setChecked(self.config.get("p2p_auto_search"))
        self.p2p_auto_search_check.toggled.connect(self.on_setting_changed)
        settings_layout.addWidget(self.p2p_auto_search_check)

        settings_layout.addStretch()

        # --- Right Panel (File List & Log) ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        # Splitter for Library / (Releases + Log)
        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setHandleWidth(10)

        # --- Library Tree Panel (Top Right) ---
        library_panel = QWidget()
        library_layout = QVBoxLayout(library_panel)
        library_layout.setContentsMargins(0, 0, 0, 0)

        # Tree controls (Expand/Collapse)
        tree_controls_widget = QWidget()
        tree_controls_layout = QHBoxLayout(tree_controls_widget)
        tree_controls_layout.setContentsMargins(2,2,2,2) # Add small margins

        # --- NEW BUTTONS ---
        self.expand_all_button = QPushButton("Expand All")
        self.expand_all_button.clicked.connect(self.expand_all)
        self.expand_artists_button = QPushButton("Expand Artists")
        self.expand_artists_button.clicked.connect(self.expand_artists)
        self.expand_albums_button = QPushButton("Expand Albums")
        self.expand_albums_button.clicked.connect(self.expand_albums)

        self.collapse_all_button = QPushButton("Collapse All")
        self.collapse_all_button.clicked.connect(self.collapse_all)
        self.collapse_artists_button = QPushButton("Collapse Artists")
        self.collapse_artists_button.clicked.connect(self.collapse_artists)
        self.collapse_albums_button = QPushButton("Collapse Albums")
        self.collapse_albums_button.clicked.connect(self.collapse_albums)

        expand_group_layout = QHBoxLayout()
        expand_group_layout.addWidget(self.expand_all_button)
        expand_group_layout.addWidget(self.expand_artists_button)
        expand_group_layout.addWidget(self.expand_albums_button)

        collapse_group_layout = QHBoxLayout()
        collapse_group_layout.addWidget(self.collapse_all_button)
        collapse_group_layout.addWidget(self.collapse_artists_button)
        collapse_group_layout.addWidget(self.collapse_albums_button)

        tree_controls_layout.addLayout(expand_group_layout)
        tree_controls_layout.addStretch()
        tree_controls_layout.addLayout(collapse_group_layout)
        # --- END NEW BUTTONS ---

        library_layout.addWidget(tree_controls_widget) # Add buttons widget

        # The tree itself
        self.library_tree = QTreeWidget(self)
        self.library_tree.setFont(QFont("Monospace", 9))
        # --- ADDED File Path Column ---
        self.library_tree.setHeaderLabels(["Artist / Album / Track", "Status", "Checksum", "Lyrics", "File Path"])
        self.library_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch) # Main item stretches
        self.library_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents) # Status
        self.library_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents) # Checksum
        self.library_tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents) # Lyrics
        self.library_tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch) # File Path stretches
        # --- END File Path Column ---
        self.library_tree.setSortingEnabled(True)
        self.library_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.library_tree.customContextMenuRequested.connect(self.show_tree_context_menu)

        library_layout.addWidget(self.library_tree)
        library_panel.setLayout(library_layout)
        right_splitter.addWidget(library_panel)

        # --- Bottom Right Tabbed Panel (Releases & Log) ---
        bottom_right_tabs = QTabWidget()

        # New Releases Tab
        self.releases_tree = QTreeWidget()
        self.releases_tree.setHeaderLabels(["Date", "Artist", "New Release"])
        self.releases_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.releases_tree.setSortingEnabled(True)
        self.releases_tree.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.releases_tree.itemDoubleClicked.connect(self.on_release_double_clicked)

        # Log Output Tab
        self.log_output = QTextEdit(self)
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Monospace", 9))

        bottom_right_tabs.addTab(self.releases_tree, "New Releases")
        bottom_right_tabs.addTab(self.log_output, "Log")

        right_splitter.addWidget(bottom_right_tabs)
        right_splitter.setSizes([600, 200]) # 75% / 25% split

        right_layout.addWidget(right_splitter)
        right_panel.setLayout(right_layout)

        # Add panels to main splitter
        main_splitter.addWidget(self.tabs)
        main_splitter.addWidget(right_panel)
        main_splitter.setSizes([350, 1050]) # Adjusted initial size split
        main_splitter.setHandleWidth(10)

        self.setCentralWidget(main_splitter)

        # Update MB login button state
        if self.config.get("mb_access_token"):
            self.on_auth_success() # Call directly to set initial state
        else:
            self.on_auth_error("Not logged in.") # Call directly

    def _apply_dark_mode(self):
        """Applies a dark color palette to the application."""
        dark_palette = QPalette()

        # Base
        dark_palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Base, QColor(42, 42, 42))
        dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(66, 66, 66))
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.black)
        dark_palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)

        # Buttons
        dark_palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
        dark_palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)

        # Links
        dark_palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))

        # Selection
        dark_palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)

        # Disabled
        dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, Qt.GlobalColor.darkGray)
        dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, Qt.GlobalColor.darkGray)
        dark_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, Qt.GlobalColor.darkGray)

        QApplication.instance().setPalette(dark_palette)

        # Apply style sheet for QProgressBar text color and other tweaks
        self.setStyleSheet("""
            QMainWindow { background-color: #353535; } /* Match window color */
            QProgressBar::chunk {
                background-color: #2a82da; /* Blue chunk */
            }
            QProgressBar {
                color: white; /* White text */
                text-align: center;
                border: 1px solid grey;
                border-radius: 5px;
                background-color: #2a2a2a; /* Darker background */
            }
            QHeaderView::section {
                background-color: #424242; /* Darker header */
                color: white;
                padding: 4px;
                border: 1px solid #555555;
            }
            QTreeWidget {
                alternate-background-color: #424242;
                background: #2a2a2a;
                border: 1px solid #555555; /* Add border */
            }
            QTreeWidget::item:selected {
                background-color: #2a82da; /* Match highlight color */
                color: black;
            }
            QTabWidget::pane {
                border-top: 1px solid #555555; /* Adjust border */
            }
            QTabBar::tab {
                background: #353535;
                color: white;
                padding: 8px 12px; /* Adjust padding */
                border: 1px solid #555555;
                border-bottom: none; /* Remove bottom border */
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #4a4a4a; /* Slightly lighter selected */
            }
            QTabBar::tab:hover {
                background: #555555;
            }
            QSplitter::handle {
                background-color: #555555; /* Make handle visible */
                height: 3px; /* Horizontal splitter handle height */
                width: 3px; /* Vertical splitter handle width */
            }
            QSplitter::handle:hover {
                background-color: #666666;
            }
            QPushButton {
                background-color: #555555;
                border: 1px solid #666666;
                padding: 5px 10px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #666666;
            }
            QPushButton:pressed {
                background-color: #4a4a4a;
            }
            QPushButton:disabled {
                background-color: #404040;
                color: #777777;
            }
            QTextEdit, QListWidget, QLineEdit {
                background-color: #2a2a2a;
                border: 1px solid #555555;
                color: white;
            }
            QListWidget::item:selected {
                background-color: #2a82da;
                color: black;
            }
            QCheckBox { padding: 3px; }
            QCheckBox::indicator { width: 13px; height: 13px; }
            QLabel { padding: 2px; }
        """)

    # --- UI Slots & Actions ---

    def log_to_panel(self, message: str):
        """Thread-safe way to append to the log output."""
        if threading.current_thread() != threading.main_thread():
            log.warning(f"log_to_panel called from wrong thread: {message}")
            # Re-invoke via signal if needed, but rely on QueuedConnection for now
            return

        log.info(message) # Also log to file
        self.log_output.append(message)
        self.log_output.verticalScrollBar().setValue(
            self.log_output.verticalScrollBar().maximum()
        )

    # --- NEW Expand/Collapse Methods (Safe Iteration) ---
    def expand_all(self):
        """Expands all artists and albums."""
        self.library_tree.expandAll()

    def expand_artists(self):
        """Expands all top-level artist items."""
        artist_items = [self.library_tree.topLevelItem(i) for i in range(self.library_tree.topLevelItemCount())]
        for item in artist_items:
            item.setExpanded(True)

    def expand_albums(self):
        """Expands all album items (second level)."""
        album_items = []
        artist_items = [self.library_tree.topLevelItem(i) for i in range(self.library_tree.topLevelItemCount())]
        for artist_item in artist_items:
            for i in range(artist_item.childCount()):
                album_items.append(artist_item.child(i))

        for item in album_items:
            item.setExpanded(True)
            # Also ensure the parent artist is expanded
            if item.parent() and not item.parent().isExpanded():
                item.parent().setExpanded(True)

    def collapse_all(self):
        """Collapses all items in the library tree."""
        self.library_tree.collapseAll()

    def collapse_artists(self):
        """Collapses all top-level artist items."""
        artist_items = [self.library_tree.topLevelItem(i) for i in range(self.library_tree.topLevelItemCount())]
        for item in artist_items:
            item.setExpanded(False)

    def collapse_albums(self):
        """Collapses all album items (second level)."""
        album_items = []
        artist_items = [self.library_tree.topLevelItem(i) for i in range(self.library_tree.topLevelItemCount())]
        for artist_item in artist_items:
            for i in range(artist_item.childCount()):
                album_items.append(artist_item.child(i))

        for item in album_items:
            item.setExpanded(False)
            # Don't collapse the artist here, only the albums
    # --- END NEW Expand/Collapse ---


    def show_tree_context_menu(self, pos):
        """Show context menu for the library tree."""
        item = self.library_tree.itemAt(pos)
        if not item:
            return

        menu = QMenu(self)
        level = 0
        temp_item = item
        while temp_item.parent() is not None:
            level += 1
            temp_item = temp_item.parent()

        if level == 0: # Artist level
            action = menu.addAction(f"Search P2P for Artist: {item.text(0)}")
            action.triggered.connect(lambda: self.p2p_search_artist(item))
        elif level == 1: # Album level
            action = menu.addAction(f"Search P2P for Album: {item.text(0)}")
            action.triggered.connect(lambda: self.p2p_search_album(item))
        else: # File level (level == 2)
            action = menu.addAction(f"Search P2P for Track: {item.text(0)}")
            action.triggered.connect(lambda: self.p2p_search_track(item))

            # Add action to open file location
            open_action = menu.addAction("Open File Location")
            open_action.triggered.connect(lambda: self.open_file_location(item))

        menu.exec(self.library_tree.mapToGlobal(pos))

    def open_file_location(self, item: QTreeWidgetItem):
        """Opens the directory containing the selected file."""
        audio_file: AudioFile = item.data(0, Qt.ItemDataRole.UserRole)
        if audio_file and audio_file.path:
            dir_path = str(audio_file.path.parent)
            try:
                if sys.platform == "win32":
                    os.startfile(dir_path)
                elif sys.platform == "darwin": # macOS
                    subprocess.Popen(["open", dir_path])
                else: # Linux
                    subprocess.Popen(["xdg-open", dir_path])
                self.log_to_panel(f"Opened directory: {dir_path}")
            except Exception as e:
                self.log_to_panel(f"Failed to open directory {dir_path}: {e}")
                QMessageBox.warning(self, "Error", f"Could not open directory:\n{e}")

    def p2p_search_artist(self, item: QTreeWidgetItem):
        artist_name = item.text(0)
        self.log_to_panel(f"P2P Search (Artist): {artist_name}")
        self.p2p_manager.launch(search_query=artist_name)

    def p2p_search_album(self, item: QTreeWidgetItem):
        artist_name = item.parent().text(0)
        album_name = item.text(0)
        query = f"{artist_name} {album_name}"
        self.log_to_panel(f"P2P Search (Album): {query}")
        self.p2p_manager.launch(search_query=query)

    def p2p_search_track(self, item: QTreeWidgetItem):
        audio_file: AudioFile = item.data(0, Qt.ItemDataRole.UserRole)
        if audio_file and audio_file.artist and audio_file.title:
            query = f"{audio_file.artist} {audio_file.title}"
            self.log_to_panel(f"P2P Search (Track): {query}")
            self.p2p_manager.launch(search_query=query)
        else:
            self.log_to_panel(f"Could not P2P search for track: Missing tags.")

    # --- Settings Tab ---

    def on_setting_changed(self):
        """Slot to save any changed setting."""
        log.debug("Settings changed, updating config...")
        self.config.set("skip_synced_lyrics", self.skip_synced_check.isChecked())
        self.config.set("p2p_auto_search", self.p2p_auto_search_check.isChecked())
        # No need to save config immediately, saved on exit

    def add_music_path(self):
        """Shows dialog to add a new music directory."""
        # Default to user's Music directory
        default_dir = str(Path.home() / "Music")

        dir_path = QFileDialog.getExistingDirectory(
            self, "Select Music Directory",
            default_dir
        )
        if dir_path:
            current_paths = self.config.get("music_directories", [])
            if dir_path not in current_paths:
                self.music_paths_list.addItem(dir_path)
                current_paths.append(dir_path)
                self.config.set("music_directories", current_paths)
                # Config saved on exit
                self.log_to_panel(f"Added music directory: {dir_path}")
                self.check_music_paths() # Re-enable scan buttons
            else:
                self.log_to_panel(f"Directory already in list: {dir_path}")

    def remove_music_path(self):
        """Removes the selected music directory."""
        selected_items = self.music_paths_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        dir_path = item.text()

        reply = QMessageBox.question(self, "Remove Directory",
            f"Are you sure you want to remove this directory?\n\n{dir_path}\n\n(This will not delete any files.)")

        if reply == QMessageBox.StandardButton.Yes:
            self.music_paths_list.takeItem(self.music_paths_list.row(item))
            current_paths = self.config.get("music_directories", [])
            if dir_path in current_paths:
                current_paths.remove(dir_path)
                self.config.set("music_directories", current_paths)

            # Also remove from scan state
            scan_state = self.config.get("last_scan_state", {})
            if dir_path in scan_state:
                scan_state.pop(dir_path)
                self.config.set("last_scan_state", scan_state)

            # Config saved on exit
            self.log_to_panel(f"Removed music directory: {dir_path}")
            self.check_music_paths() # May disable scan buttons

    # --- Scan & Fetch Tab ---

    def on_login_button_clicked(self):
        """Handles click, checking for credentials first."""
        client_id = self.config.get("mb_client_id")
        client_secret = self.config.get("mb_client_secret")

        if not client_id or not client_secret:
            self.log_to_panel("Client ID or Secret not set.")
            QMessageBox.warning(self, "Missing Credentials",
                "Please set your MusicBrainz Client ID and Secret before logging in.")
            self.show_credentials_dialog()
        else:
            self.start_mb_auth.emit()
            self.login_button.setEnabled(False)
            self.login_button.setText("Logging in...")

    def show_credentials_dialog(self):
        """Shows the dialog to set MB credentials."""
        dialog = CredentialsDialog(self.config, self)
        dialog.exec()
        # No need to save config, main window saves on close.
        # But we can update the config object for the worker
        self.mb_worker.config = self.config

    def show_select_p2p_dialog(self):
        """Shows the dialog to select a P2P client."""
        dialog = SelectP2PDialog(self.p2p_manager, self.config, self)
        if dialog.exec():
            # Re-detect program with new settings
            self.p2p_manager.detect_program()
            self.launch_p2p_button.setEnabled(self.p2p_manager.is_available())
            self.launch_p2p_button.setText(f"Launch {self.p2p_manager.program_name or 'P2P Client'}")
            # Config saved on exit
            self.log_to_panel(f"P2P client set to: {self.p2p_manager.program_name}")

    def start_scan(self, resume: bool):
        """Starts the file scanning worker thread."""
        if self.scanner_worker:
            self.log_to_panel("Scan is already running.")
            return

        music_paths = self.config.get("music_directories")
        if not music_paths:
            self.log_to_panel("No music directories set. Cannot scan.")
            return

        # --- Clear UI (if not resuming) ---
        if not resume:
            self.log_to_panel("Starting new scan...")
            self.library_tree.clear()
            self.all_files_data.clear()
            self.artist_album_map.clear()
            self.artist_id_map.clear()
            self.artist_tree_items.clear()
            self.album_tree_items.clear()
            self.file_tree_items.clear()
            self.releases_tree.clear()
            self.fetch_releases_button.setEnabled(False)
            self.fetch_lyrics_button.setEnabled(False)
            scan_state = {} # Start fresh
        else:
            self.log_to_panel("Resuming scan...")
            scan_state = self.config.get("last_scan_state", {})
            # Filter state for only currently configured directories
            scan_state = {k: v for k, v in scan_state.items() if k in music_paths and v > 0} # Only keep relevant, non-zero states

        # --- Configure Worker ---
        self.scanner_thread = QThread()
        self.scanner_thread.setObjectName("FileScannerThread")
        self.scanner_worker = FileScanner(music_paths, scan_state)
        self.scanner_worker.moveToThread(self.scanner_thread)

        # --- Connect Signals ---
        self.scanner_worker.file_found.connect(self.on_file_found)
        self.scanner_worker.scan_progress.connect(self.on_scan_progress)
        self.scanner_worker.scan_finished.connect(self.on_scan_finished, Qt.ConnectionType.QueuedConnection) # Queued for safety
        self.scanner_worker.log_message.connect(self.log_to_panel)

        self.scanner_thread.started.connect(self.scanner_worker.run)
        self.scanner_thread.finished.connect(self.on_scan_thread_finished) # Cleanup

        # --- Update UI State ---
        self.set_scan_buttons_enabled(is_scanning=True)

        # --- Start ---
        self.scanner_thread.start()

    def stop_scan(self):
        """Requests the scanner worker to stop."""
        if self.scanner_worker:
            self.log_to_panel("Stopping scan...")
            self.scanner_worker.stop()
            self.stop_scan_button.setEnabled(False)
            self.stop_scan_button.setText("Stopping...")
        else:
            log.info("Stop requested but no scanner worker found.")

    def set_scan_buttons_enabled(self, is_scanning: bool):
        """Helper to toggle scan button states."""
        self.scan_button.setEnabled(not is_scanning)
        # Only enable resume if there's a valid state and not currently scanning
        has_resume_state = any(v > 0 for v in self.config.get("last_scan_state", {}).values())
        self.resume_scan_button.setEnabled(has_resume_state and not is_scanning)
        self.stop_scan_button.setEnabled(is_scanning)

        # --- Keep Settings tab enabled, but disable list editing ---
        # self.tabs.setTabEnabled(1, not is_scanning) # REMOVED
        self.music_paths_list.setEnabled(not is_scanning)
        self.add_path_button.setEnabled(not is_scanning)
        self.remove_path_button.setEnabled(not is_scanning)

        # Disable fetch buttons while scanning
        can_fetch = not is_scanning and bool(self.all_files_data)
        self.fetch_releases_button.setEnabled(can_fetch)
        self.fetch_lyrics_button.setEnabled(can_fetch)


    @pyqtSlot(AudioFile)
    def on_file_found(self, audio_file: AudioFile):
        """Slot for 'file_found' signal. Updates the UI tree."""
        # Store file data
        path_str = str(audio_file.path)
        self.all_files_data[path_str] = audio_file

        # Build artist/album map for release fetching
        if audio_file.artist != "Unknown Artist":
            self.artist_album_map.setdefault(audio_file.artist, {}) \
                .setdefault(audio_file.album, []).append(audio_file)

        # --- Update Tree ---
        # Get or create Artist node
        artist_name = audio_file.artist
        artist_item = self.artist_tree_items.get(artist_name)
        if not artist_item:
            artist_item = QTreeWidgetItem(self.library_tree, [artist_name])
            artist_item.setFont(0, QFont("Monospace", 9, QFont.Weight.Bold))
            self.artist_tree_items[artist_name] = artist_item

        # Get or create Album node
        album_name = audio_file.album
        album_key = f"{artist_name}|{album_name}" # Use unique key
        album_item = self.album_tree_items.get(album_key)
        if not album_item:
            album_item = QTreeWidgetItem(artist_item, [album_name])
            self.album_tree_items[album_key] = album_item

        # Create or update File node
        file_item = self.file_tree_items.get(path_str)
        if not file_item:
            file_item = QTreeWidgetItem(album_item)
            file_item.setData(0, Qt.ItemDataRole.UserRole, audio_file) # Store data obj
            self.file_tree_items[path_str] = file_item
        else:
            # Update existing item if re-scanning
            file_item.setData(0, Qt.ItemDataRole.UserRole, audio_file)

        # Set text and tooltips
        file_item.setText(0, f"{audio_file.track_num} - {audio_file.title}")
        file_item.setText(1, audio_file.status)
        file_item.setText(2, audio_file.hash[:8] if audio_file.hash else "...") # Show first 8 chars or placeholder
        file_item.setText(3, audio_file.lrc_status)
        # --- ADDED File Path Column Text ---
        file_item.setText(4, path_str)
        # --- END File Path Column Text ---
        file_item.setToolTip(0, f"{audio_file.filename}\n{audio_file.path}")

        # Color based on error
        if audio_file.error_msg:
            file_item.setForeground(0, QColor("red"))
            file_item.setIcon(0, QIcon.fromTheme("dialog-error"))
            file_item.setToolTip(1, audio_file.error_msg) # Tooltip on Status column
        else:
            file_item.setForeground(0, QColor("#aaffaa")) # Light green
            file_item.setIcon(0, QIcon.fromTheme("dialog-ok"))
            file_item.setToolTip(1, "OK") # Clear old tooltip

    @pyqtSlot(int, int, str)
    def on_scan_progress(self, current: int, total: int, message: str):
        """Slot for 'scan_progress' signal."""
        percentage = 0
        if total > 0:
            percentage = int((current / total) * 100)

        self.scan_progress_bar.setValue(percentage)
        self.scan_progress_bar.setFormat(f"{percentage}%")
        self.scan_count_label.setText(message)

    @pyqtSlot(dict)
    def on_scan_finished(self, final_scan_state: dict):
        """Slot for 'scan_finished' signal. Cleans up thread."""
        self.log_to_panel("Scan finished.")

        # Save the final scan state (will be empty if complete)
        self.config.set("last_scan_state", final_scan_state)
        # Config saved on exit or settings change

        if self.scanner_thread:
            self.scanner_thread.quit() # Ask thread to stop

        # Update UI
        scan_completed = not any(v > 0 for v in final_scan_state.values())

        if scan_completed:
            self.scan_progress_bar.setValue(100)
            self.scan_progress_bar.setFormat("Complete")
            self.resume_scan_button.setEnabled(False) # No state to resume
            self.resume_scan_button.setText("Resume Scan")
        else:
            # Scan was stopped early
            self.scan_progress_bar.setFormat("Stopped")
            self.resume_scan_button.setEnabled(True)
            self.resume_scan_button.setText("Resume Scan")

        self.set_scan_buttons_enabled(is_scanning=False)
        self.stop_scan_button.setText("Stop Scan") # Reset text

        # Enable other buttons now that we have data
        if self.all_files_data:
            self.fetch_releases_button.setEnabled(True)
            self.fetch_lyrics_button.setEnabled(True)
            self.log_to_panel(f"Total files in memory: {len(self.all_files_data)}.")

    def on_scan_thread_finished(self):
        """Called when the scanner thread has fully quit."""
        log.info("Scanner thread has finished.")
        self.scanner_thread = None
        self.scanner_worker = None
        # Ensure buttons are in correct state if thread finished unexpectedly
        if not hasattr(self, 'lyrics_fetcher_worker') or not self.lyrics_fetcher_worker:
             self.set_scan_buttons_enabled(is_scanning=False)


    # --- Lyric Fetcher ---

    def start_lyric_fetch(self):
        """Starts the lyric fetching worker thread."""
        if self.lyrics_fetcher_worker:
            self.log_to_panel("Lyric fetch is already running.")
            return

        files_to_check = list(self.all_files_data.values())

        files_to_fetch = [
            f for f in files_to_check
            if (f.lrc_status == "No Lyrics") or
               (f.lrc_status == "[.txt]" and not self.config.get("skip_synced_lyrics"))
        ]

        if not files_to_fetch:
            self.log_to_panel("No files need lyrics fetched based on current settings.")
            QMessageBox.information(self, "Lyrics", "All eligible files already have lyrics or are skipped.")
            return

        self.log_to_panel(f"Starting lyric fetch for {len(files_to_fetch)} files...")

        # --- Configure Worker ---
        self.lyrics_fetcher_thread = QThread()
        self.lyrics_fetcher_thread.setObjectName("LyricFetcherThread")
        self.lyrics_fetcher_worker = LyricFetcher(files_to_fetch, self.config.get("skip_synced_lyrics"))
        self.lyrics_fetcher_worker.moveToThread(self.lyrics_fetcher_thread)

        # --- Connect Signals ---
        self.lyrics_fetcher_worker.lyric_updated.connect(self.on_lyric_updated)
        self.lyrics_fetcher_worker.fetch_progress.connect(self.on_lyric_progress)
        self.lyrics_fetcher_worker.fetch_finished.connect(self.on_lyric_fetch_finished, Qt.ConnectionType.QueuedConnection)
        self.lyrics_fetcher_worker.log_message.connect(self.log_to_panel)

        self.lyrics_fetcher_thread.started.connect(self.lyrics_fetcher_worker.run)
        self.lyrics_fetcher_thread.finished.connect(self.on_lyric_thread_finished) # Cleanup

        # --- Update UI State ---
        self.set_scan_buttons_enabled(is_scanning=False) # Disable scan buttons
        self.fetch_lyrics_button.setEnabled(False)
        self.fetch_lyrics_button.setText("Fetching Lyrics...")
        self.stop_scan_button.setEnabled(True) # Re-use stop button
        # Disconnect scan stop, connect lyric stop
        try: self.stop_scan_button.clicked.disconnect()
        except TypeError: pass # If not connected
        self.stop_scan_button.clicked.connect(self.stop_lyric_fetch)
        self.stop_scan_button.setText("Stop Fetch")

        # --- Start ---
        self.lyrics_fetcher_thread.start()

    def stop_lyric_fetch(self):
        """Requests the lyric worker to stop."""
        if self.lyrics_fetcher_worker:
            self.log_to_panel("Stopping lyric fetch...")
            self.lyrics_fetcher_worker.stop()
            self.stop_scan_button.setEnabled(False)
            self.stop_scan_button.setText("Stopping...")

    @pyqtSlot(AudioFile)
    def on_lyric_updated(self, audio_file: AudioFile):
        """A file's lyric status was updated."""
        path_str = str(audio_file.path)
        # Update master data list
        self.all_files_data[path_str] = audio_file

        # Update tree item
        item = self.file_tree_items.get(path_str)
        if item:
            # --- UPDATED Column Index ---
            item.setText(3, audio_file.lrc_status) # Lyrics is now column 3
            # --- END Update ---
            # Change color
            if audio_file.lrc_status.startswith("[.lrc"):
                item.setForeground(3, QColor("cyan")) # Col 3
            elif audio_file.lrc_status.startswith("[.txt"):
                item.setForeground(3, QColor("yellow")) # Col 3
            else: # Reset color
                 item.setForeground(3, self.palette().color(QPalette.ColorRole.WindowText)) # Use default text color


    @pyqtSlot(int, int)
    def on_lyric_progress(self, current: int, total: int):
        """Update progress bar for lyric fetch."""
        percentage = 0
        if total > 0:
            percentage = int((current / total) * 100)

        self.scan_progress_bar.setValue(percentage)
        self.scan_progress_bar.setFormat(f"Lyrics: {current} / {total} ({percentage}%)")
        self.scan_count_label.setText(f"Fetched lyrics for {current} of {total} files")

    @pyqtSlot()
    def on_lyric_fetch_finished(self):
        """Lyric fetch worker is done."""
        self.log_to_panel("Lyric fetch finished.")

        if self.lyrics_fetcher_thread:
            self.lyrics_fetcher_thread.quit()

        self.scan_progress_bar.setFormat("Lyric Fetch Complete")
        self.fetch_lyrics_button.setEnabled(True)
        self.fetch_lyrics_button.setText("Fetch Missing Lyrics")

        # Reset stop button
        self.stop_scan_button.setEnabled(False)
        self.stop_scan_button.setText("Stop Scan")
        try: self.stop_scan_button.clicked.disconnect()
        except TypeError: pass # If not connected
        self.stop_scan_button.clicked.connect(self.stop_scan)
        # Re-enable scan buttons if scanner not running
        if not self.scanner_worker:
            self.set_scan_buttons_enabled(is_scanning=False)


    def on_lyric_thread_finished(self):
        """Called when the lyric thread has fully quit."""
        log.info("Lyric fetcher thread has finished.")
        self.lyrics_fetcher_thread = None
        self.lyrics_fetcher_worker = None
        # Ensure buttons are correct if thread finished unexpectedly
        if not self.scanner_worker:
             self.set_scan_buttons_enabled(is_scanning=False)

    # --- MusicBrainz ---

    @pyqtSlot()
    def on_auth_success(self):
        """Slot for 'auth_success' signal."""
        self.log_to_panel("Successfully authenticated with MusicBrainz.")
        self.login_button.setEnabled(True) # Keep enabled to allow re-login if needed
        self.login_button.setText("Logged In")
        self.login_button.setStyleSheet("background-color: #006400;") # Dark green

    @pyqtSlot(str)
    def on_auth_error(self, message: str):
        """Slot for 'auth_error' signal."""
        self.log_to_panel(f"MusicBrainz Auth Error: {message}")
        self.login_button.setEnabled(True)
        self.login_button.setText("Login Failed")
        self.login_button.setStyleSheet("background-color: #8b0000;") # Dark red
        # If it's a real error, show a popup
        if message not in ["Not logged in.", "Client ID/Secret not set."]:
             QMessageBox.warning(self, "Auth Error", f"MusicBrainz login failed:\n\n{message}")

    def fetch_new_releases(self):
        """Starts the process of fetching new releases."""
        if not self.all_files_data:
            self.log_to_panel("No files scanned. Please scan library first.")
            return

        # 1. Get unique artist names from scanned data
        artist_names = {
            f.artist for f in self.all_files_data.values()
            if f.artist and f.artist != "Unknown Artist"
        }

        if not artist_names:
            self.log_to_panel("No valid artists found in library.")
            return

        self.log_to_panel(f"Found {len(artist_names)} unique artists. Searching for IDs...")
        self.fetch_releases_button.setEnabled(False)
        self.fetch_releases_button.setText("1. Finding Artist IDs...")

        # 2. Start artist ID search via signal to MB worker
        self.start_artist_id_search.emit(artist_names)

    @pyqtSlot(list)
    def on_artist_search_finished(self, artist_id_list: List[Tuple[str, str]]):
        """
        Slot for 'artist_search_finished'.
        Receives the list of (name, id) tuples.
        """
        self.log_to_panel(f"Found MusicBrainz IDs for {len(artist_id_list)} artists.")
        self.artist_id_map = dict(artist_id_list)

        if not self.artist_id_map:
            self.log_to_panel("Could not find any artist IDs. Cannot fetch releases.")
            self.fetch_releases_button.setEnabled(True)
            self.fetch_releases_button.setText("Fetch New Releases")
            return

        # 3. Now, start the release fetch using these IDs via signal
        self.log_to_panel("Fetching new releases from MusicBrainz...")
        self.fetch_releases_button.setText("2. Fetching Releases...")
        self.start_release_fetch.emit(artist_id_list)

    @pyqtSlot(list)
    def on_releases_found(self, releases: List[MBRelease]):
        """Slot for 'releases_found' signal. Populates the releases tree."""
        self.log_to_panel(f"Found {len(releases)} new releases.")

        self.releases_tree.clear()

        if not releases:
            self.fetch_releases_button.setEnabled(True)
            self.fetch_releases_button.setText("Fetch New Releases")
            QMessageBox.information(self, "New Releases", "Your library is up to date!")
            return

        for release in releases:
            item = QTreeWidgetItem(self.releases_tree, [
                release.date,
                release.artist,
                release.title
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, release) # Store data obj
            item.setToolTip(2, f"{release.title}\n{release.url}")

        self.fetch_releases_button.setEnabled(True)
        self.fetch_releases_button.setText("Fetch New Releases")

        self.log_to_panel("--- See 'New Releases' tab for details ---")

    @pyqtSlot(QTreeWidgetItem, int)
    def on_release_double_clicked(self, item: QTreeWidgetItem, column: int):
        """Opens the MusicBrainz URL when a new release is double-clicked."""
        release_data: MBRelease = item.data(0, Qt.ItemDataRole.UserRole)
        if release_data and release_data.url:
            self.log_to_panel(f"Opening URL: {release_data.url}")
            webbrowser.open(release_data.url)


    def closeEvent(self, event):
        """Handle window close event."""
        log.info("Close event triggered. Shutting down...")

        # Save window geometry before potentially stopping threads
        try:
            self.config.set("window_geometry", self.saveGeometry().toHex().data().decode('utf-8'))
        except Exception as e:
            log.warning(f"Could not save window geometry: {e}")

        # Save settings (including scan state if stopped)
        self.config.save_config()

        # Signal all worker threads to stop
        log.debug("Signaling worker threads to stop...")
        if self.scanner_worker:
            self.scanner_worker.stop()
        if self.lyrics_fetcher_worker:
            self.lyrics_fetcher_worker.stop()
        # No need to explicitly stop MB worker, quit() handles it

        # Stop and wait for worker threads
        threads_to_wait = [self.mb_thread, self.scanner_thread, self.lyrics_fetcher_thread]
        active_threads = []
        for thread in threads_to_wait:
            if thread and thread.isRunning():
                active_threads.append(thread)
                log.debug(f"Quitting thread {thread.objectName()}...")
                thread.quit()

        # Wait for threads to finish
        if active_threads:
            log.debug(f"Waiting for {len(active_threads)} threads to finish...")
            all_finished = True
            for thread in active_threads:
                if not thread.wait(5000): # Wait up to 5 seconds per thread
                    log.warning(f"Thread {thread.objectName()} did not stop gracefully. Terminating.")
                    thread.terminate() # Force stop if necessary
                    all_finished = False

            if all_finished:
                log.debug("All threads finished gracefully.")
            else:
                log.warning("Some threads were terminated.")

        log.info("Proceeding with application exit.")
        event.accept() # Accept the close event


# === Main Execution ===

def main():
    # Enable high-DPI scaling using QGuiApplication (preferred)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    # The line below caused the AttributeError, it's removed
    # QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)

    app = QApplication(sys.argv)

    try:
        window = MusicWatcher()
        window.show()
        exit_code = app.exec()
        log.info(f"Application event loop finished with exit code {exit_code}.")
        sys.exit(exit_code)
    except Exception as e:
        log.critical(f"Unhandled exception in main: {e}", exc_info=True)
        # Optionally show an error message to the user
        error_dialog = QMessageBox()
        error_dialog.setIcon(QMessageBox.Icon.Critical)
        error_dialog.setWindowTitle("Fatal Error")
        error_dialog.setText(f"A critical error occurred:\n\n{e}\n\nSee log file for details:\n{LOG_FILE}")
        error_dialog.exec()
        sys.exit(1) # Exit with error code
    finally:
        logging.shutdown() # Ensure logs are flushed

if __name__ == "__main__":
    main()

