# app.py - Flask 后端：接收用户消息 -> 调用 DeepSeek -> 返回结构化 JSON
# 启动:  python app.py   然后浏览器打开 http://127.0.0.1:5000
# 依赖:  pip install -r requirements.txt
# 配置:  把 DeepSeek API Key 和 阿里云 OSS 凭证填到 .env 文件

import os
import re
import json
import uuid
import time
import random
import threading
from functools import wraps
import requests
import pymysql
try:
    import redis  # 可选依赖：未安装时仅停用多轮上下文，不影响程序启动和其他功能
    _REDIS_MODULE_OK = True
except ImportError:
    redis = None
    _REDIS_MODULE_OK = False
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template, redirect, session, url_for, g
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

# 加载 .env 环境变量
load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
# session 加密签名密钥（从 .env 读取，生产环境务必修改）
app.secret_key = os.getenv("SECRET_KEY", "aichat-dev-secret-key-please-change")
app.permanent_session_lifetime = timedelta(days=7)


@app.after_request
def _no_cache_html(resp):
    # HTML 页面不缓存：避免代码更新后浏览器仍使用旧的登录页/主页
    if resp.headers.get("Content-Type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

# ======== 数据库配置（支持 MySQL / PostgreSQL，由 DATABASE_SEL 选择，默认 pgsql） ========
DATABASE_SEL = os.getenv("DATABASE_SEL", "pgsql").strip().lower()

# MySQL
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "123456")
MYSQL_DB = os.getenv("MYSQL_DB", "aichat")

# PostgreSQL
PG_HOST = os.getenv("PG_HOST", "127.0.0.1")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "123456")
PG_DB = os.getenv("PG_DB", "aichat")

# 迁移时需要复制的表（库名、表名、字段在两种数据库中保持一致）
MIGRATE_TABLES = ["user_account", "certificate", "user_question_record"]

# ======== Redis 配置（对话上下文缓存） ========
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "") or None
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
CONTEXT_TTL_SECONDS = 3600   # 上下文保留时长：1 小时（每次问答滑动续期）
CONTEXT_MAX_ROUNDS = 5       # 调用大模型时携带的最近问答轮数
# 历史中每轮 assistant 回复送入模型的最大长度。
# 原因：DeepSeek 在 response_format=json_object 下，历史里若存在很长的“纯文本”
# assistant 消息，会触发其约束解码缺陷，content 只返回一串空格（finish_reason=stop）。
# 历史只需保留语义要点，限长 + 以 JSON 对象形式回传可规避该问题。
HIST_ANSWER_LIMIT = 300

# ======== DeepSeek 配置 ========
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# ======== 对象存储配置（OSS / COS 启动时二选一） ========
# STORAGE_PROVIDER=oss 强制用阿里云 OSS；=cos 强制用腾讯云 COS；
# =auto（默认）在两套都配置齐全时随机选一套（负载分担），只配置一套则用那一套。
# 选定后整个进程固定使用，保证文件列表/删除/下载一致。
STORAGE_PROVIDER = os.getenv("STORAGE_PROVIDER", "auto").lower()
# 存储凭证来源：=db（小写）时四个密钥从 MySQL certificate 表读取，忽略环境变量；
# 其他值（默认 env）沿用环境变量，行为不变
STORAGE_CERT_SEL = os.getenv("STORAGE_CERT_SEL", "env").strip().lower()

# 阿里云 OSS
OSS_ACCESS_KEY_ID = os.getenv("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("OSS_ACCESS_KEY_SECRET", "")
OSS_ENDPOINT = os.getenv("OSS_ENDPOINT", "")         # 例如: oss-cn-hangzhou.aliyuncs.com
OSS_BUCKET_NAME = os.getenv("OSS_BUCKET_NAME", "")
OSS_KEY_PREFIX = os.getenv("OSS_KEY_PREFIX", "chat/")  # 上传到 OSS 的目录前缀

# 腾讯云 COS
COS_SECRET_ID = os.getenv("COS_SECRET_ID", "")
COS_SECRET_KEY = os.getenv("COS_SECRET_KEY", "")
COS_REGION = os.getenv("COS_REGION", "")             # 例如: ap-shanghai
COS_BUCKET_NAME = os.getenv("COS_BUCKET_NAME", "")   # 格式: <BucketName-APPID>
COS_KEY_PREFIX = os.getenv("COS_KEY_PREFIX", "chat/")

# 允许上传的扩展名（白名单），逗号分隔
ALLOWED_EXTS = {e.lower() for e in os.getenv(
    "ALLOWED_EXTS", "jpg,jpeg,png,gif,webp,bmp,pdf,txt,md,doc,docx,xls,xlsx,ppt,pptx,csv"
).split(",") if e.strip()}
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(10 * 1024 * 1024)))  # 默认 10MB

# ======== 操作日志（每周一个文件，文件名为该周周一日期） ========
# 格式: 2026-09-27 12:00:00 | 127.0.0.1 | UPLOAD | {"filename": "xx", "size": 123}
LOG_DIR = "logs"
_log_lock = threading.Lock()


def _client_ip():
    """客户端 IP：部署在 nginx 等反代后时优先取 X-Forwarded-For"""
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (request.remote_addr or "unknown")


def write_log(action, ip, content):
    """按周写操作日志：logs/<本周周一日期>.log，跨周自动切换新文件"""
    try:
        now = datetime.now()
        # weekday(): 周一=0 ... 周日=6，减去偏移即得本周周一日期
        monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        line = f"{now.strftime('%Y-%m-%d %H:%M:%S')} | {ip} | {action} | {json.dumps(content, ensure_ascii=False)}"
        os.makedirs(LOG_DIR, exist_ok=True)
        with _log_lock:
            with open(os.path.join(LOG_DIR, f"{monday}.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"[ERROR] 写日志失败: {e}")


def _iso8601_to_ts(s):
    """COS 返回的 LastModified 是 ISO 8601 字符串（UTC），转成 Unix 秒，与 OSS 口径一致"""
    try:
        return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


# ======== 存储凭证解析（环境变量 / MySQL certificate 表二选一） ========
_CERT_KEYS = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "COS_SECRET_ID", "COS_SECRET_KEY")
_db_certs_cache = None


def _load_storage_certs():
    """返回四个存储密钥（dict）。
    STORAGE_CERT_SEL=db 时从 MySQL certificate 表读取（certificatekey/value），
    进程内只查一次；否则直接用环境变量，保持原有行为。
    endpoint/region/bucket 等非密钥配置始终来自环境变量。
    """
    global _db_certs_cache
    if STORAGE_CERT_SEL != "db":
        return {
            "OSS_ACCESS_KEY_ID": OSS_ACCESS_KEY_ID,
            "OSS_ACCESS_KEY_SECRET": OSS_ACCESS_KEY_SECRET,
            "COS_SECRET_ID": COS_SECRET_ID,
            "COS_SECRET_KEY": COS_SECRET_KEY,
        }

    if _db_certs_cache is None:
        _db_certs_cache = {}
        db_name = MYSQL_DB if DATABASE_SEL == "mysql" else PG_DB
        try:
            conn = db_connect(database=db_name)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT certificatekey, value FROM certificate "
                        "WHERE certificatekey IN (%s, %s, %s, %s)",
                        _CERT_KEYS,
                    )
                    for row in cur.fetchall():
                        _db_certs_cache[row["certificatekey"]] = row["value"] or ""
            finally:
                conn.close()
            print(f"[INFO] 存储凭证来源: {DATABASE_SEL} {db_name}.certificate 表（STORAGE_CERT_SEL=db）")
        except Exception as e:
            print(f"[ERROR] 从 {DATABASE_SEL} certificate 表读取存储凭证失败: {e}（文件功能将不可用）")
    return _db_certs_cache


