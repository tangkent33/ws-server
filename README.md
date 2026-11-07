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
| `TP_DETECT` | 舊版的網域特徵 | ⛔ v9.5.2 起 DLL 不再產生（那是遊戲自己的橫幅） |
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
| `/statusboard start` | 在目前頻道建立一行式狀態列並開始自動刷新 |
| `/statusboard now` / `stop` | 立刻刷新 / 停止並刪除 |
| `/shutdown` | 關閉 bot（會先移除斜線指令） |

`<房間>`、`<玩家>` 都支援**自動補全**（打幾個字就會列出房間或線上玩家）。

## 狀態列（`/statusboard`）

一條常駐訊息，定時改寫成一行：

```
🟢 2/20 個玩家在線 ｜ 3 個玩家已加入伺服器 ｜ 伺服器已運行 3 小時 46 分鐘 ｜ 最近更新時間: 周六, 07. 11月 2026 14:04:09 CST
```

| 欄位 | 來源 |
|---|---|
| 在線 / 上限 | 加入·離開事件累積的 `mc.players`；上限由啟動時問一次 `list` 得到，之後沿用 `MC_MAX_PLAYERS` |
| 已加入伺服器 | 本次執行以來**累計**出現過的玩家數（離開不減） |
| 伺服器已運行 | 本機 `Minecraft.Windows.exe` 的啟動時間（快取 5 分鐘）；讀不到時退回 bot 啟動時間 |
| 最近更新時間 | 每次刷新當下 |

- 刷新間隔 `STATUS_INTERVAL`（預設 60 秒，最低 20）——改一條訊息比發新訊息省限流
- **不會**每分鐘去打 `list` 指令（那條會經橋接器注入遊戲，會污染指令佇列）；只在啟動時問一次
- 訊息會自動置頂（沒有「管理訊息」權限就跳過）
- bot 重啟後自動接續同一條訊息（`statusboard.json` 記錄頻道與訊息 id；`STATUS_AUTO_START=false` 可關）
- 手動把訊息刪掉沒關係，下一輪重新整理會自動重建

## 平台邀請號（不會被踢）

miaaoo 的加入機制是：後端先用「邀請帳號」加你好友，再發 Xbox 邀請。所以你會在
Xbox / 遊戲邀請列表看到 `Happy2026MC`、`HelloMC25`、`HelloMC25a`、`HelloMC25b5988`
這些號 —— **它們是官方邀請號，不是入侵者**。

這些號列在 `.env` 的 `PLATFORM_INVITERS`（逗號分隔，預設含上面幾個 + `MCLobby26`），
任何情況都不會被踢：

- 加入事件不觸發封禁攔截（即使有人手動把它們寫進 `blacklist.json`）
- 命中 TP 特徵也直接略過（不告警、不踢）
- 手動 `/kick` 也會被拒，回「（miaaoo 邀請帳號，已跳過）」

後端會輪換邀請號，遇到新的直接補進 `PLATFORM_INVITERS` 即可。`/settings` 可看目前名單。

## 斜線指令常駐（不再「離線就消失」）

Discord 對「每個伺服器的指令覆蓋」限流很兇：**每 10 分鐘只允許 2 次 PUT**。
舊版做過「退出就刪指令、重啟再重新註冊」，頻繁重啟就會撞 429，
指令被清空後又註冊不回來 → 用戶看到的就是**「斜線指令消失」**。

因此現在**完全移除「停止時刪指令」這套機制**，斜線指令**常駐** Discord（像一般 bot 一樣）：

| 時機 | 行為 |
| --- | --- |
| 啟動 | 直接刷新（sync 覆蓋更新），不清空不註銷 |
| `/shutdown`、`stop.bat`、Ctrl+C | **不刪指令**，只斷線；重啟後指令還在 |

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

## TP 偵測的誤判防護（v9.3 → v9.5.2 重做）

TP Multiplayer 外掛自己會在聊天欄印橫幅：

```
* 外部 在 TP Multiplayer 上發現全球所有xbox好友的世界
  在网站查看世界列表，无需添加好友即可加入
  tpmultiplayer.xyz
```

**關鍵事實：`tpmultiplayer.xyz` 這個網址本來就印在橫幅上。**
所以「看到網域」永遠不代表有人透過 TP 進來 —— 舊版拿網域當特徵，每次橫幅刷新就誤報一次，
還會拿橫幅裡的垃圾名字去踢「不存在的玩家」（遊戲回「沒有與選擇器匹配的目標」）。

現在的分工：

| 位置 | 規則 |
| --- | --- |
| DLL `analysis.cpp` | `TP` 分類（來自 `tpmultiplayer` / `TP Multiplayer` 字樣）**一律靜默丟棄，不發任何事件**；橫幅尾巴的二進位噪聲也不會再造成洗頻 |
| DLL `TPJOIN` | 只有 `try connect: <名字>` 與 `玩家嘗試加入 => 名稱:X PFID:Y` 兩條錨點才算數；名字必須 3~16 位英數/底線，含 `tpmultiplayer` 的丟棄 |
| bot `bot.py` | `is_tp_banner()` 再擋一次（含 `tp multiplayer` / `tpmultiplayer` 字樣即判定為橫幅）；`_on_player_message` 也會擋，橫幅不會再被當成玩家發言轉發 |

