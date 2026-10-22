import json
import os
import re
from contextlib import contextmanager
from datetime import date, datetime

import boto3
import pymysql
from flask import Flask, jsonify, request, send_from_directory
from pymysql.cursors import DictCursor
from werkzeug.exceptions import HTTPException

app = Flask(__name__, static_folder="static")

# The DB login is read from AWS Secrets Manager (never stored in code).
SECRET_NAME = os.environ.get("DB_SECRET_NAME", "garden/db")
REGION = os.environ.get("AWS_REGION", "us-east-1")
DB_NAME = os.environ.get("DB_NAME", "gardendb")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS gardeners (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        email VARCHAR(150) NOT NULL UNIQUE,
        joined DATE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS plots (
        id INT PRIMARY KEY,
        name VARCHAR(20) NOT NULL UNIQUE,
        status ENUM('Available','Occupied','Maintenance') NOT NULL DEFAULT 'Available',
        gardener_id INT NULL,
        joined DATE NULL,
        FOREIGN KEY (gardener_id) REFERENCES gardeners(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS bookings (
        id INT AUTO_INCREMENT PRIMARY KEY,
        plot_id INT NOT NULL,
        gardener_id INT NOT NULL,
        booked_at DATETIME NOT NULL,
        released_at DATETIME NULL,
        FOREIGN KEY (plot_id) REFERENCES plots(id),
        FOREIGN KEY (gardener_id) REFERENCES gardeners(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS crops (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(60) NOT NULL,
        plot_id INT NOT NULL,
        plant_date DATE NOT NULL,
        emoji VARCHAR(8) NOT NULL DEFAULT '🌱',
        FOREIGN KEY (plot_id) REFERENCES plots(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

EMOJI = {"tomato": "🍅", "tomatoes": "🍅", "spinach": "🥬", "lettuce": "🥬", "carrot": "🥕",
         "carrots": "🥕", "sunflower": "🌻", "sunflowers": "🌻", "potato": "🥔", "corn": "🌽",
         "basil": "🌿", "strawberry": "🍓", "okra": "🌱"}

_cfg = None
_ready = False


def db_config():
    """Fetch DB connection details once, from Secrets Manager (or DB_* vars for local tests)."""
    global _cfg
    if _cfg:
        return _cfg
    if os.environ.get("DB_HOST"):
        _cfg = {"host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", 3306)),
                "user": os.environ.get("DB_USER", "root"), "password": os.environ.get("DB_PASSWORD", ""),
                "database": DB_NAME}
    else:
        client = boto3.client("secretsmanager", region_name=REGION)
        s = json.loads(client.get_secret_value(SecretId=SECRET_NAME)["SecretString"])
        _cfg = {"host": s["host"], "port": int(s.get("port", 3306)), "user": s["username"],
                "password": s["password"], "database": s.get("dbname", DB_NAME)}
    return _cfg


@contextmanager
def db():
    cfg = db_config()
    conn = pymysql.connect(cursorclass=DictCursor, charset="utf8mb4", connect_timeout=5, **cfg)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_ready():
    """Create tables and the 12 plots on first use (safe to run on several servers at once)."""
    global _ready
    if _ready:
        return
    with db() as conn, conn.cursor() as cur:
        for stmt in SCHEMA:
            cur.execute(stmt)
        for i in range(1, 13):
            cur.execute("INSERT IGNORE INTO plots (id,name,status) VALUES (%s,%s,%s)",
                        (i, f"Plot {i:02d}", "Maintenance" if i == 12 else "Available"))
    _ready = True


def clean(row):
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in row.items()}


def error(message, code):
    return jsonify({"error": message}), code


@app.before_request
def prepare():
    if request.path.startswith("/api/") and request.path != "/api/health":
        ensure_ready()


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return error(e.description, e.code)
    app.logger.exception("Unhandled error")
    return error("Server error. Check the application logs.", 500)


def load_plots(cur, status=None, plot_id=None):
    sql = ("SELECT p.id,p.name,p.status,p.joined,g.name AS gardener "
           "FROM plots p LEFT JOIN gardeners g ON g.id=p.gardener_id")
    conds, args = [], []
    if status:
        conds.append("p.status=%s"); args.append(status)
    if plot_id:
        conds.append("p.id=%s"); args.append(plot_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    cur.execute(sql + " ORDER BY p.id", args)
    plots = [clean(r) for r in cur.fetchall()]
    cur.execute("SELECT id,name,plot_id,plant_date,emoji FROM crops ORDER BY id")
    crops = {}
    for c in cur.fetchall():
        crops.setdefault(c["plot_id"], []).append(clean(c))
    for p in plots:
        p["gardener"] = p["gardener"] or ""
        p["joined"] = p["joined"] or ""
        p["crops"] = crops.get(p["id"], [])
    return plots


@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    # Used by the load balancer. Deliberately does not touch the database.
    return jsonify({"status": "ok", "service": "Community Garden API"})


@app.get("/api/health/db")
def health_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
    return jsonify({"status": "ok", "database": "connected"})


@app.get("/api/plots")
def get_plots():
    status = request.args.get("status")
    if status:
        status = next((s for s in ("Available", "Occupied", "Maintenance") if s.lower() == status.lower()), None)
        if not status:
            return error("Status must be Available, Occupied, or Maintenance.", 400)
    with db() as conn, conn.cursor() as cur:
        return jsonify(load_plots(cur, status=status))


@app.get("/api/plots/<int:plot_id>")
def get_plot(plot_id):
    with db() as conn, conn.cursor() as cur:
        plots = load_plots(cur, plot_id=plot_id)
    return jsonify(plots[0]) if plots else error("Plot not found.", 404)


@app.get("/api/gardeners")
def get_gardeners():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT g.id,g.name,g.joined,
                              GROUP_CONCAT(p.name ORDER BY p.id SEPARATOR ', ') AS plots
                       FROM gardeners g LEFT JOIN plots p ON p.gardener_id=g.id
                       GROUP BY g.id,g.name,g.joined ORDER BY g.id""")
        return jsonify([clean(r) for r in cur.fetchall()])


@app.post("/api/bookings")
def book_plot():
    data = request.get_json(silent=True) or {}
    try:
        plot_id = int(data.get("plot_id"))
    except (TypeError, ValueError):
        return error("plot_id must be a number.", 400)
    name = str(data.get("name", "")).strip()[:100]
    email = str(data.get("email", "")).strip().lower()[:150]
    if not name:
        return error("Gardener name is required.", 400)
    if not EMAIL_RE.match(email):
        return error("A valid email is required.", 400)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM plots WHERE id=%s FOR UPDATE", (plot_id,))
        plot = cur.fetchone()
        if not plot:
            return error("Plot not found.", 404)
        if plot["status"] != "Available":
            return error("This plot is not available for booking.", 409)
        cur.execute("SELECT id FROM gardeners WHERE email=%s", (email,))
        g = cur.fetchone()
        if g:
            gid = g["id"]
        else:
            cur.execute("INSERT INTO gardeners (name,email,joined) VALUES (%s,%s,%s)", (name, email, date.today()))
            gid = cur.lastrowid
        cur.execute("UPDATE plots SET status='Occupied',gardener_id=%s,joined=%s WHERE id=%s",
                    (gid, date.today(), plot_id))
        cur.execute("INSERT INTO bookings (plot_id,gardener_id,booked_at) VALUES (%s,%s,NOW())", (plot_id, gid))
        booking_id = cur.lastrowid
    return jsonify({"message": "Plot booked successfully.", "booking_id": booking_id,
                    "plot_id": plot_id, "status": "Occupied"}), 201


@app.post("/api/plots/<int:plot_id>/release")
def release_plot(plot_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM plots WHERE id=%s FOR UPDATE", (plot_id,))
        plot = cur.fetchone()
        if not plot:
            return error("Plot not found.", 404)
        if plot["status"] != "Occupied":
            return error("This plot does not have an active booking.", 409)
        cur.execute("UPDATE bookings SET released_at=NOW() WHERE plot_id=%s AND released_at IS NULL", (plot_id,))
        cur.execute("UPDATE plots SET status='Available',gardener_id=NULL,joined=NULL WHERE id=%s", (plot_id,))
        cur.execute("DELETE FROM crops WHERE plot_id=%s", (plot_id,))
    return jsonify({"message": "Plot released successfully.", "plot_id": plot_id, "status": "Available"})


@app.post("/api/plots/<int:plot_id>/crops")
def add_crop(plot_id):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:60]
    if not name:
        return error("Crop name is required.", 400)
    try:
        planted = date.fromisoformat(str(data.get("plant_date") or date.today().isoformat()))
    except ValueError:
        return error("plant_date must look like 2026-10-22.", 400)
    emoji = EMOJI.get(name.lower(), "🌱")
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM plots WHERE id=%s", (plot_id,))
        plot = cur.fetchone()
        if not plot:
            return error("Plot not found.", 404)
        if plot["status"] != "Occupied":
            return error("Book the plot before adding crops.", 409)
        cur.execute("INSERT INTO crops (name,plot_id,plant_date,emoji) VALUES (%s,%s,%s,%s)",
                    (name, plot_id, planted, emoji))
        crop_id = cur.lastrowid
    return jsonify({"id": crop_id, "name": name, "plot_id": plot_id,
                    "plant_date": planted.isoformat(), "emoji": emoji}), 201


@app.delete("/api/crops/<int:crop_id>")
def delete_crop(crop_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM crops WHERE id=%s", (crop_id,))
        if cur.rowcount == 0:
            return error("Crop not found.", 404)
    return jsonify({"message": "Crop removed successfully."})


@app.get("/api/stats")
def stats():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT status,COUNT(*) AS n FROM plots GROUP BY status")
        counts = {r["status"].lower(): r["n"] for r in cur.fetchall()}
        cur.execute("SELECT (SELECT COUNT(*) FROM plots) AS plots,(SELECT COUNT(*) FROM gardeners) AS gardeners,"
                    "(SELECT COUNT(*) FROM bookings WHERE released_at IS NULL) AS bookings,"
                    "(SELECT COUNT(*) FROM crops) AS crops")
        t = cur.fetchone()
    return jsonify({"total_plots": t["plots"], "available_plots": counts.get("available", 0),
                    "occupied_plots": counts.get("occupied", 0), "maintenance_plots": counts.get("maintenance", 0),
                    "registered_gardeners": t["gardeners"], "active_bookings": t["bookings"],
                    "total_crops": t["crops"]})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
