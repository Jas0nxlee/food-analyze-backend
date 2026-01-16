import os
import io
import time
import json
import base64
import urllib.parse
import urllib.request
import requests
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from PIL import Image

from db import connect, init_schema

app = Flask(__name__)

CN_TZ = timezone(timedelta(hours=8))

DB_READY = False
DB_INIT_ERROR = ""
DB_INIT_AT = 0

def now_ts():
    return int(time.time() * 1000)

def today_str():
    dt = datetime.now(CN_TZ)
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

BAIDU_TOKEN = ""
BAIDU_TOKEN_EXPIRES_AT = 0

def baidu_get_access_token():
    global BAIDU_TOKEN, BAIDU_TOKEN_EXPIRES_AT
    now = int(time.time())
    if BAIDU_TOKEN and BAIDU_TOKEN_EXPIRES_AT - now > 60:
        return BAIDU_TOKEN
    api_key = os.environ.get("BAIDU_API_KEY") or ""
    secret_key = os.environ.get("BAIDU_SECRET_KEY") or ""
    if not api_key or not secret_key:
        raise RuntimeError("missing_baidu_credentials")
    qs = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "client_id": api_key, "client_secret": secret_key}
    )
    url = f"https://aip.baidubce.com/oauth/2.0/token?{qs}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    token = data.get("access_token") or ""
    expires_in = int(data.get("expires_in") or 0)
    if not token:
        raise RuntimeError(data.get("error_description") or data.get("error") or "baidu_token_failed")
    BAIDU_TOKEN = token
    BAIDU_TOKEN_EXPIRES_AT = now + max(expires_in, 0)
    return BAIDU_TOKEN

