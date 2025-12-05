# ================================
#  FLASK BACKEND FOR CANTEEN APP
#  Slot-based QR + Mess Day Tracking + Menu Photo + Monthly Reset
#  + Per-person one-scan-per-slot-per-day + Device Lock + Member Photo
#  + Export Logs (Excel / CSV)
# ================================
from flask import Flask, request, jsonify, Response

import mysql.connector
from datetime import date, datetime
import secrets
import qrcode
from io import BytesIO
import base64
import os
import calendar
import csv
from flask_cors import CORS
app = Flask(__name__)
CORS(app)
app.secret_key = "c0e4ce9f2d5b4475dfc9b49034f3e61a87b1c96c6b8a405ebcfa7c4df388e73a"

# STATIC / UPLOAD CONFIG
# -----------------------------
app.config["UPLOAD_FOLDER"] = "static"        # where menu & member photos are stored
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
MEMBERS_FOLDER = os.path.join(app.config["UPLOAD_FOLDER"], "members")
os.makedirs(MEMBERS_FOLDER, exist_ok=True)

# -----------------------------
# MYSQL DATABASE CONFIG
# -----------------------------
DB = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "port": int(os.getenv("DB_PORT"))
}


def db():
    return mysql.connector.connect(**DB)


# -----------------------------
# HELPER: DAYS IN MONTH
# -----------------------------
def get_days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


# -----------------------------
# HELPER: MEMBER PHOTO URL
# -----------------------------
def get_member_photo_url(member_id: int):
    """
    Looks for static/members/<member_id>.jpg / .jpeg / .png.
    Returns URL string or None.
    """
    for ext in ("jpg", "jpeg", "png"):
        path = os.path.join(MEMBERS_FOLDER, f"{member_id}.{ext}")
        if os.path.exists(path):
            return f"/static/members/{member_id}.{ext}"
    return None


# ============================================================
#  FRONTEND ROUTES
# ============================================================
@app.route("/")
def student_page():
    # Student main page (scanner + summary + menu photo)
    return open("main.html", encoding="utf-8").read()


@app.route("/admin2025-mess")
def admin_page():
    # Main admin dashboard
    return open("admin2025-mess.html", encoding="utf-8").read()


@app.route("/admin2025-mess/qr")
def admin_qr_page():
    # Dedicated QR page for admin (slot-based QR)
    return open("admin2025-qr.html", encoding="utf-8").read()


# ============================================================
#  HELPER: CURRENT SLOT (AUTO)
# ============================================================
def get_current_slot():
    h = datetime.now().hour
    if 6 <= h < 11:
        return "morning"
    elif 11 <= h < 15:
        return "afternoon"
    elif 15 <= h < 19:
        return "evening"
    else:
        return "night"


@app.route("/api/current-slot")
def current_slot_api():
    return jsonify({"slot": get_current_slot()})


# ============================================================
#  SIMPLE LOGIN BY ROLL NUMBER  (/api/login?roll=...&device_id=...)
#  - If device_id empty → behaves like old login (no lock)
#  - If device_id present:
#       * if member has no device_id → lock to this device
#       * if member has same device_id → allow
#       * if different device_id → reject (device-locked)
# ============================================================
@app.route("/api/login")
def login_api():
    roll = request.args.get("roll")
    device_id = request.args.get("device_id")  # can be null / empty

    c = db()
    cur = c.cursor(dictionary=True)
    cur.execute("SELECT id, name, device_id FROM members WHERE roll_or_id = %s", (roll,))
    row = cur.fetchone()
    cur.close()
    c.close()

    if not row:
        return jsonify({"success": False, "message": "Invalid roll number"})

    member_id = row["id"]
    current_device = row.get("device_id")

    # If no device_id passed → behave like old login (no lock enforcement)
    if not device_id:
        return jsonify({
            "success": True,
            "member_id": member_id,
            "name": row["name"],
            "locked": bool(current_device)
        })

    # If member already locked to some device
    if current_device:
        if current_device == device_id:
            # Same device – allow
            return jsonify({
                "success": True,
                "member_id": member_id,
                "name": row["name"],
                "locked": True
            })
        else:
            # Different device – block
            return jsonify({
                "success": False,
                "locked": True,
                "message": "This account is already used on another device. Contact admin."
            })

    # Not locked yet + device_id provided → lock account to this device
    c2 = db()
    cur2 = c2.cursor()
    cur2.execute("UPDATE members SET device_id=%s WHERE id=%s", (device_id, member_id))
    c2.commit()
    cur2.close()
    c2.close()

    return jsonify({
        "success": True,
        "member_id": member_id,
        "name": row["name"],
        "locked": True
    })


