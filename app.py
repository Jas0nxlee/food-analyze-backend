import os
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify

from db import connect, init_schema

app = Flask(__name__)

DB_READY = False
DB_INIT_ERROR = ""
DB_INIT_AT = 0

def now_ts():
    return int(time.time() * 1000)

def today_str():
    dt = datetime.now(timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d")

def ensure_db_ready():
    global DB_READY, DB_INIT_ERROR, DB_INIT_AT
    if DB_READY:
        return True
    try:
        init_schema()
        DB_READY = True
        DB_INIT_ERROR = ""
        DB_INIT_AT = now_ts()
        return True
    except Exception as e:
        DB_READY = False
        DB_INIT_ERROR = f"{type(e).__name__}: {str(e)}"
        DB_INIT_AT = now_ts()
        return False

def get_openid():
    for k in ["X-WX-OPENID", "x-wx-openid", "X-WX-From-Openid", "x-wx-from-openid", "x-wx-openid"]:
        v = request.headers.get(k)
        if v:
            return v
    return "dev_openid"

def portion_factor(level: str):
    if level == "small":
        return 0.75
    if level == "large":
        return 1.25
    return 1.0

def ensure_seed_food_items(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(1) AS c FROM food_items")
        c = int(cur.fetchone()["c"])
        if c > 0:
            return
        ts = now_ts()
        cur.execute(
            "INSERT INTO food_items (id, name, calories_per_100g, created_at) VALUES (%s,%s,%s,%s),(%s,%s,%s,%s)",
            ("f_gbjd", "宫保鸡丁", 180, ts, "f_rice", "米饭", 116, ts),
        )

@app.route("/api/health", methods=["GET"])
def health():
    ok = ensure_db_ready()
    return jsonify({"ok": ok, "ts": now_ts(), "dbReady": ok, "dbError": DB_INIT_ERROR, "dbInitAt": DB_INIT_AT})

@app.route("/api/todaySummary", methods=["GET"])
def today_summary():
    openid = get_openid()
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503
    d0 = today_str()
    start = int(datetime.strptime(d0, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    end = start + 24 * 60 * 60 * 1000
    conn = connect()
    try:
        ensure_seed_food_items(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT daily_target AS t FROM users WHERE openid=%s", (openid,))
            row = cur.fetchone()
            target = int(row["t"]) if row else 1800
            cur.execute(
                "SELECT COALESCE(SUM(total_calories),0) AS c FROM meals WHERE openid=%s AND occurred_at>= %s AND occurred_at < %s",
                (openid, start, end),
            )
            calories = int(cur.fetchone()["c"])
        remaining = max(target - calories, 0)
        return jsonify({"target": target, "calories": calories, "remaining": remaining})
    finally:
        conn.close()

@app.route("/api/meals", methods=["GET"])
def list_meals():
    openid = get_openid()
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503
    date = request.args.get("date", today_str())
    start = int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    end = start + 24 * 60 * 60 * 1000
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, occurred_at, image_file_id, recognize_id, taste_level, total_calories FROM meals WHERE openid=%s AND occurred_at>= %s AND occurred_at < %s ORDER BY occurred_at DESC",
                (openid, start, end),
            )
            rows = cur.fetchall()
        items = [
            {
                "_id": r["id"],
                "occurredAt": int(r["occurred_at"]),
                "imageFileId": r.get("image_file_id") or "",
                "recognizeId": r.get("recognize_id") or "",
                "tasteLevel": r.get("taste_level") or "normal",
                "totals": {"calories": int(r.get("total_calories") or 0)},
            }
            for r in rows
        ]
        return jsonify({"items": items})
    finally:
        conn.close()

@app.route("/api/meals/<meal_id>", methods=["GET"])
def get_meal(meal_id):
    openid = get_openid()
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, occurred_at, image_file_id, recognize_id, taste_level, total_calories FROM meals WHERE id=%s AND openid=%s",
                (meal_id, openid),
            )
            m = cur.fetchone()
            if not m:
                return jsonify({"error": "not_found"}), 404
            cur.execute(
                "SELECT id, food_item_id, display_name, portion_level, calories FROM meal_items WHERE meal_id=%s AND openid=%s ORDER BY created_at ASC",
                (meal_id, openid),
            )
            items = cur.fetchall()
        meal = {
            "_id": m["id"],
            "occurredAt": int(m["occurred_at"]),
            "imageFileId": m.get("image_file_id") or "",
            "recognizeId": m.get("recognize_id") or "",
            "tasteLevel": m.get("taste_level") or "normal",
            "totals": {"calories": int(m.get("total_calories") or 0)},
        }
        items_out = [
            {
                "_id": it["id"],
                "mealId": meal_id,
                "foodItemId": it.get("food_item_id") or "",
                "displayName": it.get("display_name") or "",
                "portionLevel": it.get("portion_level") or "medium",
                "computed": {"calories": int(it.get("calories") or 0)},
            }
            for it in items
        ]
        return jsonify({"meal": meal, "items": items_out})
    finally:
        conn.close()

@app.route("/api/meals/<meal_id>", methods=["DELETE"])
def delete_meal(meal_id):
    openid = get_openid()
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM meal_items WHERE meal_id=%s AND openid=%s", (meal_id, openid))
            cur.execute("DELETE FROM meals WHERE id=%s AND openid=%s", (meal_id, openid))
        return jsonify({"ok": True})
    finally:
        conn.close()

@app.route("/api/recognize", methods=["POST"])
def recognize():
    data = request.get_json(force=True) or {}
    recognize_id = f"rec_{now_ts()}"
    items = [
        {"name": "宫保鸡丁", "confidence": 0.86, "foodItemId": "f_gbjd", "calorieHint": 180},
        {"name": "米饭", "confidence": 0.74, "foodItemId": "f_rice", "calorieHint": 116},
    ]
    return jsonify({"recognizeId": recognize_id, "items": items})

@app.route("/api/meals", methods=["POST"])
def create_meal():
    openid = get_openid()
    data = request.get_json(force=True) or {}
    occurred_at = int(data.get("occurredAt", now_ts()))
    taste_level = data.get("tasteLevel", "normal")
    confirmed_items = data.get("confirmedItems", [])
    image_file_id = data.get("imageFileId", "")
    recognize_id = data.get("recognizeId", "")
    meal_id = f"m_{now_ts()}"
    created_at = now_ts()
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503
    conn = connect()
    try:
        ensure_seed_food_items(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO meals (id, openid, occurred_at, image_file_id, recognize_id, taste_level, total_calories, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (meal_id, openid, occurred_at, image_file_id, recognize_id, taste_level, 0, created_at),
            )
            total_cal = 0
            for it in confirmed_items:
                fid = it.get("foodItemId") or ""
                pl = it.get("portionLevel") or "medium"
                cur.execute("SELECT name, calories_per_100g FROM food_items WHERE id=%s", (fid,))
                fr = cur.fetchone()
                name = fr["name"] if fr else fid
                base = int(fr["calories_per_100g"]) if fr else 0
                cal = int(round(base * portion_factor(pl)))
                total_cal += cal
                item_id = f"mi_{meal_id}_{fid}"
                cur.execute(
                    "INSERT INTO meal_items (id, meal_id, openid, food_item_id, display_name, portion_level, calories, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (item_id, meal_id, openid, fid, name, pl, cal, created_at),
                )
            cur.execute("UPDATE meals SET total_calories=%s WHERE id=%s AND openid=%s", (total_cal, meal_id, openid))
        return jsonify({"_id": meal_id, "ok": True})
    finally:
        conn.close()

@app.route("/api/reports/month", methods=["GET"])
def report_month():
    openid = get_openid()
    month = request.args.get("month") or datetime.now(timezone.utc).astimezone().strftime("%Y-%m")
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT daily_target AS t FROM users WHERE openid=%s", (openid,))
            row = cur.fetchone()
            target = int(row["t"]) if row else 1800
            cur.execute(
                """
                SELECT
                  DATE_FORMAT(FROM_UNIXTIME(occurred_at/1000), '%%Y-%%m-%%d') AS d,
                  SUM(total_calories) AS c
                FROM meals
                WHERE openid=%s AND DATE_FORMAT(FROM_UNIXTIME(occurred_at/1000), '%%Y-%%m')=%s
                GROUP BY d
                """,
                (openid, month),
            )
            rows = cur.fetchall()
        days_logged = len(rows)
        total = sum(int(r.get("c") or 0) for r in rows)
        avg = int(round(total / days_logged)) if days_logged else 0
        days_over = sum(1 for r in rows if int(r.get("c") or 0) > target)
        advice = []
        if days_over >= 10:
            advice.append("减少油炸和含糖饮料摄入")
        if avg > target:
            advice.append("优先控制晚餐主食和油脂")
        if not advice:
            advice = ["继续保持记录习惯", "三餐规律，控制加餐"]
        return jsonify({"summary": {"avgCalories": avg, "daysLogged": days_logged, "daysOverTarget": days_over}, "advice": {"bullets": advice}})
    finally:
        conn.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
