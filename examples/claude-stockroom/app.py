#!/usr/bin/env python3
"""Stockroom inventory HTTP app."""
import argparse
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SCHEMA_VERSION_TABLE = "schema_version"
IDEMPOTENCY_TABLE = "transfer_idempotency"

MIGRATIONS = [
    # (key, sql)
    ("transfer_idempotency", f"""
        CREATE TABLE IF NOT EXISTS {IDEMPOTENCY_TABLE} (
            idempotency_key TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """),
]


def get_db(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migrate(conn):
    conn.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} "
                 f"(key TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    applied = {r["key"] for r in conn.execute(f"SELECT key FROM {SCHEMA_VERSION_TABLE}")}
    for key, sql in MIGRATIONS:
        if key not in applied:
            conn.execute(sql)
            conn.execute(f"INSERT INTO {SCHEMA_VERSION_TABLE}(key) VALUES (?)", (key,))
    conn.commit()


def json_body(handler):
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length <= 0:
        return None
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def send_json(handler, status, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def is_positive_int(v):
    # Reject bool, float, and numeric strings. Accept JSON int.
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


class App:
    def __init__(self, db_path):
        self.db_path = db_path
        self._conn_local = threading.local()

    def conn(self):
        conn = getattr(self._conn_local, "conn", None)
        if conn is None:
            conn = get_db(self.db_path)
            migrate(conn)
            self._conn_local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._conn_local, "conn", None)
        if conn is not None:
            conn.close()
            self._conn_local.conn = None


