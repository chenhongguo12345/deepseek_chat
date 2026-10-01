# DeepSeek 结构化对话助手

一个基于 Flask + DeepSeek API + 阿里云 OSS 的 Web 聊天应用。用户发送消息后，后端调用 DeepSeek 大模型并返回**结构化 JSON**（回复正文 + 意图分类 + 置信度 + 关键词）；支持上传文件到 OSS 作为对话附件，并可在页面上管理（预览 / 下载 / 删除）已上传的 OSS 文件。

## 功能总览

| 功能 | 说明 |
|------|------|
| AI 对话 | 调用 DeepSeek 大模型，返回结构化 JSON（reply / intent / confidence / keywords） |
| 文件上传 | 通过页面 ➕ 按钮上传文件到阿里云 OSS（扩展名白名单 + 大小限制） |
| 附件对话 | 已上传文件可随消息一起发送，作为对话上下文（模型只能看到文件名和链接，读不到内容） |
| 文件列表 | 页面右上角「📁 文件列表」面板，只列出当前登录用户自己的目录（`chat/{用户名}/`）下的文件 |
| 文件预览 | 点击文件名在新标签页打开（1 小时有效的签名 URL） |
| 文件下载 | 点击 ⬇ 强制浏览器下载（中文文件名不乱码） |
| 文件删除 | 点击 🗑 删除 OSS 上的文件（带二次确认，服务端做前缀防越权校验） |
| 用户登录 | 用户名 + 密码登录（校验 MySQL `aichat.user_account` 表），未登录访问自动跳转登录页；支持登出，会话有效期 7 天 |

## 快速开始

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制并填写配置
copy .env.example .env   # 然后编辑 .env 填入 DeepSeek Key 和 OSS 凭证

# 3. 启动
python app.py            # 然后浏览器打开 http://127.0.0.1:5000
```

## 配置项（.env）

| 变量 | 必填 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | 是 | DeepSeek API Key |
| `DEEPSEEK_MODEL` | 否 | 模型名，默认 `deepseek-chat` |
| `STORAGE_PROVIDER` | 否 | 对象存储选型：`oss` / `cos` / `auto`（默认 auto，两套都配置时随机选一套，多实例可实现负载分担；只配一套则用那一套）。启动时选定，之后整个进程都用这一套 |
| `STORAGE_CERT_SEL` | 否 | 存储密钥来源：设为 `db`（小写）时，`OSS_ACCESS_KEY_ID`/`OSS_ACCESS_KEY_SECRET`/`COS_SECRET_ID`/`COS_SECRET_KEY` 四个密钥从 MySQL `certificate` 表读取（`certificatekey` 存键名、`value` 存值），忽略环境变量；默认 `env` 或其他值时沿用环境变量。endpoint/region/bucket 始终取自环境变量 |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` | 二选一 | 阿里云 AccessKey |
| `OSS_ENDPOINT` | 二选一 | 例如 `oss-cn-hangzhou.aliyuncs.com` |
| `OSS_BUCKET_NAME` / `OSS_KEY_PREFIX` | 二选一 | Bucket 名称 / 目录前缀（默认 `chat/`） |
| `COS_SECRET_ID` / `COS_SECRET_KEY` | 二选一 | 腾讯云 SecretId / SecretKey |
| `COS_REGION` | 二选一 | 地域，例如 `ap-shanghai` |
| `COS_BUCKET_NAME` / `COS_KEY_PREFIX` | 二选一 | Bucket 名称（格式 `<名>-<APPID>`）/ 目录前缀（默认 `chat/`） |
| `ALLOWED_EXTS` | 否 | 允许上传的扩展名白名单，逗号分隔 |
| `MAX_FILE_SIZE` | 否 | 单文件大小上限（字节），默认 10MB |
| `MYSQL_HOST` / `MYSQL_PORT` | 是 | MySQL 地址 / 端口，默认 `127.0.0.1` / `3306` |
| `MYSQL_USER` / `MYSQL_PASSWORD` | 是 | 数据库账号 / 密码，默认 `root` / `123456` |
| `MYSQL_DB` | 是 | 数据库名，默认 `aichat`（不存在时启动自动创建） |
| `REDIS_HOST` / `REDIS_PORT` | 否 | Redis 地址 / 端口，默认 `127.0.0.1` / `6379`；Redis 不可用时自动降级为无上下文模式 |
| `REDIS_PASSWORD` / `REDIS_DB` | 否 | Redis 密码（无密码留空不填）/ 库编号，默认 0 |
| `SECRET_KEY` | 否 | session Cookie 签名密钥，生产环境务必改成随机长字符串 |