class OssStorage:
    """阿里云 OSS 封装，对外提供统一的存储接口"""
    prefix = OSS_KEY_PREFIX

    def __init__(self, key_id=None, key_secret=None):
        import oss2
        key_id = key_id if key_id is not None else OSS_ACCESS_KEY_ID
        key_secret = key_secret if key_secret is not None else OSS_ACCESS_KEY_SECRET
        auth = oss2.Auth(key_id, key_secret)
        self._bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET_NAME)

    def put_object(self, key, data):
        self._bucket.put_object(key, data)

    def delete_object(self, key):
        self._bucket.delete_object(key)

    def sign_url(self, key, expires=3600, params=None):
        return self._bucket.sign_url("GET", key, expires, params=params)

    def list_objects(self, prefix, marker="", max_keys=100):
        """返回 (items, is_truncated, next_marker)，item 为统一格式的 dict"""
        r = self._bucket.list_objects(prefix=prefix, marker=marker, max_keys=max_keys)
        items = [
            {"key": o.key, "size": o.size, "last_modified": int(o.last_modified)}
            for o in r.object_list
            if not o.key.endswith("/")  # 跳过目录占位符
        ]
        return items, r.is_truncated, (r.next_marker if r.is_truncated else "")


class CosStorage:
    """腾讯云 COS 封装，与 OssStorage 保持完全相同的对外接口"""
    prefix = COS_KEY_PREFIX

    def __init__(self, secret_id=None, secret_key=None):
        from qcloud_cos import CosConfig, CosS3Client
        secret_id = secret_id if secret_id is not None else COS_SECRET_ID
        secret_key = secret_key if secret_key is not None else COS_SECRET_KEY
        cfg = CosConfig(Region=COS_REGION, SecretId=secret_id, SecretKey=secret_key)
        self._client = CosS3Client(cfg)

    def put_object(self, key, data):
        self._client.put_object(Bucket=COS_BUCKET_NAME, Body=data, Key=key)

    def delete_object(self, key):
        self._client.delete_object(Bucket=COS_BUCKET_NAME, Key=key)

    def sign_url(self, key, expires=3600, params=None):
        kwargs = {"Bucket": COS_BUCKET_NAME, "Key": key, "Expired": expires}
        if params:
            kwargs["Params"] = params
        return self._client.get_presigned_download_url(**kwargs)

    def list_objects(self, prefix, marker="", max_keys=100):
        kwargs = {"Bucket": COS_BUCKET_NAME, "Prefix": prefix, "MaxKeys": max_keys}
        if marker:
            kwargs["Marker"] = marker
        r = self._client.list_objects(**kwargs)
        items = [
            {"key": o["Key"], "size": int(o["Size"]),
             "last_modified": _iso8601_to_ts(o.get("LastModified", ""))}
            for o in r.get("Contents", [])
            if not o["Key"].endswith("/")
        ]
        truncated = str(r.get("IsTruncated", "false")).lower() == "true"
        return items, truncated, (r.get("NextMarker", "") if truncated else "")


def _oss_configured(certs):
    return all([certs.get("OSS_ACCESS_KEY_ID"), certs.get("OSS_ACCESS_KEY_SECRET"),
                OSS_ENDPOINT, OSS_BUCKET_NAME])


def _cos_configured(certs):
    return all([certs.get("COS_SECRET_ID"), certs.get("COS_SECRET_KEY"),
                COS_REGION, COS_BUCKET_NAME])


# 延迟初始化存储实例：进程内只初始化一次，之后始终使用同一套
_storage = None

def get_storage():
    """按 STORAGE_PROVIDER 选择并初始化一套对象存储（OSS 或 COS），不可用返回 None"""
    global _storage
    if _storage is not None:
        return _storage

    # 密钥统一从 _load_storage_certs() 取（db 模式来自 MySQL，env 模式来自环境变量）
    certs = _load_storage_certs()

    provider = STORAGE_PROVIDER
    if provider == "auto":
        # auto：收集配置齐全的存储，都齐全时随机选一套实现负载分担
        candidates = []
        if _oss_configured(certs):
            candidates.append("oss")
        if _cos_configured(certs):
            candidates.append("cos")
        provider = random.choice(candidates) if candidates else ""

    if provider == "oss":
        if not _oss_configured(certs):
            print("[ERROR] STORAGE_PROVIDER=oss 但 OSS 配置不完整")
            return None
        cls = OssStorage
    elif provider == "cos":
        if not _cos_configured(certs):
            print("[ERROR] STORAGE_PROVIDER=cos 但 COS 配置不完整")
            return None
        cls = CosStorage
    else:
        if provider:
            print(f"[ERROR] 未知的 STORAGE_PROVIDER: {provider}（可选: oss / cos / auto）")
        return None

    try:
        if provider == "oss":
            _storage = cls(certs["OSS_ACCESS_KEY_ID"], certs["OSS_ACCESS_KEY_SECRET"])
        else:
            _storage = cls(certs["COS_SECRET_ID"], certs["COS_SECRET_KEY"])
        print(f"[INFO] 对象存储已连接: {provider.upper()}，目录前缀 {cls.prefix}")
        return _storage
    except ImportError as e:
        # 选中的存储 SDK 未安装：如果另一套配置齐全，自动回退到那一套
        other_provider = "cos" if provider == "oss" else "oss"
        other_ok = _cos_configured(certs) if other_provider == "cos" else _oss_configured(certs)
        other_cls = CosStorage if other_provider == "cos" else OssStorage
        if other_ok:
            print(f"[WARN] {provider.upper()} SDK 未安装（{e}），自动回退到 {other_provider.upper()}")
            try:
                if other_provider == "oss":
                    _storage = other_cls(certs["OSS_ACCESS_KEY_ID"], certs["OSS_ACCESS_KEY_SECRET"])
                else:
                    _storage = other_cls(certs["COS_SECRET_ID"], certs["COS_SECRET_KEY"])
                print(f"[INFO] 对象存储已连接（回退）: {other_provider.upper()}，目录前缀 {other_cls.prefix}")
                return _storage
            except Exception as e2:
                print(f"[ERROR] 对象存储初始化失败: {e2}")
                return None
        else:
            print(f"[ERROR] {provider.upper()} SDK 未安装（{e}），且 {other_provider.upper()} 未配置，文件功能不可用。"
                  f"请执行: pip install {'cos-python-sdk-v5' if provider == 'cos' else 'oss2'}")
            return None
    except Exception as e:
        print(f"[ERROR] 对象存储初始化失败: {e}")
        return None


