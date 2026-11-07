# MC 聊天橋接 v9（單向：遊戲 → Discord）

## 檔案
| 檔案 | 說明 |
| --- | --- |
| `MC-WS.exe` | 建立 `\\.\pipe\mc_chat_827`、把 DLL 注入 `Minecraft.Windows.exe`、把事件轉成 WebSocket 送到 `127.0.0.1:8000` |
| `827dll.dll` | 注入到遊戲內的擷取 DLL（掃描記憶體，抓聊天 / 加入 / 離開 / 死亡 / TP / 封禁） |
| `bot.py` | Discord Bot（單檔 v9.4，已把原本 `bridge/` 套件全部合併進來），接收事件並轉發到頻道 / Webhook |
| `start.bat` | 一鍵啟動上面兩個 |
| `old_bridge_20261107/` | 合併前的 `bridge/` 套件備份（程式已不再引用，確認沒問題後可刪） |
| `bot_v1_backup.py` | 更早的單檔版本備份 |

## 使用步驟
1. 先開啟 Minecraft，進入世界（或加入別人的房間）。
2. 雙擊 `start.bat`（會先開橋接器，再開 bot）。
3. 橋接器視窗出現 `[+] DLL 已連接` 就代表開始抓事件。

> 每次啟動 MC-WS.exe 都會把 827dll.dll 複製成 `827dll_<編號>.dll` 再注入，
> 管線名也跟著編號走，所以**換版本不用重開 Minecraft**；舊的 DLL 會自動結束。

## 事件 → Discord
| 事件 | 來源 | Discord 顯示 |
| --- | --- | --- |
| `PlayerMessage` | `<名字> 訊息` | `名字: 訊息` |
| `PlayerJoin` | `X 加入了遊戲` | ✅ **X** 加入了遊戲 |
| `PlayerLeave` | `X 離開了遊戲` | ❌ **X** 離開了遊戲 |
| `PlayerDied` | `X 被 Y 殺死了` / `X 被 Y 砸爛了` / `X kill by Y` / `X death!` | 💀 … |
| `PlayerBan` | `玩家xuid … 已被封禁` | 🔨 封禁事件 |
| `TP_DETECT` | `tpmultiplayer.xyz` 等 | 🛑 TP 平台特徵 |
| `TP_JOIN` | `玩家嘗試加入 => 名稱:xxx PFID:…` / `try connect: xxx` | 🛑 偵測到 TP 拉人 → **自動踢出** |
| `KickResult` | 指令執行結果回報 | 失敗才寫 Discord |
| `BridgeStatus` | DLL 心跳 | 只寫 console（debug 等級） |

## 斜線指令

指令是以**伺服器（guild）指令**方式註冊，`/sync` 可強制重同步；簽名不符時會自動重同步一次。

| 指令 | 說明 |
| --- | --- |
| `/status` | 橋接狀態、玩家數、待送指令數 |
| `/ping` | 測試機器人是否在線 |
| `/players` | 線上玩家（先問遊戲 `list`，失敗才用事件快取） |
| `/check` | 時間 / 天氣 / 種子 |
| `/cmd <指令>` | 在遊戲內執行 Minecraft 指令（有禁用清單） |
| `/kick <玩家> [理由]` | 踢人（不封禁） |
| `/ban <玩家> [分鐘] [理由]` | 寫入封禁名單並踢出 |
| `/unban <玩家>` / `/banned` | 解封 / 查看名單 |
| `/join <房間>` | 加入房間（快捷，等同 `/lianji join`） |
| `/lianji list [語言]` | 房間列表（分頁 + 重新整理） |
| `/lianji info <房間>` | 房間詳情 + 成員名單（標出遊戲內是否也偵測到） |
| `/lianji join <房間>` | 加入房間 |
| `/lianji me` | 查自己的 XUID / addid / 中繼帳號 |
| `/lianji refresh` | 刷新後端粉絲快取 |
| `/settings` `/sync` `/wl` | 設定 / 重同步指令 / 白名單 |
| `/shutdown` | 關閉 bot（會先移除斜線指令） |

`<房間>`、`<玩家>` 都支援**自動補全**（打幾個字就會列出房間或線上玩家）。

## 指令跟著 bot 上下線

Discord 的斜線指令註冊後是**常駐**的，bot 離線也還掛在那裡。要讓它「離線就看不到」，
唯一的方法是退出前主動刪掉 —— 本專案就是這樣做的：

