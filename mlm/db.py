"""SQLite storage. One connection per thread; WAL so the GUI can read while workers write."""
import os
import sqlite3
from pathlib import Path


def data_dir() -> Path:
    d = Path(os.environ.get("MLM_DATA_DIR") or Path.home() / "Library/Application Support/MusicLibraryManager")
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_db_path() -> str:
    return str(data_dir() / "library.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, root TEXT, size INTEGER, mtime REAL,
  format TEXT, codec TEXT, title TEXT DEFAULT '', artist TEXT DEFAULT '', albumartist TEXT DEFAULT '',
  album TEXT DEFAULT '', genre TEXT DEFAULT '', year TEXT DEFAULT '', track INTEGER, track_total INTEGER,
  disc INTEGER, disc_total INTEGER, duration REAL, bitrate INTEGER, sample_rate INTEGER, channels INTEGER,
  bits INTEGER, has_art INTEGER DEFAULT 0, art_hash TEXT, art_w INTEGER, art_h INTEGER, art_mime TEXT,
  readable INTEGER DEFAULT 1, error TEXT, hash TEXT, hash_key TEXT, fingerprint TEXT, acoustid_rec TEXT,
  missing INTEGER DEFAULT 0, scanned_at REAL);
CREATE INDEX IF NOT EXISTS ix_tracks_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS ix_tracks_album ON tracks(album);
CREATE INDEX IF NOT EXISTS ix_tracks_root ON tracks(root);
CREATE TABLE IF NOT EXISTS thumbs(art_hash TEXT PRIMARY KEY, data BLOB);
CREATE TABLE IF NOT EXISTS issues(track_id INTEGER, kind TEXT, category TEXT, severity TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_issues_track ON issues(track_id);
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, root TEXT, started REAL, finished REAL, files INTEGER,
  new INTEGER, updated INTEGER, unchanged INTEGER, errors INTEGER, missing INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS suggestions(id INTEGER PRIMARY KEY, track_id INTEGER, field TEXT, current TEXT,
  proposed TEXT, confidence TEXT, source TEXT, note TEXT, status TEXT DEFAULT 'pending', created REAL);
CREATE INDEX IF NOT EXISTS ix_sugg_track ON suggestions(track_id);
CREATE TABLE IF NOT EXISTS mb_matches(track_id INTEGER PRIMARY KEY, confidence TEXT, score REAL,
  recording_id TEXT, release_id TEXT, candidates TEXT, checked REAL);
CREATE TABLE IF NOT EXISTS http_cache(url TEXT PRIMARY KEY, body BLOB, fetched REAL);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, batch TEXT, ts REAL, track_id INTEGER, path TEXT,
  field TEXT, old TEXT, new TEXT, backup TEXT, undone INTEGER DEFAULT 0, note TEXT);
CREATE INDEX IF NOT EXISTS ix_history_batch ON history(batch);
CREATE TABLE IF NOT EXISTS dup_groups(id INTEGER PRIMARY KEY, method TEXT, key TEXT);
CREATE TABLE IF NOT EXISTS dup_members(group_id INTEGER, track_id INTEGER, keep INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE VIEW IF NOT EXISTS artists AS
  SELECT artist AS name, count(*) AS tracks, count(DISTINCT album) AS albums, sum(size) AS size
  FROM tracks WHERE missing=0 AND artist<>'' GROUP BY artist;
CREATE VIEW IF NOT EXISTS albums AS
  SELECT coalesce(nullif(albumartist,''), artist) AS albumartist, album, min(year) AS year, count(*) AS tracks,
         sum(has_art) AS with_art, group_concat(DISTINCT format) AS formats, sum(size) AS size
  FROM tracks WHERE missing=0 AND album<>'' GROUP BY lower(coalesce(nullif(albumartist,''), artist)), lower(album);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or default_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def get_setting(conn, key, default=""):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
    conn.commit()