# ============================================================
#  MENU APIs (TEXT) – still available (not used much now)
# ============================================================
@app.route("/api/menu", methods=["GET", "POST"])
def menu():
    if request.method == "GET":
        c = db()
        cur = c.cursor(dictionary=True)
        cur.execute("SELECT * FROM menu WHERE available=1")
        rows = cur.fetchall()
        cur.close()
        c.close()
        return jsonify(rows)

    data = request.get_json()
    title = data["title"]
    desc = data.get("description", "")

    c = db()
    cur = c.cursor()
    cur.execute("INSERT INTO menu(title,description) VALUES(%s,%s)", (title, desc))
    c.commit()
    cur.close()
    c.close()

    return jsonify({"status": "saved"})


@app.route("/api/menu/<int:item_id>", methods=["DELETE"])
def delete_menu(item_id):
    c = db()
    cur = c.cursor()
    cur.execute("DELETE FROM menu WHERE id=%s", (item_id,))
    c.commit()
    cur.close()
    c.close()
    return jsonify({"status": "deleted"})


# ============================================================
#  MENU PHOTO APIs
# ============================================================
@app.route("/api/upload-menu-photo", methods=["POST"])
def upload_menu_photo():
    """
    Admin uploads today's menu image.
    Saves as static/menu.<ext> and returns URL.
    """
    if "photo" not in request.files:
        return jsonify({"success": False, "error": "No file part"}), 400

    file = request.files["photo"]
    if file.filename == "":
        return jsonify({"success": False, "error": "No file selected"}), 400

    if "." not in file.filename:
        return jsonify({"success": False, "error": "Invalid filename"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in ("jpg", "jpeg", "png"):
        return jsonify({"success": False, "error": "Only JPG/PNG allowed"}), 400

    # Remove old menu.* files
    for old_ext in ("jpg", "jpeg", "png"):
        old_path = os.path.join(app.config["UPLOAD_FOLDER"], f"menu.{old_ext}")
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass

    filename = f"menu.{ext}"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(save_path)

    url = f"/static/{filename}"
    return jsonify({"success": True, "url": url})


@app.route("/api/menu-photo")
def get_menu_photo():
    """
    Returns current menu photo URL for student app.
    """
    for ext in ("jpg", "jpeg", "png"):
        path = os.path.join(app.config["UPLOAD_FOLDER"], f"menu.{ext}")
        if os.path.exists(path):
            return jsonify({"photo": f"/static/menu.{ext}"})

    return jsonify({"photo": None})


# ============================================================
#  MEMBER APIs (include photo upload support for POST)
# ============================================================
@app.route("/api/members", methods=["GET", "POST"])
def members():
    if request.method == "GET":
        c = db()
        cur = c.cursor(dictionary=True)
        cur.execute("SELECT * FROM members")
        rows = cur.fetchall()
        cur.close()
        c.close()

        # Attach photo URL for each member
        for r in rows:
            r["photo"] = get_member_photo_url(r["id"])
        return jsonify(rows)

    # ---------- POST (Add member) ----------
    # We support FormData (with optional photo) from admin.html
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        form = request.form
        files = request.files

        name = form.get("name", "").strip()
        roll = form.get("roll_or_id", "").strip()
        slots = form.get("allowed_slots", "").strip()

        if not name or not roll or not slots:
            return jsonify({"status": "error", "message": "Missing fields"}), 400

        today = date.today()
        days_in_month = get_days_in_month(today.year, today.month)

        c = db()
        cur = c.cursor()
        cur.execute(
            """
            INSERT INTO members(name,roll_or_id,allowed_slots,used_days,remaining,carry_forward)
            VALUES(%s,%s,%s,0,%s,0)
            """,
            (name, roll, slots, days_in_month),
        )
        member_id = cur.lastrowid
        c.commit()
        cur.close()
        c.close()

        # Save photo if provided
        if "photo" in files:
            file = files["photo"]
            if file and file.filename:
                if "." in file.filename:
                    ext = file.filename.rsplit(".", 1)[1].lower()
                    if ext in ("jpg", "jpeg", "png"):
                        # remove old files for this member
                        for old_ext in ("jpg", "jpeg", "png"):
                            old_path = os.path.join(MEMBERS_FOLDER, f"{member_id}.{old_ext}")
                            if os.path.exists(old_path):
                                try:
                                    os.remove(old_path)
                                except Exception:
                                    pass
                        filename = f"{member_id}.{ext}"
                        save_path = os.path.join(MEMBERS_FOLDER, filename)
                        file.save(save_path)

        return jsonify({"status": "saved", "member_id": member_id})

    # ---------- JSON fallback (older usage) ----------
    data = request.get_json()
    name = data["name"]
    roll = data["roll_or_id"]
    slots = data["allowed_slots"]

    today = date.today()
    days_in_month = get_days_in_month(today.year, today.month)

    c = db()
    cur = c.cursor()
    cur.execute(
        """
        INSERT INTO members(name,roll_or_id,allowed_slots,used_days,remaining,carry_forward)
        VALUES(%s,%s,%s,0,%s,0)
        """,
        (name, roll, slots, days_in_month),
    )
    c.commit()
    cur.close()
    c.close()

    return jsonify({"status": "saved"})


@app.route("/api/member/<int:mid>", methods=["DELETE"])
def delete_member(mid):
    c = db()
    cur = c.cursor()
    cur.execute("DELETE FROM members WHERE id=%s", (mid,))
    c.commit()
    cur.close()
    c.close()

    # Also delete photo if exists
    for ext in ("jpg", "jpeg", "png"):
        path = os.path.join(MEMBERS_FOLDER, f"{mid}.{ext}")
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    return jsonify({"status": "deleted"})


# ============================================================
#  SLOT-BASED QR TOKENS (ONE QR PER SLOT, COMMON FOR ALL)
# ============================================================
def create_slot_token(slot: str) -> str:
    """Create a global QR token for the given slot (not per member)."""
    token = secrets.token_urlsafe(16)
    c = db()
    cur = c.cursor()
    # member_id = NULL → slot-level QR
    cur.execute(
        """
        INSERT INTO qr_tokens(member_id, token, slot, valid_date)
        VALUES(%s,%s,%s,%s)
        """,
        (None, token, slot, date.today()),
    )
    c.commit()
    cur.close()
    c.close()
    return token

FRONTEND_URL = os.getenv("FRONTEND_URL","https://cecmess.netlify.app")
@app.route("/api/get-slot-qr")
def get_slot_qr():
    """
    Admin QR page calls this.
    For current slot & date:
      - if token exists with member_id IS NULL → reuse
      - else → create new token
      - return QR image as data URL
    """
    slot = get_current_slot()

    # Find existing slot-level token
    c = db()
    cur = c.cursor(dictionary=True)
    cur.execute(
        """
        SELECT token FROM qr_tokens
        WHERE member_id IS NULL AND slot=%s AND valid_date=%s
        LIMIT 1
        """,
        (slot, date.today()),
    )
    row = cur.fetchone()
    cur.close()
    c.close()

    if row:
        token = row["token"]
    else:
        token = create_slot_token(slot)

    # Build QR image
    url = FRONTEND_URL + "/?token=" + token
    img = qrcode.make(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    qr_data = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    return jsonify({"qr": qr_data, "slot": slot})


# ============================================================
#  (OLD) MEMBER-BASED QR GENERATION (STILL AVAILABLE)
# ============================================================
def create_token(member_id, slot):
    token = secrets.token_urlsafe(16)
    c = db()
    cur = c.cursor()
    cur.execute(
        """
        INSERT INTO qr_tokens(member_id,token,slot,valid_date)
        VALUES(%s,%s,%s,%s)
        """,
        (member_id, token, slot, date.today()),
    )
    c.commit()
    cur.close()
    c.close()
    return token


@app.route("/api/member/generate/<int:mid>")
def generate_qr(mid):
    c = db()
    cur = c.cursor(dictionary=True)
    cur.execute("SELECT allowed_slots FROM members WHERE id=%s", (mid,))
    m = cur.fetchone()
    cur.close()
    c.close()

    if not m:
        return jsonify([])

    slots = [s.strip() for s in m["allowed_slots"].split(",") if s.strip()]
    qr_list = []

    for s in slots:
        t = create_token(mid, s)
        url = FRONTEND_URL + "/?token=" + t

        img = qrcode.make(url)
        buf = BytesIO()
        img.save(buf, format="PNG")
        qr_data = (
            "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        )

        qr_list.append({"slot": s, "qr_data": qr_data})

    return jsonify(qr_list)


@app.route("/api/generate_all")
def generate_all():
    c = db()
    cur = c.cursor(dictionary=True)
    cur.execute("SELECT * FROM members")
    members = cur.fetchall()
    cur.close()
    c.close()

    count = 0
    for m in members:
        slots = [x.strip() for x in m["allowed_slots"].split(",") if x.strip()]
        for s in slots:
            c2 = db()
            cur2 = c2.cursor()
            cur2.execute(
                """
                SELECT id FROM qr_tokens
                WHERE member_id=%s AND slot=%s AND valid_date=%s
                """,
                (m["id"], s, date.today()),
            )
            exists = cur2.fetchone()
            cur2.close()
            c2.close()

            if not exists:
                create_token(m["id"], s)
                count += 1

    return jsonify({"message": f"Generated {count} QR tokens"})


# ============================================================
#  MESS DAY TRACKING
# ============================================================
def ensure_day_record(member_id):
    c = db()
    cur = c.cursor()
    cur.execute(
        """
        SELECT id FROM mess_days WHERE member_id=%s AND date=%s
        """,
        (member_id, date.today()),
    )
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO mess_days(member_id,date) VALUES(%s,%s)",
            (member_id, date.today()),
        )
        c.commit()
    cur.close()
    c.close()


def update_usage(member_id, slot):
    """
    Mark slot used today. If this is first slot today,
    increase used_days and decrease remaining.
    """
    ensure_day_record(member_id)

    # 1) Mark this slot as used = 1
    c = db()
    cur = c.cursor()
    cur.execute(
        f"""
        UPDATE mess_days SET {slot}=1
        WHERE member_id=%s AND date=%s
        """,
        (member_id, date.today()),
    )
    c.commit()
    cur.close()
    c.close()

    # 2) If day not counted yet, count one full mess day
    c2 = db()
    cur2 = c2.cursor(dictionary=True)
    cur2.execute(
        """
        SELECT morning,afternoon,evening,night,consumed
        FROM mess_days WHERE member_id=%s AND date=%s
        """,
        (member_id, date.today()),
    )
    r = cur2.fetchone()
    cur2.close()
    c2.close()

    if r and r["consumed"] == 0:
        if r["morning"] or r["afternoon"] or r["evening"] or r["night"]:
            # mark day consumed
            c3 = db()
            cur3 = c3.cursor()
            cur3.execute(
                """
                UPDATE mess_days SET consumed=1
                WHERE member_id=%s AND date=%s
                """,
                (member_id, date.today()),
            )
            c3.commit()
            cur3.close()
            c3.close()

            # update member counters
            c4 = db()
            cur4 = c4.cursor()
            cur4.execute(
                """
                UPDATE members
                SET used_days = used_days + 1,
                    remaining = remaining - 1
                WHERE id=%s
                """,
                (member_id,),
            )
            c4.commit()
            cur4.close()
            c4.close()


# ============================================================
#  SCAN LOGGING
# ============================================================
def save_scan(member_id, token, slot, success, message):
    c = db()
    cur = c.cursor()
    cur.execute(
        """
        INSERT INTO scans(member_id,token,slot,valid_date,success,message,scanned_at)
        VALUES(%s,%s,%s,%s,%s,%s,NOW())
        """,
        (member_id, token, slot, date.today(), int(success), message),
    )
    c.commit()
    cur.close()
    c.close()


# ============================================================
#  MONTHLY RESET CORE LOGIC
#  - used_days = 0
#  - carry_forward = 0
#  - remaining = days_in_current_month
#  - mess_days cleared
# ============================================================
def perform_month_reset(today: date):
    days_in_month = get_days_in_month(today.year, today.month)

    conn = db()
    cur = conn.cursor()

    # Reset all members
    cur.execute(
        """
        UPDATE members
        SET used_days = 0,
            remaining = %s,
            carry_forward = 0
        """,
        (days_in_month,),
    )

    # Clear all daily records
    cur.execute("DELETE FROM mess_days")

    conn.commit()
    cur.close()
    conn.close()


def monthly_reset():
    """
    Auto reset:
    Runs automatically on 1st of every month (via before_request).
    Uses app_meta table to ensure it runs only once per month.
    """
    today = date.today()

    # Only run on 1st
    if today.day != 1:
        return

    conn = db()
    cur = conn.cursor(dictionary=True)

    # Ensure app_meta table exists
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            id INT PRIMARY KEY,
            last_reset DATE
        )
        """
    )

    # Read last_reset row
    cur.execute("SELECT last_reset FROM app_meta WHERE id = 1")
    row = cur.fetchone()

    if row is None:
        cur.execute("INSERT INTO app_meta (id, last_reset) VALUES (1, NULL)")
        conn.commit()
        last_reset = None
    else:
        last_reset = row["last_reset"]

    # If already reset this month, do nothing
    if last_reset is not None and last_reset.year == today.year and last_reset.month == today.month:
        cur.close()
        conn.close()
        return

    # Perform reset for this month
    perform_month_reset(today)

    # Update last_reset
    cur = conn.cursor()
    cur.execute("UPDATE app_meta SET last_reset = %s WHERE id = 1", (today,))
    conn.commit()

    cur.close()
    conn.close()


# Run monthly_reset automatically before every request
@app.before_request
def auto_monthly_reset():
    monthly_reset()


# ============================================================
#  MANUAL RESET (ADMIN BUTTON)  /api/reset-month
#  - Same as perform_month_reset, but can be called anytime.
#  - Also updates app_meta.last_reset to today.
# ============================================================
@app.route("/api/reset-month", methods=["POST", "GET"])
def reset_month_api():
    today = date.today()

    # Run reset
    perform_month_reset(today)

    # Also update app_meta so auto reset won't repeat for this month
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            id INT PRIMARY KEY,
            last_reset DATE
        )
        """
    )
    cur.execute("SELECT last_reset FROM app_meta WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO app_meta (id, last_reset) VALUES (1, %s)", (today,))
    else:
        cur.execute("UPDATE app_meta SET last_reset = %s WHERE id = 1", (today,))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "reset_done", "days_in_month": get_days_in_month(today.year, today.month)})