# 系统提示词：强制模型返回结构化 JSON
SYSTEM_PROMPT = """你是一个乐于助人的中文对话助手。
如果用户附带了文件链接，请把链接当作参考资料，但不要在 reply 中原样输出 URL。
对用户的每一次输入，你必须返回严格的 JSON 对象，字段如下：
{
  "reply": "你对用户的回复正文（中文，自然口语化）",
  "intent": "用户意图的简短分类（如：提问/闲聊/请求/感谢/其他）",
  "confidence": "你对意图判断的置信度，0 到 1 之间的浮点数",
  "keywords": ["从用户输入中提取的关键词，最多 3 个"]
}
只返回 JSON，不要任何额外文字、不要 markdown 代码块标记。
"""


# ======== 数据库适配层（MySQL / PostgreSQL，由 DATABASE_SEL 选择） ========
# 业务 SQL 全部使用 %s 占位符（pymysql 与 psycopg2 均支持），游标均为字典行，
# 因此上层路由代码无需感知底层数据库类型。

# 各表建表 DDL（字段名/类型在两种数据库中保持一致，按方言分别声明）
_DDL_MYSQL = [
    """CREATE TABLE IF NOT EXISTS user_account (
        username VARCHAR(64) NOT NULL PRIMARY KEY,
        valid TINYINT NOT NULL DEFAULT 1,
        password VARCHAR(255) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS certificate (
        certificatekey VARCHAR(64) NOT NULL PRIMARY KEY,
        value VARCHAR(128) NULL,
        comment VARCHAR(1024) NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS user_question_record (
        username VARCHAR(64) NOT NULL,
        record_idx INT NOT NULL,
        time VARCHAR(128) NOT NULL,
        question VARCHAR(1024) NOT NULL,
        answer VARCHAR(1024) NULL,
        PRIMARY KEY (username, record_idx)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

_DDL_PG = [
    """CREATE TABLE IF NOT EXISTS user_account (
        username VARCHAR(64) NOT NULL PRIMARY KEY,
        valid SMALLINT NOT NULL DEFAULT 1,
        password VARCHAR(255) NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS certificate (
        certificatekey VARCHAR(64) NOT NULL PRIMARY KEY,
        value VARCHAR(128),
        comment VARCHAR(1024)
    )""",
    """CREATE TABLE IF NOT EXISTS user_question_record (
        username VARCHAR(64) NOT NULL,
        record_idx INTEGER NOT NULL,
        time VARCHAR(128) NOT NULL,
        question VARCHAR(1024) NOT NULL,
        answer VARCHAR(1024),
        PRIMARY KEY (username, record_idx)
    )""",
]


def connect_mysql(database=None, dict_cursor=True):
    """建立 MySQL 连接；database 为 None 时不指定库（用于建库）"""
    kwargs = {"host": MYSQL_HOST, "port": MYSQL_PORT, "user": MYSQL_USER,
              "password": MYSQL_PASSWORD, "charset": "utf8mb4", "autocommit": True}
    if database:
        kwargs["database"] = database
    if dict_cursor:
        kwargs["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(**kwargs)


def connect_pg(database=None):
    """建立 PostgreSQL 连接（默认 RealDictCursor 字典游标，autocommit）"""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    kwargs = {"host": PG_HOST, "port": PG_PORT, "user": PG_USER,
              "password": PG_PASSWORD, "cursor_factory": RealDictCursor}
    if database:
        kwargs["dbname"] = database
    conn = psycopg2.connect(**kwargs)
    conn.autocommit = True
    return conn


def db_connect(database=None, backend=None):
    """按 DATABASE_SEL（或显式 backend）连接当前数据库；database=None 时连维护库"""
    backend = (backend or DATABASE_SEL).lower()
    if backend == "mysql":
        return connect_mysql(database=database)
    if backend == "pgsql":
        return connect_pg(database=database)
    raise ValueError(f"不支持的 DATABASE_SEL: {backend}（可选 mysql / pgsql）")


# werkzeug 3.x 默认 scrypt 哈希长度约 170+ 字符，旧表 password varchar(128) 存不下，
# 启动时检查并扩容到 255（幂等，长度足够则不执行 ALTER）
_PASSWORD_MIN_LEN = 255


def _ensure_password_column(cur, backend, db_name):
    if backend == "mysql":
        cur.execute(
            "SELECT CHARACTER_MAXIMUM_LENGTH AS l FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'user_account' AND COLUMN_NAME = 'password'",
            (db_name,),
        )
    else:
        cur.execute(
            "SELECT character_maximum_length AS l FROM information_schema.columns "
            "WHERE table_name = 'user_account' AND column_name = 'password'"
        )
    row = cur.fetchone()
    length = row["l"] if row else None
    if length is not None and length < _PASSWORD_MIN_LEN:
        if backend == "mysql":
            cur.execute(f"ALTER TABLE user_account MODIFY password VARCHAR({_PASSWORD_MIN_LEN}) NOT NULL")
        else:
            cur.execute(f"ALTER TABLE user_account ALTER COLUMN password TYPE VARCHAR({_PASSWORD_MIN_LEN})")
        print(f"[INFO] 已将 user_account.password 列扩容为 VARCHAR({_PASSWORD_MIN_LEN})")


def init_db():
    """启动时调用：自动建库建表；user_account 为空时创建默认管理员 admin/admin123"""
    backend = DATABASE_SEL.lower()
    if backend == "mysql":
        conn = connect_mysql(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DB}` "
                    f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                cur.execute(f"USE `{MYSQL_DB}`")
                for ddl in _DDL_MYSQL:
                    cur.execute(ddl)
                _ensure_password_column(cur, "mysql", MYSQL_DB)
                cur.execute("SELECT COUNT(*) AS c FROM user_account")
                cnt = cur.fetchone()["c"]
                if cnt == 0:
                    cur.execute(
                        "INSERT INTO user_account (username, valid, password) VALUES (%s, 1, %s)",
                        ("admin", generate_password_hash("admin123")),
                    )
                    print("[INFO] user_account 表为空，已创建默认管理员: admin / admin123（请尽快登录并修改密码）")
        finally:
            conn.close()

    elif backend == "pgsql":
        # PostgreSQL 的 CREATE DATABASE 不支持 IF NOT EXISTS，先查 pg_database
        admin_conn = connect_pg(database="postgres")
        try:
            with admin_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PG_DB,))
                if cur.fetchone() is None:
                    # 库名来自服务端配置，非用户输入；PG 不允许对库名使用参数占位符
                    cur.execute(f'CREATE DATABASE "{PG_DB}"')
                    print(f"[INFO] PostgreSQL 数据库不存在，已创建: {PG_DB}")
        finally:
            admin_conn.close()

        conn = connect_pg(database=PG_DB)
        try:
            with conn.cursor() as cur:
                for ddl in _DDL_PG:
                    cur.execute(ddl)
                _ensure_password_column(cur, "pgsql", PG_DB)
                cur.execute("SELECT COUNT(*) AS c FROM user_account")
                cnt = cur.fetchone()["c"]
                if cnt == 0:
                    cur.execute(
                        "INSERT INTO user_account (username, valid, password) VALUES (%s, 1, %s)",
                        ("admin", generate_password_hash("admin123")),
                    )
                    print("[INFO] user_account 表为空，已创建默认管理员: admin / admin123（请尽快登录并修改密码）")
        finally:
            conn.close()
    else:
        raise ValueError(f"不支持的 DATABASE_SEL: {backend}（可选 mysql / pgsql）")


def get_db():
    """每个请求复用一个数据库连接，请求结束时由 teardown 关闭"""
    conn = getattr(g, "_db", None)
    if conn is None:
        conn = db_connect(database=MYSQL_DB if DATABASE_SEL == "mysql" else PG_DB)
        g._db = conn
    return conn


@app.teardown_appcontext
def _close_db(exc):
    conn = getattr(g, "_db", None)
    if conn is not None:
        conn.close()


# ======== MySQL -> PostgreSQL 一次性数据迁移（登录页按钮触发） ========
def _mysql_type_to_pg(data_type, length, precision, scale):
    """按 MySQL INFORMATION_SCHEMA 的类型信息映射为 PostgreSQL 列类型"""
    t = (data_type or "").lower()
    if t in ("varchar",):
        return f"varchar({int(length)})"
    if t == "char":
        return f"char({int(length)})"
    if t == "tinyint":
        return "smallint"
    if t in ("smallint", "mediumint", "int", "integer"):
        return "integer"
    if t == "bigint":
        return "bigint"
    if t in ("datetime", "timestamp"):
        return "timestamp"
    if t == "date":
        return "date"
    if t == "time":
        return "time"
    if t in ("tinytext", "text", "mediumtext", "longtext"):
        return "text"
    if t in ("decimal", "numeric"):
        return f"decimal({int(precision)},{int(scale or 0)})"
    if t == "float":
        return "real"
    if t in ("double",):
        return "double precision"
    return "text"  # 未知类型兜底为 text，避免迁移中断


def migrate_mysql_to_pgsql():
    """把 MySQL 数据完整复制到 PostgreSQL：
    1) PG 中不存在目标库则自动创建（库名同 MySQL，默认 aichat）；
    2) 按 MySQL 实际表结构（字段名/类型/可空/默认值/主键）在 PG 建同名表；
    3) 清空目标表后全量复制数据。返回每张表复制行数的报告。
    """
    from psycopg2.extras import execute_values

    # 1. 确保 PG 目标数据库存在
    database_created = False
    admin_conn = connect_pg(database="postgres")
    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PG_DB,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{PG_DB}"')
                database_created = True
    finally:
        admin_conn.close()

    # 2. 读 MySQL 表结构 -> PG 建表 -> 复制数据
    my_meta = connect_mysql(database=MYSQL_DB)              # 字典游标：读元数据
    my_data = connect_mysql(database=MYSQL_DB, dict_cursor=False)  # 元组游标：读数据
    pg = connect_pg(database=PG_DB)
    report = {"database_created": database_created, "tables": {}}
    try:
        with my_meta.cursor() as mcur, my_data.cursor() as dcur, pg.cursor() as pcur:
            # 只迁移 MIGRATE_TABLES 中实际存在的表
            mcur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN (%s, %s, %s)",
                (MYSQL_DB, *MIGRATE_TABLES),
            )
            existing = [r["TABLE_NAME"] for r in mcur.fetchall()]

            for table in existing:
                # 列信息
                mcur.execute(
                    """SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                              NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE, COLUMN_DEFAULT
                       FROM INFORMATION_SCHEMA.COLUMNS
                       WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                       ORDER BY ORDINAL_POSITION""",
                    (MYSQL_DB, table),
                )
                columns = mcur.fetchall()
                # 主键列
                mcur.execute(
                    """SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                       WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY'
                       ORDER BY ORDINAL_POSITION""",
                    (MYSQL_DB, table),
                )
                pk_cols = [r["COLUMN_NAME"] for r in mcur.fetchall()]

                col_defs = []
                for c in columns:
                    pg_type = _mysql_type_to_pg(c["DATA_TYPE"], c["CHARACTER_MAXIMUM_LENGTH"],
                                                c["NUMERIC_PRECISION"], c["NUMERIC_SCALE"])
                    d = f'"{c["COLUMN_NAME"]}" {pg_type}'
                    d += "" if c["IS_NULLABLE"] == "YES" else " NOT NULL"
                    default = c["COLUMN_DEFAULT"]
                    if default is not None:
                        if str(default).upper() == "CURRENT_TIMESTAMP":
                            d += " DEFAULT CURRENT_TIMESTAMP"
                        elif str(default).lstrip("-").isdigit():
                            d += f" DEFAULT {default}"
                        else:
                            d += " DEFAULT '" + str(default).replace("'", "''") + "'"
                    col_defs.append(d)
                if pk_cols:
                    col_defs.append("PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk_cols) + ")")

                create_sql = f'CREATE TABLE IF NOT EXISTS "{table}" (\n  ' + ",\n  ".join(col_defs) + "\n)"
                pcur.execute(create_sql)

                # 清空目标表后全量复制（表间无外键，可直接 TRUNCATE）
                pcur.execute(f'TRUNCATE TABLE "{table}"')
                dcur.execute(f"SELECT * FROM `{table}`")
                col_names = [desc[0] for desc in dcur.description]
                rows = dcur.fetchall()
                if rows:
                    cols_sql = ", ".join(f'"{c}"' for c in col_names)
                    execute_values(
                        pcur,
                        f'INSERT INTO "{table}" ({cols_sql}) VALUES %s',
                        rows,
                        page_size=500,
                    )
                report["tables"][table] = len(rows)

            # 迁移后确保 password 列能容纳 werkzeug 哈希（旧表可能是 varchar(128)）
            if "user_account" in existing:
                _ensure_password_column(pcur, "pgsql", PG_DB)
    finally:
        my_meta.close()
        my_data.close()
        pg.close()
    return report


@app.route("/api/migrate/mysql-to-pg", methods=["POST"])
def migrate_to_pg():
    """登录页触发：把 MySQL 数据导入 PostgreSQL（无需登录，仅限本机运维使用）"""
    try:
        report = migrate_mysql_to_pgsql()
    except Exception as e:
        print(f"[ERROR] MySQL -> PostgreSQL 迁移失败: {e}")
        write_log("DB_MIGRATE_FAIL", _client_ip(), {"error": str(e)})
        return jsonify({"error": f"迁移失败: {str(e)}"}), 500
    write_log("DB_MIGRATE", _client_ip(), report)
    return jsonify({"ok": True, "report": report})


def verify_password(stored, plain):
    """兼容两种存储：werkzeug 哈希（pbkdf2/scrypt 前缀）与历史明文密码"""
    if stored and stored.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored, plain)
    return stored == plain


def login_required(view):
    """登录保护：页面请求未登录跳转 /login；API 请求返回 401 JSON"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录或登录已过期", "login_required": True}), 401
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)
    return wrapped


