#!/usr/bin/env python3
"""
agent_sessions.py
Cross-platform agent: registra app/window sessions + attività di editing (file saves).
Non cattura contenuti di testo. Usa debounce per limitare polling.
"""

import os, time, json, sqlite3, threading, queue, platform, subprocess, uuid
from datetime import datetime, timezone
import psutil
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pymongo import MongoClient

from dotenv import load_dotenv

load_dotenv()

# CONFIG
DB_PATH = os.path.expanduser("~/.myapp_agent/events.db")
DEVICE_ID = os.environ.get("MYAPP_DEVICE_ID") or f"dev-{uuid.uuid4().hex[:8]}"
POLL_INTERVAL = 0.8  # lightweight poll for active window
PROC_POLL = 2.0  # poll for new/closed processes
UPLOAD_INTERVAL = 10
BATCH_SIZE = 100

# Which editor/process names we consider "editors" for associating file saves
KNOWN_EDITORS = [
    "Code",
    "code",
    "vscode",
    "sublime_text",
    "sublime",
    "atom",
    "vim",
    "nvim",
    "emacs",
    "idea",
    "pycharm",
]


def get_mongo_client():
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI non impostato, salto sincronizzazione MongoDB.")
        return None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.server_info()  # test rapido
        return client
    except Exception as e:
        print("Errore connessione Mongo:", e)
        return None


