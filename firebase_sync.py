"""
firebase_sync.py - Firebase Firestore persistence for the OTP bot.

Design principles:
  1. SQLite is ALWAYS the live runtime database. Firebase is only for backup/restore.
  2. Bot never waits on Firebase - all sync is async background thread.
  3. On fresh start (empty SQLite), restore from Firebase automatically.
  4. Every db write marks data "dirty"; background worker syncs every 30s.
  5. Admin panel changes trigger an immediate sync so data is never lost.

Setup: Set one of these environment variables with your Firebase service account:
  FIREBASE_SERVICE_ACCOUNT_JSON  - raw JSON string
  FIREBASE_SERVICE_ACCOUNT_B64   - base64 encoded JSON
  FIREBASE_SERVICE_ACCOUNT_FILE  - path to JSON file
"""

import base64
import hashlib
import json
import os
import threading
import time

# ── optional import ────────────────────────────────────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    _FB_AVAILABLE = True
except ImportError:
    firebase_admin = None
    credentials = None
    firestore = None
    _FB_AVAILABLE = False

# ── Firestore collection layout ────────────────────────────────────────────────
#   bot_data/{table_name}/rows/{doc_id}   <- all SQLite tables
#   bot_files/countries.json              <- countries.json file content

TABLES_COLLECTION = "bot_data"
FILE_COLLECTION   = "bot_files"

# Tables where we push EVERY row and delete stale Firestore docs.
# (omit append-only log tables to keep costs low)
MANAGED_TABLES = {
    "users", "numbers", "countries", "available_numbers",
    "used_numbers", "services", "admins", "group_emojis",
    "api_keys", "cdr_panels", "bot_settings", "withdraw_requests",
}

# Log tables: sync only the most recent N rows (cheap)
LOG_TABLES  = {"otps", "api_logs", "cdr_logs"}
LOG_LIMIT   = 2000   # rows to keep in Firestore for log tables

# Firestore batch limit is 500 ops; we stay safely under it
BATCH_LIMIT = 400

# ── module-level state ──────────────────────────────────────────────────────────
_db          = None      # Firestore client
_enabled     = False     # True after successful init
_dirty       = threading.Event()   # set when SQLite has changed
_stop        = threading.Event()   # set to stop the worker
_worker      = None      # background thread
_lock_ref    = None      # reference to air.py's db_lock (set during init)
_conn_ref    = None      # reference to air.py's conn    (set during init)
SYNC_INTERVAL = 30       # seconds between periodic syncs


# ── helpers ────────────────────────────────────────────────────────────────────