# ============================================================
#  SCAN VALIDATION (STUDENT SCANNER)
#  Student app sends: { token, member_id }
#  - Checks QR validity
#  - Checks allowed slot
#  - Blocks double scan for same slot & day
#  - Returns member photo URL if exists
# ============================================================
@app.route("/api/validate", methods=["POST"])
def validate_scan():
    data = request.get_json()
    token = data.get("token")
    member_id = data.get("member_id")

    if not token or not member_id:
        return jsonify({"success": False, "message": "Missing data"})

    slot = get_current_slot()

    # 1) Check slot-level token exists for today
    c = db()
    cur = c.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id FROM qr_tokens
        WHERE token=%s AND slot=%s AND valid_date=%s
        """,
        (token, slot, date.today()),
    )
    trow = cur.fetchone()
    cur.close()
    c.close()

    if not trow:
        save_scan(member_id, token, slot, False, "Invalid or expired QR")
        return jsonify({"success": False, "message": "Invalid or expired QR"})

    # 2) Load member & check slot allowed
    c2 = db()
    cur2 = c2.cursor(dictionary=True)
    cur2.execute(
        "SELECT name, allowed_slots FROM members WHERE id=%s",
        (member_id,),
    )
    m = cur2.fetchone()
    cur2.close()
    c2.close()

    if not m:
        save_scan(member_id, token, slot, False, "Unknown member")
        return jsonify({"success": False, "message": "Unknown member"})

    allowed = [s.strip() for s in m["allowed_slots"].split(",") if s.strip()]
    if slot not in allowed:
        save_scan(member_id, token, slot, False, "Slot not allowed")
        return jsonify(
            {"success": False, "message": "You are not allowed for this slot"}
        )

    # 3) BLOCK DOUBLE SCAN – if this slot already 1 for today, reject
    c3 = db()
    cur3 = c3.cursor(dictionary=True)
    cur3.execute(
        """
        SELECT morning,afternoon,evening,night
        FROM mess_days
        WHERE member_id=%s AND date=%s
        """,
        (member_id, date.today()),
    )
    row = cur3.fetchone()
    cur3.close()
    c3.close()

    if row:
        already = row.get(slot, 0)
        if already == 1:
            save_scan(member_id, token, slot, False, "Already scanned today")
            return jsonify(
                {
                    "success": False,
                    "message": "Already scanned for this slot today",
                }
            )

    # 4) OK → update usage & log
    update_usage(member_id, slot)
    save_scan(member_id, token, slot, True, "OK")

    # Member photo for student screen
    photo_url = get_member_photo_url(int(member_id))

    return jsonify({"success": True, "name": m["name"], "photo": photo_url})


# ============================================================
#  LOGS + SUMMARY
# ============================================================
@app.route("/api/logs")
def logs():
    c = db()
    cur = c.cursor(dictionary=True)
    cur.execute(
        """
        SELECT 
            DATE_FORMAT(s.scanned_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS scanned_at,
            m.name,
            s.slot,
            s.success
        FROM scans s
        LEFT JOIN members m ON m.id = s.member_id
        ORDER BY s.scanned_at DESC
        LIMIT 200
        """
    )
    rows = cur.fetchall()
    cur.close()
    c.close()
    return jsonify(rows)
@app.route("/api/mess-status")
def mess_status():
    member_id = request.args.get("id", 1)

    c = db()
    cur = c.cursor(dictionary=True)
    cur.execute(
        """
        SELECT used_days, remaining, carry_forward
        FROM members WHERE id=%s
        """,
        (member_id,),
    )
    row = cur.fetchone()
    cur.close()
    c.close()

    if not row:
        return jsonify(
            {"used_days": 0, "remaining": 0, "carry_forward": 0, "paid_days": 0}
        )

    # Paid days = current month's paid days = remaining + used
    row["paid_days"] = row["used_days"] + row["remaining"]
    return jsonify(row)


@app.route("/api/mess-overview")
def mess_overview():
    c = db()
    cur = c.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, name, used_days, remaining, carry_forward
        FROM members
        ORDER BY name
        """
    )
    rows = cur.fetchall()
    cur.close()
    c.close()
    return jsonify(rows)