| 時機 | 行為 |
| --- | --- |
| `start.bat` / `run_bot.bat` 啟動 | 先清空全域指令，再逐個伺服器同步（先 `clear` 再註冊，蓋掉舊版殘留） |
| `/shutdown`、`stop.bat`、Ctrl+C、SIGTERM | 清空所有伺服器 + 全域指令 → 斷線 → 寫入 `bot_stop.flag` |
| `run_bot.bat` 看到 `bot_stop.flag` | 刪掉標記並跳出迴圈，**不再自動重啟** |
| 非主動退出（崩潰 / 被 taskkill /f） | 來不及清，指令會殘留；下次啟動會重新同步覆蓋 |

`stop.bat` 會先呼叫 `python bot.py --stop`（打 Ban API 的 `POST /shutdown`）走優雅關閉，
失敗才退回強制結束。也可以手動執行：

```
py -3 bot.py --stop
```

## 指令佇列（v9.3 起）

遊戲 WebSocket 沒連線時，踢人 / `/cmd` **不再退回鍵盤注入**，而是排進佇列，
等遊戲連上後由背景 worker 自動補送並在 Discord 回報：

- 佇列上限 `KICK_QUEUE_MAX`（預設 50），滿了直接丟棄並提示
- 放超過 `KICK_QUEUE_TTL`（預設 600 秒）就作廢，避免幾小時後突然踢人
- 補送前會確認那個人**還在線上**；已經離開的不再執行（否則遊戲會回「沒有與選擇器匹配的目標」）
- 想立刻生效：確認遊戲端 WebSocket 已連上（`/status` 看「遊戲 WebSocket」是否 🟢）

## TP 偵測的誤判防護（v9.3）

TP Multiplayer 外掛自己會在聊天欄印橫幅：

```
* 外部 在 TP Multiplayer 上發現全球所有xbox好友的世界
  在网站查看世界列表，无需添加好友即可加入
  tpmultiplayer.xyz
```

這段以前被當成「偵測到 TP 平台特徵」而狂刷 Discord，尾巴的二進位噪聲
（`tpmultiplayer.d`、`tpmultiplayer.x8`…）還讓去重失效。現在三層擋：

| 位置 | 規則 |
| --- | --- |
| DLL `analysis.cpp` | 含「發現全球 / 世界列表 / 网站查看 / xbox好友」的整行直接丟棄；網域只認 `tpmultiplayer.xyz`，其餘碎片丟棄 |
| DLL `TPJOIN` | 名字必須 3~16 位英數/底線，含 `tpmultiplayer` 的直接丟棄 |
| bot `bot.py` | `is_tp_banner()` 再擋一次；提示以「去掉括號內容」為鍵做 180 秒節流；`valid_player_name()` 擋掉垃圾名字，**不合格的連踢都不會排隊** |

真正的加入告警仍走 `try connect: <名字>` / `玩家嘗試加入 => 名稱:…` 兩條錨點。

## 已知限制
- Discord → 遊戲的訊息 / 指令走**遊戲端 WebSocket**（`/connect ws://127.0.0.1:8000`）。
  沒連上時指令會排隊（見上節），不會用鍵盤模擬硬塞。
- 聊天（`<名字> 訊息`）是靠記憶體特徵比對，少數訊息可能漏抓或誤判；
  加入 / 離開 / 封禁 / TP 是關鍵字錨定，穩定得多。
- v9.1 起聊天會先驗「原始位元組是合法 UTF-8」再用共用的「像人打的字」閘門過濾，
  記憶體碎片（例如 `9, NI8 VQ: pjT g l`）不會再被當成聊天；代價是極短的亂碼式發言可能被濾掉。
- 非房主、非 OP 一樣可以抓事件（讀的是自己客戶端的記憶體），但**不能**踢人 / 執行指令 ——
  那需要伺服器端權限，客戶端做不到。

## .env 可調
```
ONE_WAY_MODE=false         # false = 雙向（Discord 聊天也送進遊戲）
ENABLE_JOIN_LEAVE=true
ENABLE_DEATH=true
ENABLE_TP_ALERT=true
ENABLE_BAN_ALERT=true

# 指令佇列
KICK_QUEUE_TTL=600         # 排隊多久沒連上就作廢（秒）
KICK_QUEUE_MAX=50          # 佇列上限

# 聯機大廳（文件：https://docs.miaaoo.com）
MIAAOO_API_BASE=https://api.miaaoo.com
MC_XUID=                   # 填 16 位 XUID 就跳過 /profile 查詢（換過 Gamertag 很有用）

TP_KICK=true                          # 偵測到 TP 平台拉人就自動踢（要關改 false）
TP_KICK_REASON=偵測到 TP 平台加入      # 踢出時顯示的理由
TP_KICK_COOLDOWN=20                   # 同一人幾秒內不重複踢
TP_AUTO_BLACKLIST=false               # 要不要順便寫進封禁名單（永久攔）
TP_BAN_MINUTES=0                      # 上者成立時封多久，0 = 永久
```

