# Telegram 多帳號 AI 控制台

一個 FastAPI 控制台同時管理多個 Telegram 使用者帳號。每個帳號都有獨立 Session、固定人物設定、AI Base URL、API Key、模型、群組範圍及回覆模式。只處理群聊；每位群成員的上下文保留 24 小時。

## 功能

- 多帳號同時登入，Session 彼此獨立
- 控制台新增、啟動、停止、刪除帳號
- 線上修改人物提示詞、角色類型與 AI 模型；下一則訊息立即生效
- 支援 OpenAI-compatible API
- `all`、`mention`、`probability` 三種群聊模式
- **用戶黑名單**：每個帳號可獨立設定，屏蔽指定 user ID 觸發 AI
- **群組冷卻**：同一群組在冷卻時間內不會重複回覆，避免刷屏
- **活動統計**：每個帳號顯示已回覆訊息數與最後活躍時間
- **訊息日誌**：控制台可檢視每個帳號的近期對話記錄
- **JSON API**：提供 `/api/accounts`、`/api/accounts/{id}/messages`、`/api/accounts/{id}/stats` 端點，方便外部整合
- 受管帳號互相忽略，避免形成無限回覆
- Session 與 AI Key 使用 Fernet 加密後儲存在 SQLite；`MASTER_KEY` 變更後舊資料將無法解密
- 管理控制台使用簽名 Cookie 登入
- 24 小時記憶自動清理
- **Schema 自動遷移**：舊資料庫升級時自動補齊新欄位，不會崩潰
- **AI Client 快取**：同一帳號共用 AsyncOpenAI 實例，降低高流量群組的連線開銷

## 本機啟動

```bash
cp .env.example .env
# 修改 .env

docker compose up -d --build
```

控制台：`http://localhost:8000`

本機使用 HTTP 時，請在 `.env` 設定 `COOKIE_SECURE=false`；Railway 公開網域使用 HTTPS 時維持 `true`。

## 產生 Telegram Session String

先在本機 `.env` 填入 `TG_API_ID`、`TG_API_HASH`、`TG_PHONE`，然後：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.session_generator
```

Windows 啟動虛擬環境：

```powershell
.venv\Scripts\activate
```

Session String 相當於 Telegram 登入憑證，只能貼進控制台，不可提交 GitHub。

## Railway

1. 將專案推送到 GitHub，Railway 從該倉庫建立服務。
2. 在 Railway Variables 填入 `.env.example` 中的全域變數。
3. 建立 Railway Volume，掛載到 `/data`；否則重部署會遺失帳號設定與記憶。
4. 產生公開 Domain，登入控制台後新增各 Telegram 帳號。

Railway 會自動使用根目錄的 Dockerfile，健康檢查路徑為 `/health`。

## JSON API

控制台提供以下 JSON API 端點（需先登入取得 Cookie）：

| 端點 | 說明 |
|---|---|
| `GET /api/accounts` | 列出所有帳號（含 runtime 狀態） |
| `GET /api/accounts/{id}/messages` | 取得帳號近期訊息（可選 `?chat_id=&limit=`） |
| `GET /api/accounts/{id}/stats` | 取得帳號統計（訊息數、最後活躍、上線狀態） |
| `GET /health` | 健康檢查（無需登入） |

## 多帳號注意事項

同一個 Telegram Session 不可同時在兩個服務中使用。每個帳號應有自己的 Session String；若 Session 洩漏，請立即在官方 Telegram 客戶端終止該工作階段並重新產生。

人物設定可以使用一般群成員口吻，但不得捏造真實線下經歷、虛假見證或在被直接詢問時冒充非自動化帳號。
