import os
import pymysql


def _parse_host_port(value: str):
    s = (value or "").strip()
    if not s:
        return "", 0
    if ":" in s:
        host, port = s.rsplit(":", 1)
        try:
            return host.strip(), int(port.strip())
        except Exception:
            return host.strip(), 3306
    return s, 3306


def get_db_config():
    address = os.environ.get("MYSQL_ADDRESS") or os.environ.get("DB_ADDRESS") or ""
    host = os.environ.get("MYSQL_HOST") or os.environ.get("DB_HOST") or ""
    port = os.environ.get("MYSQL_PORT") or os.environ.get("DB_PORT") or ""
    user = os.environ.get("MYSQL_USER") or os.environ.get("MYSQL_USERNAME") or os.environ.get("DB_USER") or os.environ.get("DB_USERNAME") or "root"
    password = (
        os.environ.get("MYSQL_PASSWORD")
        or os.environ.get("MYSQL_ROOT_PASSWORD")
        or os.environ.get("DB_PASSWORD")
        or ""
    )
    database = os.environ.get("MYSQL_DATABASE") or os.environ.get("DB_NAME") or os.environ.get("DB_DATABASE") or "food_analyze"

    if address and not host:
        host, p = _parse_host_port(address)
        if p:
            port = str(p)
    if not port:
        port = "3306"

    return {
        "host": host,
        "port": int(port),
        "user": user,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
    }


def connect():
    cfg = get_db_config()
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg["charset"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
    )


def init_schema():
    cfg = get_db_config()
    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        charset=cfg["charset"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            cur.execute(f"USE `{cfg['database']}`")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  openid VARCHAR(64) PRIMARY KEY,
                  daily_target INT NOT NULL DEFAULT 1800,
                  taste_preference VARCHAR(16) NOT NULL DEFAULT 'normal',
                  gender VARCHAR(8) DEFAULT NULL,
                  age INT DEFAULT NULL,
                  height FLOAT DEFAULT NULL,
                  weight FLOAT DEFAULT NULL,
                  activity_level FLOAT DEFAULT 1.2,
                  created_at BIGINT NOT NULL,
                  updated_at BIGINT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
            
            # Simple migration for existing table
            try:
                cur.execute("SELECT gender FROM users LIMIT 1")
            except Exception:
                cur.execute("ALTER TABLE users ADD COLUMN gender VARCHAR(8) DEFAULT NULL")
                cur.execute("ALTER TABLE users ADD COLUMN age INT DEFAULT NULL")
                cur.execute("ALTER TABLE users ADD COLUMN height FLOAT DEFAULT NULL")
                cur.execute("ALTER TABLE users ADD COLUMN weight FLOAT DEFAULT NULL")
                cur.execute("ALTER TABLE users ADD COLUMN activity_level FLOAT DEFAULT 1.2")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meals (
                  id VARCHAR(64) PRIMARY KEY,
                  openid VARCHAR(64) NOT NULL,
                  occurred_at BIGINT NOT NULL,
                  image_file_id TEXT,
                  recognize_id VARCHAR(128),
                  taste_level VARCHAR(16) NOT NULL DEFAULT 'normal',
                  total_calories INT NOT NULL DEFAULT 0,
                  created_at BIGINT NOT NULL,
                  INDEX idx_meals_openid_occurred (openid, occurred_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meal_items (
                  id VARCHAR(96) PRIMARY KEY,
                  meal_id VARCHAR(64) NOT NULL,
                  openid VARCHAR(64) NOT NULL,
                  food_item_id VARCHAR(64),
                  display_name VARCHAR(128) NOT NULL,
                  portion_level VARCHAR(16) NOT NULL DEFAULT 'medium',
                  calories INT NOT NULL DEFAULT 0,
                  created_at BIGINT NOT NULL,
                  INDEX idx_items_meal (meal_id),
                  INDEX idx_items_openid (openid)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS food_items (
                  id VARCHAR(64) PRIMARY KEY,
                  name VARCHAR(128) NOT NULL,
                  calories_per_100g INT NOT NULL,
                  created_at BIGINT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
    finally:
        conn.close()