> 自動踢出走的是遊戲 WebSocket 的 `kick` 指令，**需要房主 / OP 權限**。

## 聯機大廳 API（對照 docs.miaaoo.com）

`bot.py` 第 6 段依官方 v2 文件實作，只用到這幾個端點：

| 用途 | 端點 | 備註 |
| --- | --- | --- |
| 房間列表 | `GET /v2/list` | `lang` 可重複傳（`?lang=zh&lang=en`）；30 秒刷新一次快取 |
| 帳號清單 | `GET /v2/account` | 回傳 `[{id, name, canaddfriends}]`，`addid` 取 `canaddfriends=true` 那個的 `id` |
| 查 XUID | `GET /profile?gt=名字` | XUID 在 `profileUsers[0].id`；`gt` 與 `xuid` 不可同時傳 |
| 房間詳情 | `GET /roominfo?session=&roomfrom=` | `members[]` 裡有 `gamertag` / `xuid` |
| 加入房間 | `POST /v2/join` | 用**新版協議** `apiversion: 2`，`userxuid` 必填（16 位） |
| 刷新粉絲 | `GET /refreshFollowers` | 回應碼不可靠；10 秒內重複呼叫只回 206 |

舊版程式有三處偏差，已修正：房間列表誤打 `/roominfo`（一直空）、
`addid` 找錯欄位名、XUID 從 `profile["xuid"]` 讀（實際在 `profileUsers[0].id`）。

加入成功後**不會**產生任何邀請網址——後端是先把你的帳號加進該帳號的粉絲、再發 Xbox 邀請，
所以正確做法是：開遊戲 → `世界` 分頁 → 重新整理 → 點房間卡片加入。

## 資料檔位置

```
whitelist.json         授權使用者（Discord ID 或 遊戲名）
blacklist.json         封禁名單
bridge_config.json     允許的伺服器 / 頻道（預設在 ~ ，可用 BRIDGE_CONFIG_FILE 改）
```

`WHITELIST_FILE` / `BLACKLIST_FILE` 填**相對路徑**時一律以專案目錄為基準，
填 `~` 或絕對路徑也照常運作。`.env` 的 `DEFAULT_AUTHORIZED_USERS` 一律有效，
即使白名單檔被清空也不會把自己鎖在外面。

## 原始碼
`src/dll/*.cpp`、`src/exe/*.cpp`、`bot.py`

## 程式結構

```
bot.py                 單檔程式（v9.4），依「目錄」分段：
  1  設定與日誌          .env 讀取、輪替式 bot.log（5MB × 3）
  2  文字工具            § 色碼、rawtext、UI 洗頻
  3  JSON 檔案存放       白名單 / 封禁名單 / 頻道設定（原子寫入）
  4  WebSocket 伺服器    收事件、去重、送指令、斷線自動重試
  5  HTTP 封禁 API       127.0.0.1:3001
  6  聯機大廳 API        /v2/list、/v2/account、/profile、/roominfo、/v2/join
  7  Discord 客戶端      事件轉發 + 斜線指令 + 指令佇列
      Room / RoomPaginator / DCListener
  8  組裝與進入點        單一實例鎖、啟動、關閉

old_bridge_20261107/   合併前的 bridge/ 套件（備份，可刪）
bot_v1_backup.py       更早的單檔版本備份

src/dll/               827dll.dll（記憶體擷取）
  common.h             共用宣告
  dllmain.cpp          進入點
  analysis.cpp         文字清洗 / 名字 / 分類 / 聊天
  http.cpp             事件 POST / 指令輪詢
  scan.cpp             區域列舉 / 熱區 / 掃描迴圈
  inject.cpp           模擬鍵盤送指令

src/exe/               MC-WS.exe（橋接器）
  common.h             共用宣告
  globals.cpp          常數與全域
  main.cpp             啟動、注入
  ws.cpp               WebSocket 客戶端
  httpserver.cpp       管線 / HTTP 端點
  events.cpp           事件 → JSON
  winutil.cpp          UAC / 權限 / 注入

build.bat              編譯上面兩個
```

## 傳輸方式

```
Minecraft.Windows.exe
  └─ 827dll_<tag>.dll
        │  POST http://127.0.0.1:<8770+tag%200>/event     ← 事件（一行一則）
        │  GET  http://127.0.0.1:<8770+tag%200>/command   ← 指令（每秒輪詢）
        ▼
     MC-WS.exe  ──  ws://127.0.0.1:8000  ──►  bot.py  ──►  Discord
```