# SQLite helpers
def ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY, device_id TEXT, app TEXT, process TEXT,
        title TEXT, start_ts TEXT, end_ts TEXT, duration_ms INTEGER, meta TEXT, sent INTEGER DEFAULT 0
    )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS raw_events(
        id TEXT PRIMARY KEY, device_id TEXT, ts TEXT, type TEXT, payload TEXT, sent INTEGER DEFAULT 0
    )"""
    )
    conn.commit()
    conn.close()


def insert_session(app, process, title, start_ts, end_ts, meta=None):
    dur = int((end_ts - start_ts) * 1000)
    rec = (
        uuid.uuid4().hex,
        DEVICE_ID,
        app,
        process,
        title,
        datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
        datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
        dur,
        json.dumps(meta or {}),
        0,
    )
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO sessions(id,device_id,app,process,title,start_ts,end_ts,duration_ms,meta,sent) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rec,
    )
    conn.commit()
    conn.close()


def push_raw_event(t, payload):
    rec = (
        uuid.uuid4().hex,
        DEVICE_ID,
        datetime.now(tz=timezone.utc).isoformat(),
        t,
        json.dumps(payload),
        0,
    )
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO raw_events(id,device_id,ts,type,payload,sent) VALUES (?,?,?,?,?,?)",
        rec,
    )
    conn.commit()
    conn.close()


def fetch_unsent_events(limit=BATCH_SIZE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, ts, type, payload FROM raw_events WHERE sent=0 ORDER BY ts LIMIT ?",
        (limit,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def mark_events_sent(ids):
    if not ids:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executemany("UPDATE raw_events SET sent=1 WHERE id=?", [(i,) for i in ids])
    conn.commit()
    conn.close()


# Active window / process detection (best-effort, cross-platform)
def get_active_window():
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore
            # kernel32 = ctypes.windll.kernel32
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_name = ""
            try:
                p = psutil.Process(pid.value)
                proc_name = p.name()
            except Exception:
                proc_name = ""
            return proc_name, title
        elif system == "Darwin":
            # fallback: osascript to get frontmost app name + window title (best-effort)
            try:
                app = (
                    subprocess.check_output(
                        [
                            "osascript",
                            "-e",
                            'tell application "System Events" to get name of first process whose frontmost is true',
                        ],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )
            except Exception:
                app = ""
            try:
                title = (
                    subprocess.check_output(
                        [
                            "osascript",
                            "-e",
                            'tell application "System Events" to tell (first process whose frontmost is true) to get name of front window',
                        ],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )
            except Exception:
                title = ""
            return app, title
        else:
            # Linux: try xdotool if present
            try:
                winid = (
                    subprocess.check_output(
                        ["xdotool", "getwindowfocus"], stderr=subprocess.DEVNULL
                    )
                    .decode()
                    .strip()
                )
                title = (
                    subprocess.check_output(
                        ["xdotool", "getwindowname", winid], stderr=subprocess.DEVNULL
                    )
                    .decode()
                    .strip()
                )
            except Exception:
                title = ""
            # try to guess process by matching title to processes (best-effort)
            return "", title
    except Exception:
        return "", ""


# Sessionizer: detect focus changes and record sessions
class SessionTracker(threading.Thread):
    def __init__(self, poll_interval=POLL_INTERVAL):
        super().__init__(daemon=True)
        self.poll_interval = poll_interval
        self._stopped = threading.Event()
        self.current = None  # (app, process, title, start_ts)
        self.lock = threading.Lock()

    def run(self):
        while not self._stopped.is_set():
            proc, title = get_active_window()
            app = proc or title.split(" - ")[-1] if title else proc
            ts = time.time()
            with self.lock:
                if self.current is None:
                    self.current = (app, proc, title, ts)
                    push_raw_event(
                        "focus_start",
                        {"app": app, "proc": proc, "title": title, "ts": ts},
                    )
                else:
                    cur_app, cur_proc, cur_title, start_ts = self.current
                    # if changed significantly -> close previous session
                    if (proc != cur_proc) or (title != cur_title):
                        end_ts = ts
                        insert_session(
                            cur_app,
                            cur_proc,
                            cur_title,
                            start_ts,
                            end_ts,
                            meta={"reason": "focus_change"},
                        )
                        push_raw_event(
                            "focus_end",
                            {
                                "app": cur_app,
                                "proc": cur_proc,
                                "title": cur_title,
                                "start": start_ts,
                                "end": end_ts,
                            },
                        )
                        # new
                        self.current = (app, proc, title, ts)
                        push_raw_event(
                            "focus_start",
                            {"app": app, "proc": proc, "title": title, "ts": ts},
                        )
            time.sleep(self.poll_interval)

    def stop(self):
        with self.lock:
            if self.current:
                # close last
                cur_app, cur_proc, cur_title, start_ts = self.current
                end_ts = time.time()
                insert_session(
                    cur_app,
                    cur_proc,
                    cur_title,
                    start_ts,
                    end_ts,
                    meta={"reason": "shutdown"},
                )
                push_raw_event(
                    "focus_end",
                    {
                        "app": cur_app,
                        "proc": cur_proc,
                        "title": cur_title,
                        "start": start_ts,
                        "end": end_ts,
                    },
                )
                self.current = None
        self._stopped.set()


# Lightweight process watcher to detect launches/closes (low-frequency)
class ProcWatcher(threading.Thread):
    def __init__(self, poll=PROC_POLL):
        super().__init__(daemon=True)
        self.poll = poll
        self.known = {}
        self._stopped = threading.Event()

    def run(self):
        while not self._stopped.is_set():
            now = time.time()
            current = {}
            for p in psutil.process_iter(["pid", "name", "create_time"]):
                try:
                    current[p.pid] = (p.info["name"], p.info.get("create_time", now))
                except Exception:
                    pass
            # detect new
            new_pids = set(current.keys()) - set(self.known.keys())
            for pid in new_pids:
                name, ctime = current[pid]
                push_raw_event("proc_start", {"pid": pid, "name": name, "ts": ctime})
            # detect closed
            closed = set(self.known.keys()) - set(current.keys())
            for pid in closed:
                name, ctime = self.known[pid]
                push_raw_event("proc_stop", {"pid": pid, "name": name, "ts": now})
            self.known = current
            time.sleep(self.poll)

    def stop(self):
        self._stopped.set()


# File save detection (watch project roots) -> infer editing activity
class SaveHandler(FileSystemEventHandler):
    def __init__(self, editors=KNOWN_EDITORS):
        self.editors = editors

    def on_modified(self, event):
        # filter files by extension typically used in coding
        if event.is_directory:
            return
        ext = os.path.splitext(event.src_path)[1].lower()
        if ext in (
            ".py",
            ".js",
            ".ts",
            ".java",
            ".go",
            ".rb",
            ".php",
            ".html",
            ".css",
            ".json",
            ".rs",
            ".cpp",
            ".c",
            ".h",
        ):
            ts = time.time()
            # try to associate with active editor (best-effort)
            proc, title = get_active_window()
            probable_editor = any(
                k.lower() in (proc or "").lower()
                or (title and k.lower() in title.lower())
                for k in self.editors
            )
            payload = {
                "path": event.src_path,
                "ts": ts,
                "editor_associated": bool(probable_editor),
                "proc": proc,
                "title": title,
            }
            push_raw_event("file_save", payload)
            # if associated to editor, extend session meta: will be visible via raw_events
            # Note: we don't capture file contents


class MongoUploader(threading.Thread):
    def __init__(self, interval=UPLOAD_INTERVAL):
        super().__init__(daemon=True)
        self.interval = interval
        self._stopped = threading.Event()
        self.client = get_mongo_client()

        from pymongo.database import Database

        self.db: Database | None = (
            self.client[os.environ.get("MONGO_DB", "agent_sessions")]
            if self.client
            else None
        )

    def run(self):
        if self.db is None:
            print("Uploader Mongo disattivato (nessun DB).")
            return
        coll = self.db["raw_events"]
        while not self._stopped.is_set():
            rows = fetch_unsent_events(BATCH_SIZE)
            if rows:
                batch = []
                ids = []
                for r in rows:
                    ids.append(r[0])
                    batch.append(
                        {
                            "_id": r[0],
                            "device_id": DEVICE_ID,
                            "ts": r[1],
                            "type": r[2],
                            "payload": json.loads(r[3]),
                            "synced_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                try:
                    coll.insert_many(batch, ordered=False)
                    mark_events_sent(ids)
                    print(f"Sincronizzati {len(batch)} eventi su Mongo.")
                except Exception as e:
                    print("Errore upload Mongo:", e)
            time.sleep(self.interval)

    def stop(self):
        self._stopped.set()
        if self.client:
            self.client.close()


# Main runner
def main(watch_paths=None):
    ensure_db()
    # consent guard
    if os.environ.get("MYAPP_ALLOW_AGENT", "0") != "1":
        print(
            "Agent non autorizzato. Imposta MYAPP_ALLOW_AGENT=1 o usa UI per attivare."
        )
        return

    sess = SessionTracker()
    procw = ProcWatcher()
    uploader = MongoUploader()

    # file watchers: watch provided project roots or home by default (careful)
    observer = Observer()
    handler = SaveHandler()
    targets = watch_paths or [
        os.path.expanduser("~")
    ]  # in production, prefer explicit project roots
    for t in targets:
        try:
            observer.schedule(handler, path=t, recursive=True)
        except Exception:
            pass

    observer.start()
    sess.start()
    procw.start()
    uploader.start()
    print("Agent running... Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
    observer.stop()
    observer.join()
    sess.stop()
    procw.stop()
    uploader.stop()


if __name__ == "__main__":
    # optionally pass comma separated watch paths via env
    wp = os.environ.get("MYAPP_WATCH_PATHS")
    watch_paths = wp.split(",") if wp else None
    main(watch_paths)
