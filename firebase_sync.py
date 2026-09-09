"""Firebase/Firestore persistence for the bot's SQLite data.

Credentials are intentionally NOT stored in this repository. Configure one of:
FIREBASE_SERVICE_ACCOUNT_JSON, FIREBASE_SERVICE_ACCOUNT_B64, or
FIREBASE_SERVICE_ACCOUNT_FILE.
"""
import base64, hashlib, json, os, sqlite3, threading, time

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception:
    firebase_admin = None
    credentials = None
    firestore = None

TABLES_COLLECTION = "bot_data"
FILE_COLLECTION = "bot_files"
_sync_event = threading.Event()
_stop_event = threading.Event()
_db = None
_enabled = False
_worker = None


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    return str(value)


def _doc_id(columns, row, pk_indexes):
    vals = [row[i] for i in pk_indexes]
    if pk_indexes and len(pk_indexes) == 1 and vals[0] is not None:
        raw = str(vals[0]).replace("/", "_")
        return raw[:120]
    raw = json.dumps([_json_safe(v) for v in vals], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _credential_from_env():
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        return credentials.Certificate(json.loads(raw))
    b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64", "").strip()
    if b64:
        decoded = base64.b64decode(b64).decode("utf-8")
        return credentials.Certificate(json.loads(decoded))
    path = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()
    if path and os.path.exists(path):
        return credentials.Certificate(path)
    return None


def init_firebase():
    global _db, _enabled
    if firebase_admin is None:
        print("⚠️ firebase-admin is not installed; Firebase sync disabled.")
        return False
    try:
        cred = _credential_from_env()
        if cred is None:
            print("ℹ️ Firebase credentials not configured; local SQLite mode only.")
            return False
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(cred)
        _db = firestore.client()
        _enabled = True
        print("✅ Firebase Firestore persistence enabled.")
        return True
    except Exception as exc:
        print(f"⚠️ Firebase initialization failed: {exc}")
        _enabled = False
        return False


def is_enabled():
    return _enabled and _db is not None


def mark_dirty():
    if is_enabled():
        _sync_event.set()


def _table_names(conn):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return [r[0] for r in cur.fetchall()]


def sync_sqlite_to_firestore(conn, lock):
    if not is_enabled():
        return
    try:
        with lock:
            tables = _table_names(conn)
            snapshots = []
            for table in tables:
                cur = conn.cursor()
                cur.execute(f'PRAGMA table_info("{table}")')
                info = cur.fetchall()
                cols = [x[1] for x in info]
                pk_indexes = [i for i, x in enumerate(info) if x[5] > 0]
                cur.execute(f'SELECT * FROM "{table}"')
                rows = cur.fetchall()
                snapshots.append((table, cols, pk_indexes, rows))
            countries_json = None
            if os.path.exists("countries.json"):
                with open("countries.json", "r", encoding="utf-8") as f:
                    countries_json = f.read()

        # Firestore batches are limited to 500 operations; use smaller batches.
        batch = _db.batch()
        ops = 0
        for table, cols, pk_indexes, rows in snapshots:
            collection = _db.collection(TABLES_COLLECTION).document(table).collection("rows")
            for row in rows:
                doc_id = _doc_id(cols, row, pk_indexes)
                data = {col: _json_safe(val) for col, val in zip(cols, row)}
                batch.set(collection.document(doc_id), data, merge=True)
                ops += 1
                if ops >= 400:
                    batch.commit()
                    batch = _db.batch()
                    ops = 0
        if ops:
            batch.commit()
        if countries_json is not None:
            _db.collection(FILE_COLLECTION).document("countries.json").set(
                {"content": countries_json, "updated_at": time.time()}, merge=True
            )
        print("☁️ Firebase sync completed.")
    except Exception as exc:
        print(f"⚠️ Firebase sync failed: {exc}")


def _restore_table(conn, table):
    collection = _db.collection(TABLES_COLLECTION).document(table).collection("rows")
    docs = list(collection.stream())
    if not docs:
        return 0
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info("{table}")')
    cols = [x[1] for x in cur.fetchall()]
    placeholders = ",".join("?" for _ in cols)
    quoted_cols = ",".join('"' + c.replace('"', '""') + '"' for c in cols)
    count = 0
    for doc in docs:
        data = doc.to_dict() or {}
        values = [data.get(c) for c in cols]
        # Restore encoded bytes if any.
        values = [base64.b64decode(v["__bytes__"]) if isinstance(v, dict) and "__bytes__" in v else v for v in values]
        cur.execute(f'INSERT OR REPLACE INTO "{table}" ({quoted_cols}) VALUES ({placeholders})', values)
        count += 1
    return count


def restore_if_needed(conn, lock):
    if not is_enabled():
        return
    try:
        with lock:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users")
            local_users = cur.fetchone()[0]
        # A fresh Render filesystem normally has zero users. Restore the cloud copy once.
        if local_users != 0:
            return
        restored = 0
        with lock:
            for table in _table_names(conn):
                restored += _restore_table(conn, table)
            doc = _db.collection(FILE_COLLECTION).document("countries.json").get()
            if doc.exists:
                content = (doc.to_dict() or {}).get("content")
                if content:
                    with open("countries.json", "w", encoding="utf-8") as f:
                        f.write(content)
            conn.commit()
        if restored:
            print(f"☁️ Restored {restored} records from Firebase.")
    except Exception as exc:
        print(f"⚠️ Firebase restore failed: {exc}")


def start_worker(conn, lock, interval=20):
    global _worker
    if not is_enabled() or (_worker and _worker.is_alive()):
        return
    def worker():
        # Give the bot a moment to finish startup/schema creation.
        time.sleep(2)
        sync_sqlite_to_firestore(conn, lock)
        while not _stop_event.is_set():
            _sync_event.wait(timeout=interval)
            _sync_event.clear()
            if _stop_event.is_set():
                break
            sync_sqlite_to_firestore(conn, lock)
    _worker = threading.Thread(target=worker, name="firebase-sync", daemon=True)
    _worker.start()


def stop_worker():
    _stop_event.set()
    _sync_event.set()