# ======== 登录 / 登出 ========
@app.route("/login")
def login_page():
    if session.get("username"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def login():
    """校验用户名密码 -> 写 session"""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "请输入用户名和密码"}), 400

    try:
        with get_db().cursor() as cur:
            cur.execute("SELECT username, password, valid FROM user_account WHERE username = %s", (username,))
            row = cur.fetchone()
    except Exception as e:
        print(f"[ERROR] 数据库查询失败: {e}")
        return jsonify({"error": "数据库连接失败，请联系管理员"}), 500

    # valid=0 表示账号被停用；密码兼容 werkzeug 哈希和历史明文
    if not row or not row.get("valid", 1) or not verify_password(row["password"], password):
        write_log("LOGIN_FAIL", _client_ip(), {"username": username})
        return jsonify({"error": "用户名或密码错误"}), 401

    session.clear()
    session.permanent = True
    session["username"] = row["username"]
    # 每次登录生成独立的会话 ID：Redis 上下文按 用户名+sessionid 隔离，
    # 同一账号多次登录（或多端登录）各自维护独立的对话上下文
    session["sid"] = uuid.uuid4().hex
    write_log("LOGIN", _client_ip(), {"username": username})
    return jsonify({"ok": True, "username": username})


@app.route("/api/register", methods=["POST"])
def register():
    """注册新用户 -> 写入 user_account（密码 werkzeug 哈希存储）；用户名冲突返回 409"""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "请输入用户名和密码"}), 400
    if not (3 <= len(username) <= 64):
        return jsonify({"error": "用户名长度需为 3-64 个字符"}), 400
    if not (6 <= len(password) <= 128):
        return jsonify({"error": "密码长度需为 6-128 个字符"}), 400

    try:
        with get_db().cursor() as cur:
            # 先查重，给用户明确提示
            cur.execute("SELECT 1 FROM user_account WHERE username = %s", (username,))
            if cur.fetchone():
                write_log("REGISTER_FAIL", _client_ip(), {"username": username, "reason": "duplicate"})
                return jsonify({"error": f"添加失败：用户名「{username}」已存在"}), 409
            try:
                cur.execute(
                    "INSERT INTO user_account (username, valid, password) VALUES (%s, 1, %s)",
                    (username, generate_password_hash(password)),
                )
            except Exception as e:
                # 并发注册时的主键冲突兜底（MySQL 1062 / PG unique violation）
                msg = str(e).lower()
                if "duplicate" in msg or "unique" in msg or "1062" in msg:
                    write_log("REGISTER_FAIL", _client_ip(), {"username": username, "reason": "duplicate"})
                    return jsonify({"error": f"添加失败：用户名「{username}」已存在"}), 409
                raise
    except Exception as e:
        print(f"[ERROR] 用户注册失败: {e}")
        return jsonify({"error": "数据库错误，注册失败，请稍后重试"}), 500

    write_log("REGISTER", _client_ip(), {"username": username})
    return jsonify({"ok": True, "username": username})