def _json_safe(v):
    """Convert a SQLite value to something Firestore can store."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(v)).decode()}
    return str(v)


def _from_json_safe(v):
    """Reverse _json_safe for restoring bytes."""
    if isinstance(v, dict) and "__b64__" in v:
        return base64.b64decode(v["__b64__"])
    return v


def _doc_id(cols, row, pk_idx):
    """Generate a stable Firestore document ID from a row's primary key."""
    if pk_idx and len(pk_idx) == 1:
        val = row[pk_idx[0]]
        if val is not None:
            return str(val).replace("/", "_")[:120]
    vals = [row[i] for i in pk_idx]
    raw = json.dumps([_json_safe(v) for v in vals], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _new_batch():
    return _db.batch()


def _commit(batch, ops):
    if ops:
        batch.commit()
    return _new_batch(), 0


# ── credentials ────────────────────────────────────────────────────────────────

def _load_credential():
    """Try all three credential sources. Returns a credentials.Certificate or None."""
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            return credentials.Certificate(json.loads(raw))
        except Exception as e:
            print(f"⚠️  FIREBASE_SERVICE_ACCOUNT_JSON parse error: {e}")

    b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64", "").strip()
    if b64:
        try:
            return credentials.Certificate(json.loads(base64.b64decode(b64).decode()))
        except Exception as e:
            print(f"⚠️  FIREBASE_SERVICE_ACCOUNT_B64 parse error: {e}")

    path = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()
    if path and os.path.isfile(path):
        try:
            return credentials.Certificate(path)
        except Exception as e:
            print(f"⚠️  FIREBASE_SERVICE_ACCOUNT_FILE error: {e}")

    return None


# ── public API ─────────────────────────────────────────────────────────────────

def init_firebase(conn, lock):
    """
    Initialize Firebase.  Must be called once at bot startup.
    conn and lock are the SQLite connection and threading.Lock from air.py.
    Returns True if Firebase is now enabled.
    """
    global _db, _enabled, _conn_ref, _lock_ref

    _conn_ref = conn
    _lock_ref = lock

    if not _FB_AVAILABLE:
        print("⚠️  firebase-admin not installed — Firebase disabled.")
        return False

    cred = _load_credential()
    if cred is None:
        print("ℹ️  No Firebase credentials found — running in local-only mode.")
        return False

    try:
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(cred)

        _db      = firestore.client()
        _enabled = True
        print("✅ Firebase Firestore connected.")
        return True

    except Exception as e:
        print(f"⚠️  Firebase init failed: {e}")
        _enabled = False
        return False


def is_enabled():
    return _enabled and _db is not None


def mark_dirty():
    """Call after every SQLite write so the background worker syncs soon."""
    if is_enabled():
        _dirty.set()


# ── table snapshot helpers ─────────────────────────────────────────────────────

def _snapshot_table(conn, lock, table, limit=None):
    """Return (cols, pk_idx, rows) for a table, or None on error."""
    try:
        with lock:
            cur = conn.cursor()
            cur.execute(f'PRAGMA table_info("{table}")')
            info = cur.fetchall()
            if not info:
                return None
            cols    = [x[1] for x in info]
            pk_idx  = [i for i, x in enumerate(info) if x[5] > 0]
            if limit:
                cur.execute(f'SELECT * FROM "{table}" ORDER BY id DESC LIMIT {limit}')
            else:
                cur.execute(f'SELECT * FROM "{table}"')
            rows = cur.fetchall()
        return cols, pk_idx, rows
    except Exception as e:
        print(f"⚠️  Snapshot error ({table}): {e}")
        return None


def _all_table_names(conn, lock):
    try:
        with lock:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


# ── Firestore write helpers ────────────────────────────────────────────────────

def _existing_doc_ids(collection_ref):
    """Fetch all document IDs in a Firestore sub-collection."""
    return {doc.id for doc in collection_ref.stream()}


def _push_table(batch, ops, table, cols, pk_idx, rows, do_cleanup=False):
    """
    Write rows to Firestore.
    If do_cleanup=True, also delete documents that are no longer in rows.
    Returns (batch, ops) after all writes.
    """
    collection = (
        _db.collection(TABLES_COLLECTION)
           .document(table)
           .collection("rows")
    )

    # Build local id→data map
    local = {}
    for row in rows:
        doc_id = _doc_id(cols, row, pk_idx)
        local[doc_id] = {col: _json_safe(val) for col, val in zip(cols, row)}

    # Delete stale docs first (for managed tables)
    if do_cleanup:
        try:
            stale = _existing_doc_ids(collection) - local.keys()
            for sid in stale:
                batch.delete(collection.document(sid))
                ops += 1
                if ops >= BATCH_LIMIT:
                    batch, ops = _commit(batch, ops)
        except Exception as e:
            print(f"⚠️  Stale-doc cleanup failed ({table}): {e}")

    # Upsert live docs (set without merge to overwrite)
    for doc_id, data in local.items():
        batch.set(collection.document(doc_id), data)
        ops += 1
        if ops >= BATCH_LIMIT:
            batch, ops = _commit(batch, ops)

    return batch, ops


# ── full sync ──────────────────────────────────────────────────────────────────

def sync_to_firestore(conn=None, lock=None):
    """
    Push entire SQLite state to Firestore.
    Uses module-level conn/lock if not passed explicitly.
    Safe to call from any thread.
    """
    if not is_enabled():
        return

    conn = conn or _conn_ref
    lock = lock or _lock_ref
    if conn is None or lock is None:
        return

    try:
        all_tables = _all_table_names(conn, lock)
        batch = _new_batch()
        ops   = 0

        for table in all_tables:
            if table in MANAGED_TABLES:
                result = _snapshot_table(conn, lock, table)
                if result is None:
                    continue
                cols, pk_idx, rows = result
                batch, ops = _push_table(
                    batch, ops, table, cols, pk_idx, rows, do_cleanup=True
                )
            elif table in LOG_TABLES:
                result = _snapshot_table(conn, lock, table, limit=LOG_LIMIT)
                if result is None:
                    continue
                cols, pk_idx, rows = result
                batch, ops = _push_table(
                    batch, ops, table, cols, pk_idx, rows, do_cleanup=False
                )
            # other tables are skipped

        batch, ops = _commit(batch, ops)  # flush remainder

        # Sync countries.json file if it exists
        if os.path.isfile("countries.json"):
            try:
                with open("countries.json", encoding="utf-8") as f:
                    content = f.read()
                _db.collection(FILE_COLLECTION).document("countries.json").set(
                    {"content": content, "ts": time.time()}
                )
            except Exception as e:
                print(f"⚠️  countries.json sync failed: {e}")

        print("☁️  Firebase sync done.")

    except Exception as e:
        print(f"⚠️  Firebase sync error: {e}")


def sync_table_now(table_name, conn=None, lock=None):
    """
    Immediately sync a single table to Firestore.
    Call this right after critical admin operations.
    """
    if not is_enabled():
        return

    conn = conn or _conn_ref
    lock = lock or _lock_ref
    if conn is None or lock is None:
        return

    try:
        is_managed = table_name in MANAGED_TABLES
        is_log     = table_name in LOG_TABLES
        limit      = LOG_LIMIT if is_log else None

        result = _snapshot_table(conn, lock, table_name, limit=limit)
        if result is None:
            return
        cols, pk_idx, rows = result

        batch = _new_batch()
        ops   = 0
        batch, ops = _push_table(
            batch, ops, table_name, cols, pk_idx, rows,
            do_cleanup=is_managed
        )
        _commit(batch, ops)
        print(f"☁️  Immediate sync: {table_name} ({len(rows)} rows)")

    except Exception as e:
        print(f"⚠️  Immediate sync error ({table_name}): {e}")


# ── restore from Firestore ─────────────────────────────────────────────────────

def restore_from_firestore(conn, lock):
    """
    On fresh start: if local SQLite has no real user data, pull from Firestore.

    "Fresh start" = users table has 0 rows.  The admin pre-insert only touches
    the admins table, so this check is reliable.
    """
    if not is_enabled():
        return

    try:
        # ── check if restore is needed ──────────────────────────────────────────
        with lock:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users")
            local_users = cur.fetchone()[0]

        if local_users > 0:
            print(f"ℹ️  Firebase restore skipped: {local_users} users already in SQLite.")
            return

        # ── verify Firestore has real data ──────────────────────────────────────
        probe = list(
            _db.collection(TABLES_COLLECTION)
               .document("users")
               .collection("rows")
               .limit(1)
               .stream()
        )
        if not probe:
            print("ℹ️  Firebase restore skipped: no data in Firestore yet.")
            return

        # ── restore every managed table ─────────────────────────────────────────
        print("☁️  Restoring data from Firebase...")
        total = 0

        with lock:
            cur = conn.cursor()

            for table in MANAGED_TABLES:
                # Make sure table exists locally (schema already created by air.py)
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)
                )
                if not cur.fetchone():
                    continue

                collection = (
                    _db.collection(TABLES_COLLECTION)
                       .document(table)
                       .collection("rows")
                )
                docs = list(collection.stream())
                if not docs:
                    continue

                cur.execute(f'PRAGMA table_info("{table}")')
                cols = [x[1] for x in cur.fetchall()]
                if not cols:
                    continue

                placeholders = ",".join("?" for _ in cols)
                quoted_cols  = ",".join(f'"{c}"' for c in cols)
                count = 0

                for doc in docs:
                    data   = doc.to_dict() or {}
                    values = [_from_json_safe(data.get(c)) for c in cols]
                    try:
                        cur.execute(
                            f'INSERT OR REPLACE INTO "{table}" ({quoted_cols}) '
                            f'VALUES ({placeholders})',
                            values
                        )
                        count += 1
                    except Exception as row_err:
                        print(f"  ⚠️  Row error in {table}: {row_err}")

                if count:
                    print(f"  ✅ {table}: {count} rows restored")
                    total += count

            # ── restore countries.json ──────────────────────────────────────────
            doc = _db.collection(FILE_COLLECTION).document("countries.json").get()
            if doc.exists:
                content = (doc.to_dict() or {}).get("content")
                if content:
                    with open("countries.json", "w", encoding="utf-8") as f:
                        f.write(content)
                    print("  ✅ countries.json restored")

            conn.commit()

        print(f"☁️  Restore complete — {total} total rows.")

    except Exception as e:
        print(f"⚠️  Firebase restore failed: {e}")