def handle_request(app, handler):
    parsed = urlparse(handler.path)
    path = parsed.path
    qs = parse_qs(parsed.query, keep_blank_values=True)

    if path == "/api/health":
        try:
            app.conn().execute("SELECT 1").fetchone()
        except Exception:
            return send_json(handler, 500, {"error": "database unavailable"})
        return send_json(handler, 200, {"ok": True})

    if path == "/api/warehouses":
        try:
            rows = app.conn().execute(
                "SELECT id, name FROM warehouses ORDER BY id").fetchall()
        except Exception:
            return send_json(handler, 500, {"error": "database error"})
        return send_json(handler, 200, [{"id": r["id"], "name": r["name"]} for r in rows])

    if path == "/api/summary":
        try:
            conn = app.conn()
            pc = conn.execute(
                "SELECT COUNT(*) c FROM products WHERE active = 1").fetchone()["c"]
            au = conn.execute(
                "SELECT COALESCE(SUM(s.on_hand - s.reserved),0) v FROM stock s "
                "JOIN products p ON p.id = s.product_id WHERE p.active = 1").fetchone()["v"]
            iv = conn.execute(
                "SELECT COALESCE(SUM(s.on_hand * p.price_cents),0) v FROM stock s "
                "JOIN products p ON p.id = s.product_id WHERE p.active = 1").fetchone()["v"]
            low = conn.execute(
                "SELECT COUNT(*) c FROM stock s JOIN products p ON p.id = s.product_id "
                "WHERE p.active = 1 AND (s.on_hand - s.reserved) < p.reorder_point").fetchone()["c"]
        except Exception:
            return send_json(handler, 500, {"error": "database error"})
        return send_json(handler, 200, {
            "product_count": pc,
            "available_units": au,
            "inventory_value_cents": iv,
            "low_stock_count": low,
        })

    if path == "/api/inventory":
        # Validate numeric params
        raw_limit = qs.get("limit", ["20"])[0]
        raw_offset = qs.get("offset", ["0"])[0]
        try:
            limit = int(raw_limit)
            offset = int(raw_offset)
        except (ValueError, TypeError):
            return send_json(handler, 400, {"error": "limit and offset must be integers"})
        if limit < 1 or limit > 100:
            return send_json(handler, 400, {"error": "limit must be between 1 and 100"})
        if offset < 0:
            return send_json(handler, 400, {"error": "offset must be nonnegative"})

        warehouse_id = qs.get("warehouse_id", [None])[0]
        if warehouse_id is not None:
            try:
                warehouse_id = int(warehouse_id)
            except (ValueError, TypeError):
                return send_json(handler, 400, {"error": "warehouse_id must be an integer"})

        q = qs.get("q", [""])[0]

        where = ["p.active = 1"]
        params = []
        if warehouse_id is not None:
            where.append("s.warehouse_id = ?")
            params.append(warehouse_id)
        if q:
            where.append("(p.sku LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\')")
            pattern = "%" + escape_like(q) + "%"
            params.append(pattern)
            params.append(pattern)

        where_sql = " AND ".join(where)
        try:
            conn = app.conn()
            total = conn.execute(
                f"SELECT COUNT(*) c FROM stock s JOIN products p ON p.id = s.product_id "
                f"WHERE {where_sql}", params).fetchone()["c"]
            rows = conn.execute(
                f"SELECT p.id AS product_id, p.sku, p.name, s.warehouse_id, "
                f"w.name AS warehouse_name, s.on_hand, s.reserved, "
                f"(s.on_hand - s.reserved) AS available, p.price_cents, p.reorder_point "
                f"FROM stock s JOIN products p ON p.id = s.product_id "
                f"JOIN warehouses w ON w.id = s.warehouse_id "
                f"WHERE {where_sql} "
                f"ORDER BY p.sku, s.warehouse_id LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
        except Exception:
            return send_json(handler, 500, {"error": "database error"})

        items = [dict(r) for r in rows]
        return send_json(handler, 200, {"total": total, "items": items})

    if path == "/api/transfers":
        if handler.command != "POST":
            return send_json(handler, 405, {"error": "method not allowed"})
        body = json_body(handler)
        if body is None or not isinstance(body, dict):
            return send_json(handler, 400, {"error": "invalid JSON body"})

        # Validate integer fields
        for f in ("product_id", "from_warehouse_id", "to_warehouse_id", "quantity"):
            if f not in body or not is_positive_int(body[f]):
                return send_json(handler, 400,
                                 {"error": f"{f} must be a positive JSON integer"})

        key = body.get("idempotency_key")
        if not isinstance(key, str) or len(key) == 0:
            return send_json(handler, 400, {"error": "idempotency_key must be a nonempty string"})
        if len(key) > 128:
            return send_json(handler, 400, {"error": "idempotency_key must be at most 128 characters"})

        product_id = body["product_id"]
        from_wh = body["from_warehouse_id"]
        to_wh = body["to_warehouse_id"]
        quantity = body["quantity"]

        if from_wh == to_wh:
            return send_json(handler, 400, {"error": "source and destination warehouse must differ"})

        import hashlib
        payload = {
            "product_id": product_id,
            "from_warehouse_id": from_wh,
            "to_warehouse_id": to_wh,
            "quantity": quantity,
        }
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()

        conn = app.conn()
        try:
            with conn:  # transaction
                # Idempotency check
                existing = conn.execute(
                    f"SELECT payload_hash, response_json FROM {IDEMPOTENCY_TABLE} "
                    f"WHERE idempotency_key = ?", (key,)).fetchone()
                if existing:
                    if existing["payload_hash"] != payload_hash:
                        return send_json(handler, 409,
                                         {"error": "idempotency key already used with a different payload"})
                    return send_json(handler, 200, json.loads(existing["response_json"]))

                # Validate entities
                product = conn.execute(
                    "SELECT id FROM products WHERE id = ? AND active = 1",
                    (product_id,)).fetchone()
                if product is None:
                    return send_json(handler, 404, {"error": "product not found"})
                from_row = conn.execute(
                    "SELECT id FROM warehouses WHERE id = ?", (from_wh,)).fetchone()
                if from_row is None:
                    return send_json(handler, 404, {"error": "source warehouse not found"})
                to_row = conn.execute(
                    "SELECT id FROM warehouses WHERE id = ?", (to_wh,)).fetchone()
                if to_row is None:
                    return send_json(handler, 404, {"error": "destination warehouse not found"})

                src = conn.execute(
                    "SELECT on_hand, reserved FROM stock WHERE product_id = ? AND warehouse_id = ?",
                    (product_id, from_wh)).fetchone()
                if src is None:
                    return send_json(handler, 409, {"error": "insufficient available stock"})
                available = src["on_hand"] - src["reserved"]
                if available < quantity:
                    return send_json(handler, 409, {"error": "insufficient available stock"})

                # Decrease source
                conn.execute(
                    "UPDATE stock SET on_hand = on_hand - ? WHERE product_id = ? AND warehouse_id = ? AND (on_hand - reserved) >= ?",
                    (quantity, product_id, from_wh, quantity))
                # Guard: if this were racy the UPDATE WHERE ensures availability

                # Increase destination (creating row if necessary)
                dest = conn.execute(
                    "SELECT on_hand FROM stock WHERE product_id = ? AND warehouse_id = ?",
                    (product_id, to_wh)).fetchone()
                if dest is None:
                    conn.execute(
                        "INSERT INTO stock(product_id, warehouse_id, on_hand, reserved) VALUES (?, ?, ?, 0)",
                        (product_id, to_wh, quantity))
                else:
                    conn.execute(
                        "UPDATE stock SET on_hand = on_hand + ? WHERE product_id = ? AND warehouse_id = ?",
                        (quantity, product_id, to_wh))

                from_name = conn.execute(
                    "SELECT name FROM warehouses WHERE id = ?", (from_wh,)).fetchone()["name"]
                to_name = conn.execute(
                    "SELECT name FROM warehouses WHERE id = ?", (to_wh,)).fetchone()["name"]

                now = None
                conn.execute(
                    "INSERT INTO stock_movements(product_id, warehouse_id, delta, reason) VALUES (?, ?, ?, ?)",
                    (product_id, from_wh, -quantity, "transfer_out"))
                conn.execute(
                    "INSERT INTO stock_movements(product_id, warehouse_id, delta, reason) VALUES (?, ?, ?, ?)",
                    (product_id, to_wh, quantity, "transfer_in"))

                response = {
                    "ok": True,
                    "transfer_id": None,
                    "product_id": product_id,
                    "from_warehouse_id": from_wh,
                    "to_warehouse_id": to_wh,
                    "quantity": quantity,
                }
                # store with a stable identity (movement count)
                resp_json = json.dumps(response)
                conn.execute(
                    f"INSERT INTO {IDEMPOTENCY_TABLE}(idempotency_key, payload_hash, response_json) VALUES (?, ?, ?)",
                    (key, payload_hash, resp_json))
                return send_json(handler, 200, response)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                return send_json(handler, 409, {"error": "insufficient available stock"})
            return send_json(handler, 500, {"error": "database error"})
        except Exception:
            return send_json(handler, 500, {"error": "database error"})

    # Static files (safe): only exact known filenames under dist/
    if handler.command == "GET":
        static_map = {
            "/": "index.html",
            "/app.js": "app.js",
            "/index.html": "index.html",
        }
        if path in static_map:
            fname = static_map[path]
            fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", fname)
            if os.path.isfile(fpath) and fname.endswith((".html", ".js")):
                with open(fpath, "rb") as f:
                    data = f.read()
                ctype = "text/html; charset=utf-8" if fname.endswith(".html") else "application/javascript; charset=utf-8"
                handler.send_response(200)
                handler.send_header("Content-Type", ctype)
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
                return
    return send_json(handler, 404, {"error": "not found"})


def escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Handler(BaseHTTPRequestHandler):
    app = None

    def do_GET(self):
        handle_request(self.server.app, self)

    def do_POST(self):
        handle_request(self.server.app, self)

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()

    app = App(args.db)
    # migrate once on startup to verify schema
    conn = get_db(args.db)
    migrate(conn)
    conn.close()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.app = app
    print(f"listening on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