@app.route("/api/logout", methods=["POST"])
def logout():
    username = session.get("username")
    session.clear()
    if username:
        write_log("LOGOUT", _client_ip(), {"username": username})
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    """供前端获取当前登录用户名"""
    username = session.get("username")
    if not username:
        return jsonify({"login_required": True}), 401
    return jsonify({"username": username})


@app.route("/")
@login_required
def index():
    return render_template("index.html")


# ======== 凭证管理（certificate 表 CRUD） ========
@app.route("/certs")
@login_required
def certs_page():
    """凭证管理页面：增删改查 MySQL certificate 表"""
    return render_template("certs.html")


@app.route("/api/certs", methods=["GET"])
@login_required
def certs_list():
    """列出全部凭证"""
    try:
        with get_db().cursor() as cur:
            cur.execute("SELECT certificatekey, value, comment FROM certificate ORDER BY certificatekey")
            rows = cur.fetchall()
    except Exception as e:
        print(f"[ERROR] 查询凭证失败: {e}")
        return jsonify({"error": f"查询失败: {str(e)}"}), 500
    return jsonify({"certs": rows})


@app.route("/api/certs", methods=["POST"])
@login_required
def cert_create():
    """新增凭证：{certificatekey, value, comment}"""
    data = request.get_json(silent=True) or {}
    key = (data.get("certificatekey") or "").strip()
    value = (data.get("value") or "").strip()
    comment = (data.get("comment") or "").strip()
    if not key:
        return jsonify({"error": "凭证关键字不能为空"}), 400
    if len(key) > 64:
        return jsonify({"error": "关键字过长（最大 64）"}), 400
    if len(value) > 128:
        return jsonify({"error": "值过长（最大 128）"}), 400
    try:
        with get_db().cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM certificate WHERE certificatekey = %s", (key,))
            if cur.fetchone()["c"] > 0:
                return jsonify({"error": f"关键字 {key} 已存在"}), 409
            cur.execute(
                "INSERT INTO certificate (certificatekey, value, comment) VALUES (%s, %s, %s)",
                (key, value, comment),
            )
    except Exception as e:
        print(f"[ERROR] 新增凭证失败: {e}")
        return jsonify({"error": f"新增失败: {str(e)}"}), 500
    write_log("CERT_ADD", _client_ip(), {"username": session["username"], "certificatekey": key})
    return jsonify({"ok": True})


@app.route("/api/certs/<path:key>", methods=["PUT"])
@login_required
def cert_update(key):
    """修改凭证：{value, comment}（按主键 key 定位，关键字本身不允许改）"""
    data = request.get_json(silent=True) or {}
    value = (data.get("value") or "").strip()
    comment = (data.get("comment") or "").strip()
    if len(value) > 128:
        return jsonify({"error": "值过长（最大 128）"}), 400
    try:
        with get_db().cursor() as cur:
            cur.execute(
                "UPDATE certificate SET value = %s, comment = %s WHERE certificatekey = %s",
                (value, comment, key),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "凭证不存在"}), 404
    except Exception as e:
        print(f"[ERROR] 修改凭证失败: {e}")
        return jsonify({"error": f"修改失败: {str(e)}"}), 500
    write_log("CERT_EDIT", _client_ip(), {"username": session["username"], "certificatekey": key})
    return jsonify({"ok": True})