也就是說：**TP 告警的唯一來源是「真的有人嘗試加入」**，不是螢幕上出現了什麼字。

## 公屏播報（v9.5.3）

偵測到「玩家嘗試加入」時，除了發 Discord，也會**直接打在遊戲公屏上**：

```
§e[TP] §f玩家嘗試加入：§cKonax07
```

| 設定 | 說明 |
| --- | --- |
| `TP_ANNOUNCE=true` | 開關（預設開） |
| `TP_ANNOUNCE_TEXT` | 內容模板，`{player}` 會換成玩家名，`§` 是色碼 |

實作重點（`MCWebSocketServer.tellraw_players`）：

- 走**遊戲自己的 WebSocket**（`tellraw @a`），不需要遊戲在前景，也不用鍵盤注入
- **只發給遊戲那條連線**，不發給橋接器 —— 否則橋接器會再讓 DLL 注入鍵盤，同一句話在公屏出現兩次
- 不等回顯（播報不需要結果），避免拖住 TP 事件的處理
- 同一人 8 秒內只播一次（舊 DLL 可能重複送事件）
- 遊戲 WebSocket 沒連線時直接略過並記 log，**不會**退回鍵盤注入

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

## 上傳 GitHub 的內容（隱私）

倉庫裡**不會**出現這些東西：

| 檔案 | 狀態 |
| --- | --- |
| `.env` | 不上傳。倉庫只放 `.env.example`（Token / Webhook / XUID / API Key 全部留空），要用時複製成 `.env` 再填 |
| `.gitignore`、`push.bat`、`pull.bat` | 只留在本機，不進版本庫 |
| `whitelist.json`、`blacklist.json` | 倉庫裡是空殼 `{}`；本機真實內容靠 `git update-index --skip-worktree` 保護，不會被 push 上去 |

> `skip-worktree` 的效果就是「git 假裝這兩個檔案沒改過」，所以 `git status` 看不到它們的變動（正常）。
> 要臨時取消保護：`git update-index --no-skip-worktree whitelist.json blacklist.json`

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
bot.py                 單檔程式（v9.5），依「目錄」分段：
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

## v9.7 修了什麼（重啟後「dc↔mc 全斷」的那次）

1. **重啟後 40 秒才上線 → 這段時間的事件全部消失**
   以前 `setup_hook()` 會先 `await tree.sync()` 清全域指令，這一步撞到 Discord
   限流要等 30~60 秒，而 `setup_hook` 是在 gateway 連線**之前**跑的，
   結果 bot 根本還沒登入，畫面上卻已經看到 `++ 玩家加入了遊戲`。
   → 改成登入後才在背景清（用一棵空的獨立指令樹，不動本地樹），登入時間回到 1~2 秒。
2. **登入前收到的事件先暫存**：`_send_to_discord` 在 `is_ready()` 之前不再默默丟掉，
   而是存進緩衝（上限 40 則 / 3 分鐘），`on_ready` 時補發。
3. **加入 / 離開的玩家名被截斷**（`gx`、`c_`、`gZs`、`pinyq _ _`）
   DLL 掃描時原本固定往回吃 200 位元組，字串比這更長就會從名字中間開始。
   → 改成往回找到「上一條字串的 NUL」再取，並且要求名字 3 字元以上、
   每個片段至少 2 字元、不含 `tpmultiplayer`。bot 端也再驗一次。
4. **TP 橫幅刷屏**（`!! [TP] TP Multiplayer (tpmultiplayer1mj)`）
   這是**舊版 DLL 殘留在遊戲記憶體裡**繼續吐舊格式事件（每次注入的檔名都不同，
   舊模組不會被覆蓋）。新版 DLL 對 `type == TP` 整段靜默丟棄，
   而且啟動時會回報版本：`[sys] dll_ready（v10.1）`。
   若看到 `[!] DLL 沒有回報版本` 就是舊版還在講話 → 關掉 Minecraft 再重開，
   之後重跑 `start.bat`（舊版約 2 分鐘沒人聽就會自己停下來）。

## 從 GitHub 下載後能直接用嗎？

**要先建置一次**。倉庫裡不放現成的 `MC-WS.exe` / `827dll.dll`
（注入型 DLL 直接發佈會被防毒軟體誤殺，自己編譯一份更乾淨）。

建置一律走 **Gradle wrapper（gradlew）**，不用另外安裝 Gradle ——
第一次執行時 wrapper 會自己下載對應版本的 Gradle 發行版：

```bat
gradlew.bat build
```

編出來的 `827dll.dll` 與 `MC-WS.exe` 會放在專案根目錄，`start.bat` 直接就能用。

### 事前準備

| 需要 | 說明 |
|---|---|
| Visual Studio 2022 Community（免費） | 安裝時勾選「**使用 C++ 的桌面開發**」工作負載（含 MSVC 與 Windows SDK） |
| JDK 17 或更新版本 | Gradle wrapper 需要 Java，`java -version` 有輸出即可 |
| Python 3.10+ | bot 端執行用（第一次跑 `start.bat` 會自動裝 `requirements.txt`） |