> 两套存储都未配置时服务仍可启动，聊天功能正常，仅文件相关功能不可用。
>
> **数据库初始化**：启动时 `init_db()` 自动 `CREATE DATABASE IF NOT EXISTS aichat`，并创建两张表：`user_account`（`username` 主键、`valid`（1 启用 / 0 停用）、`password`、`created_at`，表为空时自动创建默认管理员 **admin / admin123**，密码 werkzeug 哈希存储，请登录后尽快修改）和 `certificate`（`certificatekey` 主键、`value`、`comment`，`STORAGE_CERT_SEL=db` 时存储 OSS/COS 密钥，插入示例：`INSERT INTO certificate (certificatekey, value) VALUES ('OSS_ACCESS_KEY_ID', 'LTAI...')`，四个键名为 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`COS_SECRET_ID`、`COS_SECRET_KEY`）。已存在的表不会被修改；登录校验兼容历史明文密码和 `pbkdf2:`/`scrypt:` 哈希密码。

## 存储抽象层（app.py）

为支持 OSS / COS 双存储，所有存储操作收敛到统一接口，业务路由不感知具体厂商：

- **`OssStorage` / `CosStorage`**：两个封装类实现完全相同的接口——`put_object(key, data)`、`delete_object(key)`、`sign_url(key, expires, params)`、`list_objects(prefix, marker)`（返回统一格式的 `(items, is_truncated, next_marker)`，时间统一为 Unix 秒），并各带 `prefix` 属性（上传目录前缀）。COS 的 ISO 8601 时间由 `_iso8601_to_ts()` 转换
- **`get_storage()`**：先通过 `_load_storage_certs()` 解析密钥（`STORAGE_CERT_SEL=db` 时从 MySQL `certificate` 表读取，进程内缓存；否则读环境变量），再按 `STORAGE_PROVIDER` 选型并惰性初始化全局单例，进程内只初始化一次，之后所有请求都用同一套存储

## 后端接口（app.py）

> 除 `/login`、`/api/login` 外，所有页面和 API 均受 `@login_required` 保护：页面请求未登录 302 跳转 `/login`，API 请求返回 `401 {"login_required": true}`，前端 `apiFetch()` 统一拦截后跳转登录页。

### `GET /login` → `login_page()` / `POST /api/login` → `login()`
登录页渲染 + 登录接口。登录接口按用户名查 `user_account` 表，校验 `valid=1` 且密码匹配（`verify_password()` 兼容哈希/明文），通过后 `session["username"]` 写入签名 Cookie（7 天有效）。另有 `POST /api/logout` 清空会话、`GET /api/me` 返回当前用户。

### `GET /` → `index()`
登录后渲染聊天页面 `templates/index.html`（头部通过 Jinja2 显示当前 `session.username`）。

### `POST /api/upload` → `upload()`
接收 multipart 表单中的 `file` 字段，上传到 OSS。

处理流程：扩展名白名单校验 → 读入内存校验大小 → 生成唯一 object key（`chat/{用户名}/ + 时间戳 + uuid前8位 + 原文件名`，按用户分目录隔离）→ `bucket.put_object()` 上传 → 返回 1 小时有效的签名 URL。

返回：`{url, filename, size, object_key}`；错误返回 `{error}` + 4xx/5xx。