@app.route("/api/certs/<path:key>", methods=["DELETE"])
@login_required
def cert_delete(key):
    """删除凭证"""
    try:
        with get_db().cursor() as cur:
            cur.execute("DELETE FROM certificate WHERE certificatekey = %s", (key,))
            if cur.rowcount == 0:
                return jsonify({"error": "凭证不存在"}), 404
    except Exception as e:
        print(f"[ERROR] 删除凭证失败: {e}")
        return jsonify({"error": f"删除失败: {str(e)}"}), 500
    write_log("CERT_DEL", _client_ip(), {"username": session["username"], "certificatekey": key})
    return jsonify({"ok": True})


# ======== 文件上传 ========
@app.route("/api/upload", methods=["POST"])
@login_required
def upload():
    """接收 multipart 文件 -> 上传到阿里云 OSS -> 返回 {url, filename, size, object_key}"""
    if "file" not in request.files:
        return jsonify({"error": "未检测到文件"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "文件名为空"}), 400

    # 扩展名校验
    ext = ""
    if "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"不支持的文件类型: {ext or '(无扩展名)'}，允许: {','.join(sorted(ALLOWED_EXTS))}"}), 400

    # 读取并校验大小
    data = file.read()
    if len(data) > MAX_FILE_SIZE:
        return jsonify({"error": f"文件过大: {len(data)} 字节，上限 {MAX_FILE_SIZE} 字节"}), 400

    storage = get_storage()
    if storage is None:
        return jsonify({"error": "服务器未配置对象存储（请在 .env 中配置 OSS 或 COS 凭证）"}), 500

    # 生成唯一 object key：用户目录 + 时间戳 + uuid + 扩展名
    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    object_key = f"{_user_prefix()}{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_name}"

    try:
        storage.put_object(object_key, data)
        # 存储桶通常是私有读写，返回签名 URL（默认 1 小时有效）供前端访问
        url = storage.sign_url(object_key, 3600)
    except Exception as e:
        return jsonify({"error": f"文件上传失败: {str(e)}"}), 502

    write_log("UPLOAD", _client_ip(), {"filename": file.filename, "size": len(data), "object_key": object_key})

    return jsonify({
        "url": url,
        "filename": file.filename,
        "size": len(data),
        "object_key": object_key,
    })


# ======== OSS 文件列表 / 删除 ========
# 上传时 object key 形如: chat/zhangsan/1727000000_ab12cd34_报告.pdf（按用户分目录）
# 该正则用于去掉时间戳和随机串前缀，还原原始文件名用于展示

_KEY_PREFIX_RE = re.compile(r"^\d+_[0-9a-f]{8}_")


def _user_prefix():
    """当前登录用户在对象存储中的目录前缀，如 chat/zhangsan/（隔离各用户的文件）"""
    return f"{get_storage().prefix}{session['username']}/"


@app.route("/api/files", methods=["GET"])
@login_required
def list_files():
    """列出当前登录用户目录下的全部文件（自动分页，只显示 chat/{用户名}/ 内的文件）"""
    storage = get_storage()
    if storage is None:
        return jsonify({"error": "服务器未配置对象存储"}), 500

    up = _user_prefix()
    try:
        files = []
        marker = ""
        while True:
            items, truncated, marker = storage.list_objects(prefix=up, marker=marker)
            for it in items:
                short_key = it["key"][len(up):] if it["key"].startswith(up) else it["key"]
                display_name = _KEY_PREFIX_RE.sub("", short_key)
                files.append({
                    "object_key": it["key"],
                    "filename": display_name,
                    "size": it["size"],
                    "last_modified": it["last_modified"],
                    "url": storage.sign_url(it["key"], 3600),
                    # 下载走服务器代理接口：先记录下载日志，再 302 到存储的强制下载签名 URL
                    "download_url": f"/api/download?object_key={quote(it['key'])}",
                })
            if not truncated:
                break
    except Exception as e:
        return jsonify({"error": f"列出文件失败: {str(e)}"}), 502

    # 新上传的排前面
    files.sort(key=lambda x: x["last_modified"], reverse=True)
    return jsonify({"files": files})


@app.route("/api/files", methods=["DELETE"])
@login_required
def delete_file():
    """根据 object_key 删除存储上的单个文件（只允许删除当前用户目录内的对象）"""
    data = request.get_json(silent=True) or {}
    object_key = (data.get("object_key") or "").strip()
    if not object_key:
        return jsonify({"error": "缺少 object_key"}), 400

    storage = get_storage()
    if storage is None:
        return jsonify({"error": "服务器未配置对象存储"}), 500

    # 安全校验：防止越权删除本用户目录之外的对象（含其他用户的文件）
    up = _user_prefix()
    if object_key == up or not object_key.startswith(up):
        return jsonify({"error": "非法的 object_key，只能删除自己目录内的文件"}), 403
    # object key 不允许以 / 结尾（那是目录占位符，不是文件）
    if object_key.endswith("/"):
        return jsonify({"error": "非法的 object_key"}), 400

    try:
        storage.delete_object(object_key)
    except Exception as e:
        return jsonify({"error": f"删除失败: {str(e)}"}), 502

    # 还原展示文件名，方便日志阅读
    short_key = object_key[len(up):] if object_key.startswith(up) else object_key
    write_log("DELETE", _client_ip(), {"filename": _KEY_PREFIX_RE.sub("", short_key), "object_key": object_key})

    return jsonify({"ok": True, "object_key": object_key})


# ======== 文件下载 ========
@app.route("/api/download")
@login_required
def download_file():
    """下载代理：记录下载日志后 302 重定向到存储的强制下载签名 URL"""
    object_key = request.args.get("object_key", "").strip()
    if not object_key:
        return jsonify({"error": "缺少 object_key"}), 400

    storage = get_storage()
    if storage is None:
        return jsonify({"error": "服务器未配置对象存储"}), 500

    # 与删除接口相同的前缀校验，防止越权访问其他用户/目录的对象
    up = _user_prefix()
    if object_key == up or not object_key.startswith(up) or object_key.endswith("/"):
        return jsonify({"error": "非法的 object_key"}), 403

    short_key = object_key[len(up):] if object_key.startswith(up) else object_key
    display_name = _KEY_PREFIX_RE.sub("", short_key)
    write_log("DOWNLOAD", _client_ip(), {"filename": display_name, "object_key": object_key})

    # 追加 response-content-disposition 让浏览器强制下载，filename* 形式保证中文名不乱码
    download_params = {"response-content-disposition": f"attachment; filename*=utf-8''{quote(display_name)}"}
    # 短有效期（5 分钟）：用户点击后立即使用
    return redirect(storage.sign_url(object_key, 300, params=download_params), code=302)


# ======== Redis 对话上下文（key: context:{用户名}，列表每项为一轮问答 JSON） ========
_redis_client = None
_redis_warned = False


