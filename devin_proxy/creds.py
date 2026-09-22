"""Credential resolution for the Devin session token.

Order: env vars -> Devin CLI credentials.toml -> Devin Desktop state DB.
"""
import json
import os
import re
import sqlite3

DEFAULT_API_SERVER = "https://server.codeium.com"


def _toml_strings(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r'^\s*([A-Za-z0-9_]+)\s*=\s*"(.*)"\s*$', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _credentials_paths():
    home = os.path.expanduser("~")
    paths = [
        os.path.join(home, ".local", "share", "devin", "credentials.toml"),
        os.path.join(home, ".config", "devin", "credentials.toml"),
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.insert(0, os.path.join(appdata, "devin", "credentials.toml"))
    env_path = os.environ.get("DEVIN_CREDENTIALS_FILE")
    if env_path:
        paths.insert(0, env_path)
    return paths


def _desktop_dbs():
    home = os.path.expanduser("~")
    paths = [
        os.path.join(home, ".config", "Devin", "User", "globalStorage", "state.vscdb"),
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.insert(0, os.path.join(appdata, "Devin", "User", "globalStorage", "state.vscdb"))
    return paths


def _valid_key(key):
    return isinstance(key, str) and len(key.strip()) >= 16 and not re.search(r"\s", key)


def _desktop_api_key(db_path):
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key='windsurfAuthStatus'").fetchone()
        finally:
            con.close()
        if row:
            key = json.loads(row[0]).get("apiKey")
            if _valid_key(key):
                return key.strip()
    except Exception:
        pass
    try:
        buf = open(db_path, "rb").read()
        m = re.search(rb'"apiKey"\s*:\s*"((?:[^"\\]|\\.)*)"', buf)
        if m:
            key = m.group(1).decode("latin1")
            if _valid_key(key):
                return key.strip()
    except Exception:
        pass
    return None


class Credentials:
    def __init__(self, api_key, api_server_url=DEFAULT_API_SERVER, source=None):
        self.api_key = api_key
        self.api_server_url = api_server_url.rstrip("/")
        self.source = source


def load():
    token_file = os.environ.get("DEVIN_SESSION_TOKEN_FILE")
    if token_file:
        try:
            key = open(token_file, encoding="utf-8").read().strip()
            if key:
                return Credentials(key, os.environ.get("DEVIN_API_SERVER_URL",
                                   DEFAULT_API_SERVER), token_file)
        except OSError:
            pass
    env_key = os.environ.get("DEVIN_SESSION_TOKEN") or os.environ.get("WINDSURF_API_KEY")
    if env_key:
        return Credentials(env_key, os.environ.get("DEVIN_API_SERVER_URL", DEFAULT_API_SERVER), "env")
    for path in _credentials_paths():
        try:
            if not os.path.exists(path):
                continue
            data = _toml_strings(open(path, encoding="utf-8").read())
            key = data.get("windsurf_api_key") or data.get("api_key")
            if key:
                return Credentials(key, data.get("api_server_url", DEFAULT_API_SERVER), path)
        except Exception:
            continue
    for db in _desktop_dbs():
        if os.path.exists(db):
            key = _desktop_api_key(db)
            if key:
                return Credentials(key, source=db)
    return None