### `GET /api/files` → `list_files()`
列出当前登录用户的目录（`chat/{用户名}/`）下全部文件。内部用 `bucket.list_objects()` 按 marker 自动分页；跳过以 `/` 结尾的目录占位符；用正则 `^\d+_[0-9a-f]{8}_` 去掉 object key 中的时间戳/随机串前缀，还原出原始文件名用于展示。

每个文件返回：

| 字段 | 说明 |
|------|------|
| `object_key` | OSS 完整对象键（删除时用） |
| `filename` | 展示用原始文件名 |
| `size` | 字节数 |
| `last_modified` | Unix 时间戳（秒） |
| `url` | 预览签名 URL（1 小时有效） |
| `download_url` | 下载代理地址 `/api/download?object_key=...`，浏览器访问时先经服务器记录日志，再 302 跳转到存储的强制下载地址 |

列表按上传时间倒序返回。

### `GET /api/download` → `download_file()`
下载代理。校验 object key 前缀（与删除接口相同，防越权）→ 记录 `DOWNLOAD` 日志 → 生成 5 分钟有效的强制下载签名 URL（`response-content-disposition: attachment`，`filename*=utf-8''` 保证中文文件名不乱码）→ 302 重定向过去。浏览器直接下载，前端无感知。

### `DELETE /api/files` → `delete_file()`
请求体：`{"object_key": "chat/xxx"}`。

安全校验：object key 必须以当前用户目录 `chat/{用户名}/` 开头且不等于前缀本身、不能以 `/` 结尾——防止越权删除本应用目录之外的对象（包括其他用户的文件）。校验通过后调用 `bucket.delete_object()`。返回 `{"ok": true, "object_key": ...}`。

### `POST /api/chat` → `chat()`
请求体：`{"message": "...", "attachments": [{filename, url, size}]}`（两者不能同时为空）。

处理流程：**用户提问入库**（`user_question_record` 表，`record_idx` 取该用户当前 `MAX(record_idx)+1`，时间格式 `YYYY-MM-DD HH:MM:SS`，超长问题截断到 1024 字符，入库失败只打印错误不影响对话）→ 组装用户消息（文本 + 附件信息列表）→ **携带上下文调用** DeepSeek（messages = system 提示词 + Redis 中该用户最近 5 轮问答 + 当前问题；`response_format: json_object` 强制 JSON 输出）→ 解析模型返回的 JSON → **回答回写**（`UPDATE` 该记录的 `answer` 字段，同样截断 1024 字符；DeepSeek 调用失败时 answer 留空）→ **本轮问答写入 Redis 上下文**（key `context:{用户名}`，保留最近 5 轮，1 小时滑动过期）→ 附加 `user_message` 字段后返回。

返回结构：

```json
{
  "reply": "回复正文",
  "intent": "意图分类",
  "confidence": 0.95,
  "keywords": ["关键词1", "关键词2"],
  "user_message": "用户原始输入"
}
```

### 存储选型 `get_storage()`
惰性初始化全局 `_storage` 单例：`STORAGE_PROVIDER=auto` 时收集配置齐全的存储，都齐全则**随机选一套**（多实例可实现负载分担），只配一套则用那一套；显式指定 `oss`/`cos` 时若对应配置不完整则报错返回 `None`。启动时即完成选型（`__main__` 中主动调用一次），之后所有请求复用同一实例，避免未配置存储时服务无法启动。

## 操作日志

所有用户操作写入 `logs/` 目录，**每周一个文件，文件名为该周周一日期**（如 `logs/2026-09-21.log`），跨周自动切换新文件。线程安全（`_log_lock`），UTF-8 编码，每行格式：

```
2026-09-27 14:30:00 | 192.168.1.100 | UPLOAD | {"filename": "报告.pdf", "size": 12345, "object_key": "chat/..."}
```

即 `时间 | 客户端IP | 操作类型 | JSON 内容`。记录的操作：