def get_redis():
    """获取 Redis 连接（进程内单例）；任何异常/未装包/服务端版本过低都返回 None，
    对话自动降级为无上下文模式，绝不影响 Flask 启动与其他功能"""
    global _redis_client, _redis_warned
    if _redis_client is None:
        try:
            if not _REDIS_MODULE_OK:
                raise RuntimeError("未安装 redis Python 包（pip install redis）")
            common = dict(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD,
                          db=REDIS_DB, socket_connect_timeout=1, socket_timeout=1,
                          decode_responses=True)
            try:
                # redis-py 5+ 默认 RESP3，握手会发 HELLO 3；
                # Redis < 6（如 Windows 常见的 3.x/5.x）不支持 HELLO，
                # 强制 RESP2（Redis 2.x~7.x 全版本兼容）避免连接报错
                r = redis.Redis(**common, protocol=2)
                r.ping()
            except TypeError:
                # 老版本 redis-py（<4.2）没有 protocol 参数，本就默认 RESP2，直接连接
                r = redis.Redis(**common)
                r.ping()
            _redis_client = r
            print(f"[INFO] Redis 已连接: {REDIS_HOST}:{REDIS_PORT}")
        except Exception as e:
            if not _redis_warned:
                print(f"[WARN] Redis 不可用（{e}），多轮上下文功能停用，其余功能不受影响")
                _redis_warned = True
            return None
    return _redis_client


def _context_key(username):
    """上下文在 Redis 中的 key：context:{用户名}:{sessionid}。
    不同登录会话使用不同 sid，上下文只在本会话内可见；
    sid 缺失时（理论上不会发生，登录时必写）退化为仅用户名的 key。
    """
    sid = session.get("sid") or ""
    return f"context:{username}:{sid}" if sid else f"context:{username}"


def _history_assistant_content(item):
    """把一轮历史转成送入模型的 assistant 消息内容（紧凑 JSON 对象字符串）。
    - 新格式带 aj（结构化 JSON），直接用；
    - 旧格式只有纯文本 a（可能很长），截短后包成 {"reply": ...}，
      既保持与 json_object 模式一致，又规避长纯文本历史导致 DeepSeek 只回空格的缺陷。
    """
    aj = item.get("aj")
    if aj:
        return aj[:HIST_ANSWER_LIMIT * 2 + 200]
    reply = (item.get("a") or "")[:HIST_ANSWER_LIMIT]
    return json.dumps({"reply": reply}, ensure_ascii=False)


def load_context_messages(username, max_rounds=CONTEXT_MAX_ROUNDS):
    """读取用户最近 N 轮问答，展开为 messages 列表（user/assistant 交替）。
    assistant 历史统一以 JSON 对象字符串形式回传，与 response_format=json_object 保持一致。"""
    r = get_redis()
    if r is None or not username:
        return []
    try:
        raws = r.lrange(_context_key(username), -max_rounds, -1)
        msgs = []
        for raw in raws:
            try:
                item = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if item.get("q"):
                msgs.append({"role": "user", "content": item["q"]})
            aj = _history_assistant_content(item)
            if aj and json.loads(aj).get("reply"):
                msgs.append({"role": "assistant", "content": aj})
        return msgs
    except Exception as e:
        print(f"[ERROR] 读取对话上下文失败: {e}")
        return []


def save_context_round(username, question, structured):
    """保存一轮问答；列表只保留最近 CONTEXT_MAX_ROUNDS 轮，1 小时滑动过期。
    structured 为模型返回的结构化 dict，历史里存其紧凑 JSON（aj），保证与 json_object 模式一致。"""
    r = get_redis()
    if r is None or not username:
        return
    try:
        reply = (structured.get("reply", "") or "")[:HIST_ANSWER_LIMIT]
        hist_obj = {
            "reply": reply,
            "intent": structured.get("intent", ""),
            "confidence": structured.get("confidence"),
            "keywords": (structured.get("keywords", []) or [])[:3],
        }
        item = {"q": question[:1024], "a": reply, "aj": json.dumps(hist_obj, ensure_ascii=False)}
        key = _context_key(username)
        with r.pipeline() as pipe:
            pipe.rpush(key, json.dumps(item, ensure_ascii=False))
            pipe.ltrim(key, -CONTEXT_MAX_ROUNDS, -1)
            pipe.expire(key, CONTEXT_TTL_SECONDS)
            pipe.execute()
    except Exception as e:
        print(f"[ERROR] 保存对话上下文失败: {e}")


# ======== 查看当前会话上下文（Redis 最近 5 轮） ========
@app.route("/api/context", methods=["GET"])
@login_required
def get_context():
    """返回【当前登录用户 + 当前 session】在 Redis 中的最近 CONTEXT_MAX_ROUNDS 轮问答。
    只读本会话（key=context:{用户名}:{sid}），不同登录会话互不可见。"""
    username = session["username"]
    sid = session.get("sid") or ""
    r = get_redis()
    if r is None:
        return jsonify({"available": False, "username": username, "sid": sid, "rounds": [],
                        "message": "Redis 未连接，上下文功能已停用"})
    try:
        key = _context_key(username)
        raws = r.lrange(key, -CONTEXT_MAX_ROUNDS, -1)
        ttl = r.ttl(key)  # 剩余过期秒数；-1=无过期，-2=key不存在
        rounds = []
        for i, raw in enumerate(raws, 1):
            try:
                item = json.loads(raw)
            except (ValueError, TypeError):
                continue
            rounds.append({
                "index": i,                       # 本页内序号（1..N）
                "question": item.get("q", ""),
                "answer": item.get("a", ""),
            })
        write_log("CONTEXT_VIEW", _client_ip(), {"username": username, "rounds": len(rounds)})
        return jsonify({
            "available": True,
            "username": username,
            "sid": sid,
            "redis_key": key,
            "ttl_seconds": ttl if isinstance(ttl, int) and ttl > 0 else 0,
            "max_rounds": CONTEXT_MAX_ROUNDS,
            "total": len(rounds),
            "rounds": rounds,
        })
    except Exception as e:
        print(f"[ERROR] 读取上下文记录失败: {e}")
        return jsonify({"available": False, "username": username, "sid": sid, "rounds": [],
                        "message": f"读取失败: {e}"}), 500


# ======== DeepSeek 对话 ========
def _deepseek_request(messages, use_json=True):
    """调用 DeepSeek；use_json=True 时开启 response_format=json_object 约束输出"""
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.7,
        # 中文一个字约 1-2 token，过小容易触发 finish_reason=length 截断 JSON
        "max_tokens": 2048,
    }
    if use_json:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    return requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=60)


def _coerce_structured(raw):
    """宽容解析模型输出为结构化 dict：
    支持纯 JSON、带 ```json 代码块、正文前后有多余文字（截取首个 { 到末个 }）。
    解析不了或为空返回 None。"""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    text = re.sub(r"^```(?:json)?\s*|```\s*$", "", text, flags=re.M).strip()
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except (ValueError, TypeError):
            return None
    return None


