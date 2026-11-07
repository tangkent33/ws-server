# MC 聊天橋接 v9（單向：遊戲 → Discord）

## 檔案
| 檔案 | 說明 |
| --- | --- |
| `MC-WS.exe` | 建立 `\\.\pipe\mc_chat_827`、把 DLL 注入 `Minecraft.Windows.exe`、把事件轉成 WebSocket 送到 `127.0.0.1:8000` |
| `827dll.dll` | 注入到遊戲內的擷取 DLL（掃描記憶體，抓聊天 / 加入 / 離開 / 死亡 / TP / 封禁） |
| `bot.py` | Discord Bot（單檔 v9.1，已把原本 `bridge/` 套件全部合併進來），接收事件並轉發到頻道 / Webhook |
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
| `KickResult` | DLL 鍵盤注入的執行結果 | 失敗才寫 Discord |
| `BridgeStatus` | DLL 心跳 | 只寫 console（debug 等級） |

## 已知限制
- **單向**：`ONE_WAY_MODE=true`，不會把 Discord 訊息 / 指令送回遊戲。
  要雙向必須讓 DLL 模擬鍵盤輸入（遊戲需在前景），目前刻意不做。
- 聊天（`<名字> 訊息`）是靠記憶體特徵比對，少數訊息可能漏抓或誤判；
  加入 / 離開 / 封禁 / TP 是關鍵字錨定，穩定得多。
- v9.1 起聊天會先驗「原始位元組是合法 UTF-8」再用共用的「像人打的字」閘門過濾，
  記憶體碎片（例如 `9, NI8 VQ: pjT g l`）不會再被當成聊天；代價是極短的亂碼式發言可能被濾掉。
- 非房主、非 OP 一樣可以抓事件（讀的是自己客戶端的記憶體），但**不能**踢人 / 執行指令 ——
  那需要伺服器端權限，客戶端做不到。

## .env 可調
```
ONE_WAY_MODE=true          # false 才會嘗試送指令回遊戲（目前不會成功）
ENABLE_JOIN_LEAVE=true
ENABLE_DEATH=true
ENABLE_TP_ALERT=true
ENABLE_BAN_ALERT=true

TP_KICK=true                          # 偵測到 TP 平台拉人就自動踢（要關改 false）
TP_KICK_REASON=偵測到 TP 平台加入      # 踢出時顯示的理由
TP_KICK_COOLDOWN=20                   # 同一人幾秒內不重複踢
TP_AUTO_BLACKLIST=false               # 要不要順便寫進封禁名單（永久攔）
TP_BAN_MINUTES=0                      # 上者成立時封多久，0 = 永久
```

> 自動踢出是靠 DLL 的鍵盤注入（`kick "名字" §c理由`），**需要遊戲視窗在前景**。
> 若當時切到瀏覽器，踢出會失敗，Discord 會收到「自動踢出沒有成功」的提示。

## 原始碼
`src/dll/*.cpp`、`src/exe/*.cpp`、`bot.py`

## 程式結構

```
bot.py                 單檔程式（v9.1），依「目錄」分段：
  1  設定與日誌          .env 讀取、輪替式 bot.log（5MB × 3）
  2  文字工具            § 色碼、rawtext、UI 洗頻
  3  JSON 檔案存放       白名單 / 封禁名單 / 頻道設定（原子寫入）
  4  WebSocket 伺服器    收事件、去重、送指令、斷線自動重試
  5  HTTP 封禁 API       127.0.0.1:3001
  6  Miaaoo 聯機大廳 API
  7  Discord 客戶端      事件轉發 + 斜線 / 文字指令
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