def baidu_dish_recognize_by_url(image_url: str, top_num: int = 5):
    token = baidu_get_access_token()
    url = f"https://aip.baidubce.com/rest/2.0/image-classify/v2/dish?access_token={urllib.parse.quote(token)}"
    body = urllib.parse.urlencode({"url": image_url, "top_num": str(top_num)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if data.get("error_code"):
        raise RuntimeError(f"baidu_error:{data.get('error_code')}:{data.get('error_msg')}")
    return data

def baidu_dish_recognize_by_base64(image_base64: str, top_num: int = 5):
    token = baidu_get_access_token()
    url = f"https://aip.baidubce.com/rest/2.0/image-classify/v2/dish?access_token={urllib.parse.quote(token)}"
    body = urllib.parse.urlencode({"image": image_base64, "top_num": str(top_num)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if data.get("error_code"):
        raise RuntimeError(f"baidu_error:{data.get('error_code')}:{data.get('error_msg')}")
    return data

def fetch_image_as_base64(image_url: str):
    req = urllib.request.Request(
        image_url,
        method="GET",
        headers={
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read()

    # Compress image to avoid Baidu API payload limits (4MB)
    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        
        max_dim = 1024
        w, h = img.size
        if w > max_dim or h > max_dim:
            if w > h:
                new_w = max_dim
                new_h = int(h * (max_dim / w))
            else:
                new_h = max_dim
                new_w = int(w * (max_dim / h))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        raw = buf.getvalue()
    except Exception as e:
        print(f"Image compression warning: {e}")
        # Fallback to original raw data if compression fails

    return base64.b64encode(raw).decode("utf-8")

def map_food_items_by_name(conn, names):
    if not names:
        return {}
    uniq = []
    seen = set()
    for n in names:
        s = str(n or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    if not uniq:
        return {}
    placeholders = ",".join(["%s"] * len(uniq))
    sql = f"SELECT id, name, calories_per_100g FROM food_items WHERE name IN ({placeholders})"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(uniq))
        rows = cur.fetchall()
    out = {}
    for r in rows:
        out[r["name"]] = {"foodItemId": r["id"], "calorieHint": int(r.get("calories_per_100g") or 0)}
    return out

def call_volcengine_ai(prompt: str, system_prompt=None):
    api_key = os.environ.get("VOLC_API_KEY", "").strip()
    model = os.environ.get("VOLC_MODEL", "").strip()
    if not api_key or not model:
        return "AI未配置（请检查VOLC_API_KEY和VOLC_MODEL环境变量）"
    
    url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    sys = system_prompt or "你是专业的营养师。请根据用户的月度饮食记录进行分析。"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7
    }
    
    try:
        print(f"Calling AI... Model: {model}, URL: {url}")
        # Increase timeout to 120s to accommodate slow generation
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        
        if resp.status_code != 200:
            print(f"AI Error: {resp.status_code} - {resp.text}")
            return f"AI调用失败: HTTP {resp.status_code} - {resp.text}"
            
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "AI分析无返回")
    except requests.exceptions.Timeout:
        print("AI Timeout")
        return "AI调用超时（超过120秒），请稍后再试或检查网络。"
    except Exception as e:
        print(f"AI Exception: {e}")
        return f"AI调用失败: {str(e)}"

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
    start = int(datetime.strptime(d0, "%Y-%m-%d").replace(tzinfo=CN_TZ).timestamp() * 1000)
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
    
    date_param = request.args.get("date")
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("pageSize", 20))
    offset = (page - 1) * page_size
    
    conn = connect()
    try:
        with conn.cursor() as cur:
            if date_param:
                start = int(datetime.strptime(date_param, "%Y-%m-%d").replace(tzinfo=CN_TZ).timestamp() * 1000)
                end = start + 24 * 60 * 60 * 1000
                cur.execute(
                    "SELECT id, occurred_at, image_file_id, recognize_id, taste_level, total_calories FROM meals WHERE openid=%s AND occurred_at>= %s AND occurred_at < %s ORDER BY occurred_at DESC",
                    (openid, start, end),
                )
            else:
                cur.execute(
                    "SELECT id, occurred_at, image_file_id, recognize_id, taste_level, total_calories FROM meals WHERE openid=%s ORDER BY occurred_at DESC LIMIT %s OFFSET %s",
                    (openid, page_size, offset),
                )
            rows = cur.fetchall()
            
            # Fetch food names for each meal
            for r in rows:
                cur.execute("SELECT display_name FROM meal_items WHERE meal_id=%s ORDER BY created_at ASC", (r["id"],))
                item_rows = cur.fetchall()
                r["food_names"] = [x["display_name"] for x in item_rows]

        items = [
            {
                "_id": r["id"],
                "occurredAt": int(r["occurred_at"]),
                "imageFileId": r.get("image_file_id") or "",
                "recognizeId": r.get("recognize_id") or "",
                "tasteLevel": r.get("taste_level") or "normal",
                "totals": {"calories": int(r.get("total_calories") or 0)},
                "names": r.get("food_names") or []
            }
            for r in rows
        ]
        return jsonify({"items": items, "hasMore": len(items) >= page_size if not date_param else False})
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
    started = now_ts()
    data = request.get_json(force=True) or {}
    recognize_id = f"rec_{now_ts()}"
    debug_requested = bool(data.get("debug"))
    image_base64 = (data.get("imageBase64") or "").strip()
    image_url = (data.get("imageUrl") or data.get("imageFileId") or "").strip()
    if not image_base64 and not image_url:
        return jsonify({"error": "bad_request", "message": "missing imageBase64 or imageUrl"}), 400
    if image_url.startswith("cloud://"):
        return jsonify({"error": "bad_request", "message": "imageUrl must be http(s) URL"}), 400
    try:
        image_host = urllib.parse.urlparse(image_url).netloc
    except Exception:
        image_host = ""
    try:
        if image_base64:
            r = baidu_dish_recognize_by_base64(image_base64, top_num=5)
            mode = "base64"
        else:
            image_base64_fetched = fetch_image_as_base64(image_url)
            r = baidu_dish_recognize_by_base64(image_base64_fetched, top_num=5)
            mode = "url_fetch"
    except Exception as e:
        msg = str(e) or "recognize_failed"
        return jsonify({"error": "recognize_failed", "message": msg}), 502
    results = r.get("result") or []
    items = []
    names = []
    for x in results:
        name = str(x.get("name") or "").strip()
        prob = x.get("probability")
        
        # Try to extract calorie from Baidu result
        # Baidu returns 'calorie' as string (e.g. "120") or number, per 100g
        try:
            cal_val = float(x.get("calorie") or 0)
        except Exception:
            cal_val = 0.0
            
        try:
            conf = float(prob)
        except Exception:
            conf = 0.0
        if not name:
            continue
        names.append(name)
        items.append({"name": name, "confidence": conf, "foodItemId": "", "calorieHint": int(cal_val)})
    if ensure_db_ready():
        conn = connect()
        try:
            ensure_seed_food_items(conn)
            mapping = map_food_items_by_name(conn, names)
        finally:
            conn.close()
        for it in items:
            m = mapping.get(it["name"])
            if m:
                it["foodItemId"] = m["foodItemId"]
                # Only overwrite if DB has a non-zero value, or maybe trust DB always?
                # Let's trust DB if it has value, otherwise keep Baidu's
                if m["calorieHint"] > 0:
                    it["calorieHint"] = m["calorieHint"]
    return jsonify(
        {
            "recognizeId": recognize_id,
            "items": items,
            "provider": "baidu",
            "mode": mode,
            "imageHost": image_host,
            "costMs": max(now_ts() - started, 0),
            "baiduRaw": r if debug_requested else None,
        }
    )

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
                name_client = (it.get("name") or "").strip() or "未知食物"
                cal_hint = int(it.get("calorieHint") or 0)
                
                cur.execute("SELECT name, calories_per_100g FROM food_items WHERE id=%s", (fid,))
                fr = cur.fetchone()
                name = fr["name"] if fr else name_client
                base = int(fr["calories_per_100g"]) if fr else cal_hint
                
                # Special handling for non-dish items
                if "非菜" in name:
                    cal = 0
                elif base > 0:
                    cal = int(round(base * portion_factor(pl)))
                else:
                    if pl == "small":
                        cal = 100
                    elif pl == "large":
                        cal = 250
                    else:
                        cal = 180
                total_cal += cal
                item_id = f"mi_{meal_id}_{fid}_{now_ts()}" # unique id
                cur.execute(
                    "INSERT INTO meal_items (id, meal_id, openid, food_item_id, display_name, portion_level, calories, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (item_id, meal_id, openid, fid, name, pl, cal, created_at),
                )
            cur.execute("UPDATE meals SET total_calories=%s WHERE id=%s AND openid=%s", (total_cal, meal_id, openid))
        return jsonify({"_id": meal_id, "ok": True})
    finally:
        conn.close()

@app.route("/api/meals/manual", methods=["POST"])
def create_manual_meal():
    openid = get_openid()
    data = request.get_json(force=True) or {}
    
    name = (data.get("name") or "").strip()
    portion_level = data.get("portionLevel") or "medium"
    occurred_at = int(data.get("occurredAt") or now_ts())
    
    if not name:
        return jsonify({"error": "bad_request", "message": "name is required"}), 400
        
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503

    # 1. Call AI to estimate calories
    portion_desc = "中份"
    if portion_level == "small": portion_desc = "小份"
    if portion_level == "large": portion_desc = "大份"
    
    prompt = f"食物：{name}，分量：{portion_desc}。请估算热量（整数，单位kcal）。请务必只返回一个JSON对象，格式为：{{\"calories\": 100, \"reason\": \"估算依据\"}}。不要包含Markdown格式或其他文字。"
    system_prompt = "你是专业的营养师，负责估算食物热量。只输出JSON。"
    
    ai_resp = call_volcengine_ai(prompt, system_prompt=system_prompt)
    print(f"Manual AI Resp: {ai_resp}")
    
    # Parse JSON
    calories = 0
    try:
        # Strip code blocks if present
        clean_resp = ai_resp.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean_resp)
        calories = int(parsed.get("calories") or 0)
    except Exception as e:
        print(f"Failed to parse AI response: {e}")
        # Fallback estimation if AI fails or returns garbage
        if portion_level == "small": calories = 100
        elif portion_level == "large": calories = 300
        else: calories = 200

    # 2. Save to DB
    meal_id = f"m_manual_{now_ts()}"
    created_at = now_ts()
    
    conn = connect()
    try:
        ensure_seed_food_items(conn)
        with conn.cursor() as cur:
            # 幂等性检查：5秒内是否有相同食物名称的记录
            cur.execute(
                """
                SELECT m.id FROM meals m 
                JOIN meal_items mi ON m.id = mi.meal_id 
                WHERE m.openid=%s AND mi.display_name=%s AND m.created_at > %s
                """,
                (openid, name, created_at - 5000)
            )
            existing = cur.fetchone()
            if existing:
                return jsonify({"error": "duplicate", "message": "请勿重复提交", "_id": existing["id"]}), 409
            
            # Insert meal
            cur.execute(
                "INSERT INTO meals (id, openid, occurred_at, image_file_id, recognize_id, taste_level, total_calories, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (meal_id, openid, occurred_at, "", "", "normal", calories, created_at),
            )
            
            # Insert meal item
            item_id = f"mi_{meal_id}_{now_ts()}"
            # Use a dummy food_item_id or check if name exists in food_items?
            # For simplicity, we just use the name directly.
            cur.execute(
                "INSERT INTO meal_items (id, meal_id, openid, food_item_id, display_name, portion_level, calories, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (item_id, meal_id, openid, "", name, portion_level, calories, created_at),
            )
            
        return jsonify({"_id": meal_id, "ok": True, "calories": calories, "aiRaw": ai_resp})
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
        # Try fetch cached analysis
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM monthly_reports WHERE openid=%s AND month=%s", (openid, month))
            cached = cur.fetchone()
        
        ai_analysis = cached["content"] if cached else ""
        
        return jsonify({
            "aiAnalysis": ai_analysis,
            "hasAnalysis": bool(ai_analysis)
        })
    finally:
        conn.close()

def run_async_analysis(openid, month, prompt):
    try:
        print(f"Async analysis started for {openid} - {month}")
        ai_analysis = call_volcengine_ai(prompt)
        
        # Save to cache
        ts = now_ts()
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO monthly_reports (openid, month, content, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE content=%s, created_at=%s
                    """,
                    (openid, month, ai_analysis, ts, ai_analysis, ts)
                )
            print(f"Async analysis completed for {openid} - {month}")
        finally:
            conn.close()
    except Exception as e:
        print(f"Async analysis failed: {e}")
        # Optionally write error to DB so frontend stops polling
        try:
            conn = connect()
            with conn.cursor() as cur:
                err_msg = f"分析失败: {str(e)}"
                cur.execute(
                    "UPDATE monthly_reports SET content=%s WHERE openid=%s AND month=%s",
                    (err_msg, openid, month)
                )
            conn.close()
        except:
            pass

@app.route("/api/reports/month/analyze", methods=["POST"])
def report_month_analyze():
    openid = get_openid()
    data = request.get_json(force=True) or {}
    month = data.get("month") or datetime.now(timezone.utc).astimezone().strftime("%Y-%m")
    
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503
        
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT daily_target AS t FROM users WHERE openid=%s", (openid,))
            row = cur.fetchone()
            target = int(row["t"]) if row else 1800
            
            # Fetch detailed meals for AI
            cur.execute("""
                SELECT id, occurred_at, total_calories FROM meals 
                WHERE openid=%s AND DATE_FORMAT(FROM_UNIXTIME(occurred_at/1000), '%%Y-%%m')=%s
                ORDER BY occurred_at ASC
            """, (openid, month))
            meals = cur.fetchall()
            
            if not meals:
                return jsonify({"aiAnalysis": "本月暂无饮食记录，无法进行分析。", "hasAnalysis": True})
            
            meal_ids = [m["id"] for m in meals]
            meal_items_map = {}
            if meal_ids:
                format_strings = ','.join(['%s'] * len(meal_ids))
                cur.execute(f"SELECT meal_id, display_name FROM meal_items WHERE meal_id IN ({format_strings})", tuple(meal_ids))
                items = cur.fetchall()
                for it in items:
                    mid = it["meal_id"]
                    if mid not in meal_items_map: meal_items_map[mid] = []
                    meal_items_map[mid].append(it["display_name"])

        # AI Analysis Prompt Construction
        report_text = f"用户每日目标热量：{target} kcal\n本月饮食记录：\n"
        for m in meals:
            dt = datetime.fromtimestamp(m["occurred_at"]/1000, CN_TZ).strftime("%Y-%m-%d %H:%M")
            foods = ",".join(meal_items_map.get(m["id"], []))
            report_text += f"- {dt}: 此餐{m['total_calories']}kcal, 食物: {foods}\n"
        
        prompt = f"""
        {report_text}
        
        请根据以上数据进行分析：
        1. 统计达标天数（<= {target}）和超标天数。
        2. 计算日均热量摄取。
        3. 给出具体的饮食建议及注意事项。
        请直接返回分析结果，使用Markdown格式。
        """
        
        # Set status to ANALYZING
        ts = now_ts()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO monthly_reports (openid, month, content, created_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE content=%s, created_at=%s
                """,
                (openid, month, "__ANALYZING__", ts, "__ANALYZING__", ts)
            )
            
        # Start background thread
        thread = threading.Thread(target=run_async_analysis, args=(openid, month, prompt))
        thread.start()
        
        return jsonify({"aiAnalysis": "__ANALYZING__", "hasAnalysis": True})
    finally:
        conn.close()

@app.route("/api/user/profile", methods=["GET"])
def get_user_profile():
    openid = get_openid()
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT daily_target, taste_preference, gender, age, height, weight, activity_level FROM users WHERE openid=%s",
                (openid,),
            )
            row = cur.fetchone()
            if not row:
                # Create default user if not exists
                ts = now_ts()
                cur.execute(
                    "INSERT INTO users (openid, daily_target, taste_preference, created_at, updated_at) VALUES (%s, %s, %s, %s, %s)",
                    (openid, 1800, "normal", ts, ts),
                )
                row = {
                    "daily_target": 1800,
                    "taste_preference": "normal",
                    "gender": None,
                    "age": None,
                    "height": None,
                    "weight": None,
                    "activity_level": 1.2,
                }
        
        return jsonify({
            "dailyTarget": int(row.get("daily_target") or 1800),
            "tastePreference": row.get("taste_preference") or "normal",
            "gender": row.get("gender"),
            "age": row.get("age"),
            "height": row.get("height"),
            "weight": row.get("weight"),
            "activityLevel": row.get("activity_level") or 1.2,
            "isProfileCompleted": bool(row.get("height") and row.get("weight") and row.get("age") and row.get("gender"))
        })
    finally:
        conn.close()

@app.route("/api/user/profile", methods=["POST"])
def update_user_profile():
    openid = get_openid()
    data = request.get_json(force=True) or {}
    
    if not ensure_db_ready():
        return jsonify({"error": "db_unavailable", "message": DB_INIT_ERROR}), 503

    gender = data.get("gender") # 'male' or 'female'
    try:
        age = int(data.get("age"))
        height = float(data.get("height"))
        weight = float(data.get("weight"))
        activity_level = float(data.get("activityLevel") or 1.2)
    except (TypeError, ValueError):
        return jsonify({"error": "bad_request", "message": "Invalid numeric values for age, height, or weight"}), 400

    # Calculate BMR (Mifflin-St Jeor Equation)
    # Men: 10W + 6.25H - 5A + 5
    # Women: 10W + 6.25H - 5A - 161
    bmr = 10 * weight + 6.25 * height - 5 * age
    if gender == "male":
        bmr += 5
    else:
        bmr -= 161
    
    daily_target = int(bmr * activity_level)
    
    # Update DB
    ts = now_ts()
    conn = connect()
    try:
        with conn.cursor() as cur:
            # Check if user exists
            cur.execute("SELECT 1 FROM users WHERE openid=%s", (openid,))
            if not cur.fetchone():
                 cur.execute(
                    "INSERT INTO users (openid, daily_target, taste_preference, gender, age, height, weight, activity_level, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (openid, daily_target, "normal", gender, age, height, weight, activity_level, ts, ts),
                )
            else:
                cur.execute(
                    """
                    UPDATE users 
                    SET daily_target=%s, gender=%s, age=%s, height=%s, weight=%s, activity_level=%s, updated_at=%s 
                    WHERE openid=%s
                    """,
                    (daily_target, gender, age, height, weight, activity_level, ts, openid)
                )
        
        return jsonify({
            "ok": True,
            "dailyTarget": daily_target,
            "bmr": int(bmr)
        })
    finally:
        conn.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