| 操作类型 | 触发点 | 内容 |
|---------|--------|------|
| `UPLOAD` | `/api/upload` 上传成功 | 文件名、大小、object_key |
| `DOWNLOAD` | `/api/download` 下载代理被访问 | 文件名、object_key |
| `DELETE` | `/api/files` DELETE 删除成功 | 文件名、object_key |
| `CHAT_USER` | `/api/chat` 收到有效询问 | 消息文本、附件文件名列表 |
| `CHAT_AI` | DeepSeek 应答成功返回 | reply、intent、confidence、keywords |
| `CHAT_ERROR` | DeepSeek 调用/解析失败 | 消息文本、错误摘要 |
| `LOGIN` | `/api/login` 登录成功 | 用户名 |
| `LOGIN_FAIL` | 登录失败（用户名/密码错误、账号停用） | 尝试登录的用户名 |
| `LOGOUT` | `/api/logout` 登出 | 用户名 |

IP 获取：优先取 `X-Forwarded-For` 首个地址（部署在 nginx 反代后时为真实 IP），否则取 `request.remote_addr`。日志写入失败只打印控制台错误，不影响业务接口。

## 前端（templates/）

- **login.html**：登录页（内联 CSS + 原生 JS，无构建步骤），提交用户名密码到 `/api/login`，失败显示错误提示，成功跳转 `/`
- **index.html**：单文件聊天页面，主要模块：
  - **鉴权**：所有业务请求统一走 `apiFetch()`，收到 401 自动跳转 `/login`；头部显示当前用户，「退出」按钮调用 `/api/logout`
  - **聊天**：`sendMessage()` 发送消息+附件到 `/api/chat`，`appendAiMsg()` 渲染回复气泡、意图/置信度/关键词标签和可折叠的原始 JSON
  - **上传**：➕ 按钮触发隐藏的文件选择框，`uploadFile()` 逐个上传到 `/api/upload`，待发送附件在输入框上方的暂存区显示，可单独移除
  - **文件管理**：`loadFiles()` 拉取 `/api/files` 渲染面板列表；⬇ 走 `download_url` 直接下载；🗑 调用 `deleteFile()` 二次确认后发 DELETE 请求，成功后从 DOM 移除该行

## 运行环境说明

- 需要 Python 3.9+，依赖见 `requirements.txt`（Flask / requests / python-dotenv / oss2 / cos-python-sdk-v5 / PyMySQL / redis）
- 需要可访问的 MySQL 服务（账号密码库名在 `.env` 配置），启动时自动建库建表
- 可选：本地 Redis 服务（对话上下文缓存，`context:{用户名}` 键保留最近 5 轮问答、1 小时滑动过期；未启动时自动降级，不影响其他功能）
- 项目自带 `.venv` 虚拟环境，IDE 运行时会优先使用它；命令行运行请用 `.\.venv\Scripts\python.exe app.py` 或 `py -3 app.py`（系统 PATH 里的 `python` 是微软商店占位符，不可用）

## 安全说明

- OSS 上传/删除/下载接口均有服务端校验；删除和下载接口限制只能操作当前用户目录 `chat/{用户名}/` 内的对象，用户之间文件完全隔离
- `OSS_KEY_PREFIX` / `COS_KEY_PREFIX` 仍是存储的根目录（默认 `chat/`），用户子目录拼接在其后
- `.env` 含真实密钥，**不要提交到 Git**；如曾泄露请到 RAM 控制台轮换 AccessKey
- 签名 URL 有效期 1 小时，过期后刷新文件列表即可获取新链接
- 所有页面和 API（除登录本身）均需登录后访问；session 用 `.env` 中的 `SECRET_KEY` 签名，生产环境务必修改该密钥
- 新建账号的密码以 werkzeug 哈希存储；兼容历史明文密码仅为过渡，建议尽快将明文密码更新为哈希
- 登录成功/失败、登出均写入操作日志（含 IP 和用户名）