# ── background worker ──────────────────────────────────────────────────────────

def start_sync_worker(conn, lock, interval=SYNC_INTERVAL):
    """
    Start background thread that syncs SQLite → Firestore.
    Syncs immediately at start, then every `interval` seconds,
    and also whenever mark_dirty() is called.
    """
    global _worker, _conn_ref, _lock_ref

    _conn_ref = conn
    _lock_ref = lock

    if not is_enabled():
        return
    if _worker and _worker.is_alive():
        return

    def _run():
        # Wait a moment for bot to finish startup
        time.sleep(5)
        sync_to_firestore(conn, lock)

        while not _stop.is_set():
            # Wait for either dirty signal or timeout
            triggered = _dirty.wait(timeout=interval)
            _dirty.clear()

            if _stop.is_set():
                break

            sync_to_firestore(conn, lock)

    _worker = threading.Thread(target=_run, name="firebase-sync", daemon=True)
    _worker.start()
    print(f"☁️  Firebase sync worker started (interval={interval}s).")


def stop_sync_worker():
    _stop.set()
    _dirty.set()


# ── convenience: force full sync now (call from admin panel) ───────────────────

def force_sync_now(conn=None, lock=None):
    """Immediately push all data to Firestore. Blocking call."""
    sync_to_firestore(conn or _conn_ref, lock or _lock_ref)