### 建置流程

1. `gradlew.bat build` —— 它會自己找到 Visual Studio 的 MSVC 與 Windows SDK，
   直接呼叫 `cl.exe` 編出兩個產物（不用跑 `vcvars64.bat`，也不用先設環境變數）
2. 缺東西時會明確告訴你缺什麼（沒裝 VS C++ 工具 / 沒裝 Windows SDK）
3. 之後 `start.bat` 正常啟動；改了 `src/` 底下的程式碼就再跑一次 `gradlew.bat build`
4. 想清乾淨：`gradlew.bat clean`

> `build.bat` 還留著，但它現在只是 `gradlew.bat build` 的捷徑（習慣雙擊的人用）。

Python 端不用建置：第一次跑 `start.bat` 會自動 `pip install -r requirements.txt`
（discord.py / aiohttp / websockets / python-dotenv），並幫你把 `.env.example`
複製成 `.env`，填入 `DISCORD_TOKEN` 即可。

## 目錄結構：本機執行 vs 上傳副本

本機工作目錄同時是「執行環境」和「倉庫母本」，為了不讓上傳動作碰到
正在用的檔案（`.env`、`*.exe`、`827dll.dll`、名單、日誌），上傳改成
從一個獨立資料夾走：

```
ws服务器\                 ← 本機執行用（start.bat 在這裡跑）
├─ bot.py  src\  ...      ← 程式碼（會被同步到 cloud\）
├─ .env                    ← 只有本機有，永不上傳
├─ 827dll.dll  MC-WS.exe   ← 編譯產物，只在這些位置
├─ whitelist.json / blacklist.json / bot.log / statusboard.json
├─ cloud_sync.py           ← 同步腳本（本機工具）
├─ cloud\                  ← GitHub 倉庫的另一份 checkout，只放能公開的檔案
│   ├─ .git
│   ├─ bot.py  src\  gradlew  build.gradle ...
│   └─ whitelist.json = {}   ← 雲端一律空殼
└─ push.bat / pull.bat     ← 只從 cloud\ 上傳 / 下拉
```

- **`push.bat`**：先把清單內的檔案同步到 `cloud\`，再從 `cloud\` commit + push。
  本機檔案（`.env`、exe、dll、名單、日誌）完全不會被讀寫或上傳。
- **`pull.bat`**：`git -C cloud pull`，再把程式碼複製回本機；
  名單（`whitelist.json` / `blacklist.json`）不會被覆蓋。
- **`python cloud_sync.py --dry-run`**：只預覽會同步／刪除哪些檔案。
- 要上傳的檔案清單寫在 `cloud_sync.py` 的 `FILES` / `SRC_DIRS`；
  不在清單裡的東西不會出現在 GitHub 上（`cloud\` 裡的殘留會被清掉）。

## 桌面控制面板（GUI）

bot 啟動後會另外開一個只綁本機的介面（`http://127.0.0.1:8080`），
`gui.html` 是它的畫面（即時狀態、在線玩家、執行指令、踢人/封禁、bot.log）。
用瀏覽器開 `http://127.0.0.1:8080` 就能用；想要獨立視窗（不是瀏覽器分頁），
把它包成 exe：

```bat
build_gui.bat
```

會用 `pywebview` 把畫面做成原生視窗，再用 `pyinstaller` 打包成
`dist\MC-Bridge-GUI.exe`。之後直接雙擊這個 exe 就是一個桌面程式，
它連的還是本機 `127.0.0.1:8080`（所以 bot 要先跑起來）。

- 端口可用 `.env` 的 `GUI_PORT` 改（預設 8080）
- exe 只在「bot 正在跑」時才有資料；沒跑 start.bat 會顯示「連不上 bot」提示
- `dist\`、`*.spec` 是打包產物，不進 git

## 遊戲內指令（開/關功能）

房主在**遊戲聊天**直接打指令，就能即時開關功能（不用重啟 bot，也不用開 Discord）。
回覆會打在遊戲公屏上：

```text
!ws                查看每個功能現在是開還是關
!ws tp off         關掉 TP 偵測提示
!ws tpkick off     關掉 TP 自動踢出
!ws join off       關掉加入/離開提示
!ws death off      關掉死亡提示
!ws tpann off      關掉 TP 公屏播報
!ws ban off        關掉封禁事件提示
!ws mcdc off       關掉「遊戲 → Discord」聊天轉發
!ws dcmc on        開啟「Discord → 遊戲」聊天轉發
!ws all off        一次全關
!ws all on         一次全開
```

可用的功能名（都有簡寫）：`join`、`death`、`tp`、`tpkick`、`tpann`、`ban`、`mcdc`、`dcmc`、`all`。
`dcmc` 預設值 = `.env` 的 `ONE_WAY_MODE=false`（雙向），要永久改去 `.env`。
`on/off` 也可以打「開/關」。這些只改執行中的記憶體，**重啟後回到 `.env` 的預設值**。
