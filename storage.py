"""
storage.py
----------
Local, file-based storage for saved Jenkins connection profiles.
No database is used - everything lives in a single JSON file under the
user's home directory, per the "no database, no third-party apps" constraint.

Note: the API token is only lightly obfuscated (base64), NOT encrypted.
Anyone with file-system access to this machine could decode it. If you
need real protection, wire this up to the OS credential store (Windows
Credential Manager / macOS Keychain / Secret Service) - all of which are
accessible via each OS's *built-in* tooling if needed later.
"""

import json
import os
import base64

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".fluent_plus")
CONFIG_FILE = os.path.join(CONFIG_DIR, "profiles.json")


def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _obfuscate(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("utf-8")


def _deobfuscate(value: str) -> str:
    try:
        return base64.b64decode(value.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def load_profiles() -> dict:
    """Returns a dict: {profile_name: {url, username, token, last_job}}"""
    _ensure_dir()
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    profiles = {}
    for name, entry in raw.items():
        profiles[name] = {
            "url": entry.get("url", ""),
            "username": entry.get("username", ""),
            "token": _deobfuscate(entry.get("token", "")),
        }
    return profiles


def save_profile(profile_name: str, url: str, username: str, token: str):
    _ensure_dir()
    profiles = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                profiles = json.load(f)
        except (json.JSONDecodeError, OSError):
            profiles = {}

    profiles[profile_name] = {
        "url": url,
        "username": username,
        "token": _obfuscate(token),
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)


def delete_profile(profile_name: str):
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            profiles = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    profiles.pop(profile_name, None)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)