# ============================================================
#  EXPORT LOGS → CSV (Excel) + CLEAR SCANS
# ============================================================
@app.route("/api/export-logs")
def export_logs():
    conn = db()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT s.id, s.scanned_at, s.valid_date, s.slot, s.success, s.message,
               m.name, m.roll_or_id
        FROM scans s
        LEFT JOIN members m ON m.id = s.member_id
        ORDER BY s.scanned_at ASC
        """
    )
    rows = cur.fetchall()

    # Build CSV in memory
    output = []
    header = [
        "Scan ID",
        "Scanned At",
        "Valid Date",
        "Slot",
        "Success",
        "Message",
        "Member Name",
        "Roll / ID",
    ]
    output.append(header)

    for r in rows:
        output.append([
            r["id"],
            str(r.get("scanned_at") or ""),
            str(r.get("valid_date") or ""),
            r.get("slot") or "",
            r.get("success"),
            r.get("message") or "",
            r.get("name") or "",
            r.get("roll_or_id") or "",
        ])

    # Convert to CSV string
    csv_buf = []
    for row in output:
        # naive CSV join (OK for simple data)
        csv_buf.append(",".join([str(x).replace(",", " ") for x in row]))
    csv_data = "\n".join(csv_buf)

    # CLEAR ALL SCANS after export
    cur2 = conn.cursor()
    cur2.execute("DELETE FROM scans")
    conn.commit()
    cur2.close()
    cur.close()
    conn.close()

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=scan_logs.csv"},
    )


# ============================================================
#  RUN SERVER
# ============================================================
if __name__ == "__main__":
    # debug=True for local testing
    app.run(host="0.0.0.0", port=5000, debug=True)