@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    """接收 {message: "...", attachments: [{filename, url, size}]} -> 调用 DeepSeek -> 返回结构化 JSON"""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    attachments = data.get("attachments") or []

    if not message and not attachments:
        return jsonify({"error": "消息和附件不能同时为空"}), 400

    if not DEEPSEEK_API_KEY:
        return jsonify({"error": "服务器未配置 DEEPSEEK_API_KEY，请在 .env 中设置"}), 500

    # 记录用户询问（含附件文件名列表）
    write_log("CHAT_USER", _client_ip(),
              {"message": message, "attachments": [a.get("filename", "") for a in attachments]})

    # 用户提问入库：user_question_record 表，record_idx 取该用户当前最大索引 +1（失败不影响对话）
    record_ref = None  # 成功入库后记为 (username, record_idx)，用于回写 answer
    if message:
        username = session["username"]
        try:
            with get_db().cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(record_idx), 0) AS max_idx FROM user_question_record WHERE username = %s",
                    (username,),
                )
                next_idx = cur.fetchone()["max_idx"] + 1
                cur.execute(
                    "INSERT INTO user_question_record (username, record_idx, time, question) VALUES (%s, %s, %s, %s)",
                    (username, next_idx,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     message[:1024]),  # 截断到字段上限 varchar(1024)
                )
                record_ref = (username, next_idx)
        except Exception as e:
            print(f"[ERROR] 保存用户提问失败: {e}")

    # 组装用户消息：文本 + 附件信息（DeepSeek 不直接读 URL，把附件作为参考信息）
    user_content = message or ""
    if attachments:
        attach_lines = []
        for a in attachments:
            attach_lines.append(f"- 文件: {a.get('filename','未知')} ({a.get('size',0)} 字节) 链接: {a.get('url','')}")
        user_content += "\n\n[附带文件]\n" + "\n".join(attach_lines) + "\n（注意：DeepSeek 无法直接读取文件内容，仅作为信息附带的链接上下文。）"

    # 调用 DeepSeek
    # 组装 messages：system 提示词 + 用户最近 5 轮历史上下文（Redis） + 当前问题
    context_messages = load_context_messages(session["username"])
    messages_with_ctx = [{"role": "system", "content": SYSTEM_PROMPT}] + context_messages + \
                       [{"role": "user", "content": user_content}]
    messages_no_ctx = [{"role": "system", "content": SYSTEM_PROMPT},
                       {"role": "user", "content": user_content}]

    # 多级降级（仅在异常时才进入下一级，正常请求只调用一次）：
    # 1) 带历史 + 强制 JSON（常规路径）
    # 2) 不带历史 + 强制 JSON（规避个别历史触发 DeepSeek 只回空格的缺陷）
    # 3) 带历史 + 普通文本模式（规避 JSON 约束解码异常）
    # 4) 不带历史 + 普通文本模式（最终兜底）
    strategies = [
        (messages_with_ctx, True),
        (messages_no_ctx, True),
        (messages_with_ctx, False),
        (messages_no_ctx, False),
    ]
    structured = None
    last_raw = ""
    last_error = ""
    http_fatal = None  # 4xx 等无需重试的错误响应

    for attempt, (msgs, use_json) in enumerate(strategies):
        try:
            resp = _deepseek_request(msgs, use_json=use_json)
            if resp.status_code != 200:
                # 4xx（鉴权/参数错误）重试无意义，直接终止
                if 400 <= resp.status_code < 500:
                    http_fatal = resp
                    break
                last_error = f"DeepSeek API {resp.status_code}"
                continue
            resp_data = resp.json()
            first_choice = resp_data["choices"][0]
            raw_content = first_choice.get("message", {}).get("content") or ""
            last_raw = raw_content
            obj = _coerce_structured(raw_content)
            if obj is not None and str(obj.get("reply", "")).strip():
                structured = obj
                if attempt > 0:
                    # 走到了降级分支，记录便于排查
                    write_log("CHAT_FALLBACK", _client_ip(),
                              {"message": message, "attempt": attempt + 1,
                               "with_ctx": msgs is messages_with_ctx, "json_mode": use_json})
                break
            last_error = ("回答为空" if not raw_content.strip()
                          else "返回 JSON 缺少 reply 字段")
        except requests.exceptions.RequestException as e:
            last_error = f"网络异常: {str(e)[:150]}"
        except Exception as e:
            last_error = f"解析失败: {str(e)[:150]}"

    if http_fatal is not None:
        write_log("CHAT_ERROR", _client_ip(),
                 {"message": message, "error": f"DeepSeek API {http_fatal.status_code}"})
        return jsonify({"error": f"DeepSeek API 调用失败: {http_fatal.status_code} {http_fatal.text[:200]}"}), 502

    if structured is None:
        # 最终兜底：如果拿到了非空纯文本（普通模式下模型可能直接给文字），包装后返回，避免用户看到空回复
        plain = (last_raw or "").strip()
        if plain:
            structured = {"reply": plain[:1024], "intent": "", "confidence": None, "keywords": []}
            write_log("CHAT_FALLBACK", _client_ip(), {"message": message, "attempt": "plain-wrap"})
        else:
            write_log("CHAT_ERROR", _client_ip(),
                      {"message": message, "error": last_error, "raw_tail": (last_raw or "")[-100:]})
            return jsonify({"error": f"模型暂时无法给出有效回复（{last_error}），请稍后重试"}), 502

    # 记录服务器应答
    write_log("CHAT_AI", _client_ip(),
              {"reply": structured.get("reply", ""), "intent": structured.get("intent", ""),
               "confidence": structured.get("confidence"), "keywords": structured.get("keywords", [])})

    # 大模型回答回写到提问记录的 answer 字段（失败不影响返回）
    if record_ref:
        try:
            with get_db().cursor() as cur:
                cur.execute(
                    "UPDATE user_question_record SET answer = %s WHERE username = %s AND record_idx = %s",
                    (structured.get("reply", "")[:1024],  # 截断到字段上限 varchar(1024)
                     record_ref[0], record_ref[1]),
                )
        except Exception as e:
            print(f"[ERROR] 保存模型回答失败: {e}")

    # 本轮问答存入 Redis 上下文（key=用户名+sid，保留最近 5 轮，1 小时滑动过期）
    if message:
        save_context_round(session["username"], message, structured)

    structured["user_message"] = message or "(仅附件)"
    return jsonify(structured)


if __name__ == "__main__":
    if not DEEPSEEK_API_KEY:
        print("[WARN] 未检测到 DEEPSEEK_API_KEY")
    # 初始化数据库（自动建库建表、创建默认管理员）
    try:
        init_db()
        active_db_name = MYSQL_DB if DATABASE_SEL == "mysql" else PG_DB
        print(f"[INFO] 数据库已就绪: {DATABASE_SEL.upper()} {active_db_name}")
    except Exception as e:
        print(f"[ERROR] {DATABASE_SEL.upper()} 初始化失败: {e}"
              f"（登录功能不可用，请检查 .env 中 {DATABASE_SEL.upper()} 配置及数据库服务是否启动）")
    # 启动时即完成存储选型，之后整个进程都用这一套
    if get_storage() is None:
        print("[WARN] 未检测到对象存储配置（OSS 或 COS），文件相关功能不可用")
    print("[INFO] 服务启动: http://127.0.0.1:5000  (Ctrl+C 退出)")
    app.run(host="0.0.0.0", port=5000, debug=True)
