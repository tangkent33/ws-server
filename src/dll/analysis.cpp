#include "common.h"

bool ValidUtf8(const std::string& s) {
    size_t i = 0, n = s.size();
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        int extra;
        if (c < 0x80) extra = 0;
        else if ((c & 0xE0) == 0xC0) extra = 1;
        else if ((c & 0xF0) == 0xE0) extra = 2;
        else if ((c & 0xF8) == 0xF0) extra = 3;
        else return false;
        for (int k = 1; k <= extra; k++) {
            if (i + k >= n) return false;
            if (((unsigned char)s[i + k] & 0xC0) != 0x80) return false;
        }
        i += extra + 1;
    }
    return true;
}

// 原始位元組的嚴格 UTF-8 驗證：記憶體裡的字串若不是合法 UTF-8，幾乎一定是二進位雜訊
// （舊版這條路徑先做 Sanitize，非法位元組被換成空格後「看起來就合法了」，垃圾因此漏進來）
bool ValidUtf8(const char* p, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)p[i];
        int extra;
        if (c == 0) return false;                       // 不該有 NUL
        // Bedrock 的字串常出現「單字節 0xA7 + 格式碼」的舊式色碼（不是 UTF-8 的 C2 A7），
        // 那是合法內容，不能當成壞位元組；不然整條聊天都會被丟掉。
        if (c == 0xA7) {
            if (i + 1 < n) {
                unsigned char nx = (unsigned char)p[i + 1];
                bool fmt = (nx >= '0' && nx <= '9') || (nx >= 'a' && nx <= 'f') || (nx >= 'A' && nx <= 'F') ||
                           nx == 'k' || nx == 'l' || nx == 'm' || nx == 'n' || nx == 'o' || nx == 'r' || nx == 'x';
                if (fmt) { i += 2; continue; }
            }
            return false;
        }
        if (c < 0x80) extra = 0;
        else if ((c & 0xE0) == 0xC0) { if (c < 0xC2) return false; extra = 1; }  // 拒絕 overlong
        else if ((c & 0xF0) == 0xE0) extra = 2;
        else if ((c & 0xF8) == 0xF0) { if (c > 0xF4) return false; extra = 3; }
        else return false;
        if (i + (size_t)extra >= n) return false;
        for (int k = 1; k <= extra; k++) {
            if (((unsigned char)p[i + k] & 0xC0) != 0x80) return false;
        }
        i += (size_t)extra + 1;
    }
    return true;
}

// 字串長度（遇到 NUL 為止）：用來把掃描窗口縮回真正的字串邊界，
// 這樣才不會把緊鄰在後面的記憶體內容一起當成聊天內容。
static size_t StrLenNz(const char* p, size_t n) {
    size_t i = 0;
    while (i < n && p[i] != 0) i++;
    return i;
}

std::string Trim(const std::string& s) {
    size_t b = 0, e = s.size();
    while (b < e && (s[b] == ' ' || s[b] == '\t' || s[b] == '\r')) b++;
    while (e > b && (s[e - 1] == ' ' || s[e - 1] == '\t' || s[e - 1] == '\r')) e--;
    return s.substr(b, e - b);
}

// 移除 Minecraft 的 § 樣式碼（UTF-8 為 C2 A7 + 1 byte；也处理單字節 0xA7）
// 並把控制字元/非法位元組換成空格，讓後續比對穩定。
std::string Sanitize(const char* p, size_t n) {
    std::string out;
    out.reserve(n);
    for (size_t i = 0; i < n; ) {
        unsigned char c = (unsigned char)p[i];
        if (c == 0xC2 && i + 2 < n && (unsigned char)p[i + 1] == 0xA7) { i += 3; continue; }
        // 單字節 0xA7 + 格式碼：只有後面真的是格式碼（0-9a-fk-orx）才跳過，
        // 否則 0xA7 常常是 UTF-8 被截斷後殘留的續位元組，硬跳 2 個位元組會把下一個字吃掉。
        if (c == 0xA7 && i + 1 < n) {
            unsigned char nx = (unsigned char)p[i + 1];
            bool fmt = (nx >= '0' && nx <= '9') || (nx >= 'a' && nx <= 'f') || (nx >= 'A' && nx <= 'F') ||
                       nx == 'k' || nx == 'l' || nx == 'm' || nx == 'n' || nx == 'o' || nx == 'r' || nx == 'x';
            if (fmt) { i += 2; continue; }
        }
        if (c == 0) { out += '\x01'; i++; continue; } if (c < 0x20 || c == 0x7F) { out += ' '; i++; continue; }
        if (c < 0x80) { out += (char)c; i++; continue; }
        // 多位元組：檢查合法性
        int extra = 0;
        if ((c & 0xE0) == 0xC0) extra = 1;
        else if ((c & 0xF0) == 0xE0) extra = 2;
        else if ((c & 0xF8) == 0xF0) extra = 3;
        else { out += ' '; i++; continue; }
        bool ok = (i + extra < n);
        for (int k = 1; ok && k <= extra; k++) {
            if (((unsigned char)p[i + k] & 0xC0) != 0x80) ok = false;
        }
        if (!ok) { out += ' '; i++; continue; }
        out.append(p + i, extra + 1);
        i += extra + 1;
    }
    // 壓縮連續空白
    std::string out2;
    out2.reserve(out.size());
    bool prevSpace = false;
    for (char ch : out) {
        bool sp = (ch == ' ');
        if (sp && prevSpace) continue;
        out2 += ch;
        prevSpace = sp;
    }
    return Trim(out2);
}

// ---------------------------------------------------------------- 去重

static std::unordered_map<std::string, DWORD> g_seen;   // key -> tick
static std::mutex g_seenMutex;

bool Duplicate(const std::string& key) {
    DWORD now = GetTickCount();
    std::lock_guard<std::mutex> lock(g_seenMutex);
    auto it = g_seen.find(key);
    if (it != g_seen.end() && now - it->second < 300000) return true;
    g_seen[key] = now;
    if (g_seen.size() > 4000) {
        for (auto i = g_seen.begin(); i != g_seen.end(); ) {
            if (now - i->second > 600000) i = g_seen.erase(i); else ++i;
        }
    }
    return false;
}

// ---------------------------------------------------------------- 分類


// 去掉名字前面黏上的雜訊 token（"m BadTooth4274"、"T o k yamato120307"）
static std::string StripLeadingJunk(const std::string& name) {
    std::vector<std::string> toks;
    std::string cur;
    for (char c : name) {
        if (c == ' ') { if (!cur.empty()) { toks.push_back(cur); cur.clear(); } }
        else cur += c;
    }
    if (!cur.empty()) toks.push_back(cur);
    if (toks.size() <= 1) return name;

    auto bad = [](const std::string& t) {
        if (t.size() < 3) return true;
        for (size_t i = 0; i < t.size(); i++) {
            unsigned char c = (unsigned char)t[i];
            if (c < 0x80) { if (!(isalnum(c) || c == '_' || c == '-')) return true; }
            else {
                if (c >= 0xE4 && c <= 0xE9 && i + 2 < t.size()) { i += 2; continue; }
                return true;   // 非中文的高位元組 → 雜訊
            }
        }
        return false;
    };

    size_t first = 0;
    while (first + 1 < toks.size() && bad(toks[first])) first++;
    std::string out;
    for (size_t i = first; i < toks.size(); i++) { if (!out.empty()) out += ' '; out += toks[i]; }
    return out;
}

static bool NameOk(const std::string& s) {
    if (s.size() < 2 || s.size() > 24) return false;
    // UI 上的通用字串，不是玩家
    static const char* generic[] = { "A player", "a player", u8"一名玩家", u8"多人", u8"玩家", "Player", "player", u8"無名氏" };
    for (const char* g : generic) if (s == g) return false;
    if (!ValidUtf8(s)) return false;
    static const char* bad[] = { "void", "tab", "null", "true", "false", "struct", "class", "std" };
    for (const char* b : bad) if (s == b) return false;
    for (unsigned char c : s) {
        if (c < 0x20) return false;
        if (c == '<' || c == '>' || c == '|' || c == ':' || c == '/' || c == '\\' ||
            c == '&' || c == '(' || c == ')' || c == '{' || c == '}' || c == '[' || c == ']' ||
            c == '"' || c == '\'' || c == '=' || c == '+' || c == '%' || c == '#' || c == '@' || c == '*') return false;
    }
    return true;
}

// 從「關鍵字前」的文字取玩家名：先切到最後一個字串起點（\x01），再取尾部名字片段
static std::string NameBefore(const std::string& text) {
    std::string t = text;
    size_t sep = t.find_last_of('\x01');
    if (sep != std::string::npos) t = t.substr(sep + 1);
    t = Trim(t);
    if (t.empty()) return "";
    // 取尾部連續的「名字字元」片段（允許中間有單一空白）
    size_t end = t.size();
    size_t start = end;
    while (start > 0) {
        unsigned char c = (unsigned char)t[start - 1];
        if (c == ' ') {
            // 允許名字內含一個空白，但前面必須還是名字字元
            if (start >= 2) {
                unsigned char p = (unsigned char)t[start - 2];
                if (p != ' ' && p != ':' && p != '|') { start--; continue; }
            }
            break;
        }
        if (c < 0x80 && !(isalnum(c) || c == '_' || c == '-')) break;
        if (c >= 0x80) { start--; continue; }   // 中文名
        start--;
        // gamertag 上限 16；抓超過 24 個字元代表這個「名字」其實是一長串
        // 黏在一起的記憶體（中間沒有 NUL），再抓下去只會生出垃圾名。
        if (end - start > 24) return "";
    }
    std::string name = Trim(t.substr(start, end - start));

    // 名字若只剩數字，代表被樣式碼切成兩段：嘗試接上前一個英數詞
    bool allDigits = !name.empty();
    for (unsigned char c : name) if (!isdigit(c)) { allDigits = false; break; }
    if (allDigits) {
        size_t e2 = start;
        while (e2 > 0 && (t[e2 - 1] == ' ' || t[e2 - 1] == '\x01')) e2--;
        size_t s2 = e2;
        while (s2 > 0) {
            unsigned char c = (unsigned char)t[s2 - 1];
            if (c == ' ' || c == '\x01') break;
            s2--;
        }
        std::string prev = t.substr(s2, e2 - s2);
        if (prev.size() < 4) return "";
        name = prev + name;
    }
    return StripLeadingJunk(name);
}

static bool DeathNameOk(const std::string& n) {
    if (n.size() < 3) return false;
    for (char c : n) if (c == ' ') return false;   // 死亡訊息只收單一 token 的名字，避免抓到 UI 句子
    return true;
}

static void Classify(const std::string& s, Found& f) {
    struct KW { const char* kw; const char* type; };
    static const KW kws[] = {
        { u8"加入了遊戲", "JOIN" }, { u8"加入了游戏", "JOIN" }, { "joined the game", "JOIN" },
        { u8"離開了遊戲", "LEAVE" }, { u8"离开了游戏", "LEAVE" }, { "left the game", "LEAVE" },
        { u8"已被封禁", "BAN" }, { u8"被封禁", "BAN" }, { "has been banned", "BAN" },
        { "tpmultiplayer", "TP" }, { "TP Multiplayer", "TP" },
        // TPJOIN 兩種格式：try connect: 名字 / 玩家嘗試加入 => 名稱:X PFID:Y
        // 用 PFID 當錨點（比「名稱:」獨特得多，不會被玩家資訊面板誤觸）
        { "try connect:", "TPJOIN" }, { "PFID:", "TPJOIN" },
        // 死亡訊息（中文/英文/伺服器自訂），順序由具體到寬鬆
        { " kill by ", "DEATH" }, { " death!", "DEATH" }, { u8" 死了", "DEATH" },
    };
    for (const auto& k : kws) {
        size_t p = s.find(k.kw);
        if (p == std::string::npos) continue;
        f.type = k.type; f.kwPos = p;
        if (f.type == "JOIN" || f.type == "LEAVE" || f.type == "DEATH") {
            // 關鍵字後面必須「乾淨」：加入了遊戲 / 加入了遊戲 (8/40)，否則多半是 UI 句子
            std::string tail = s.substr(p);
            size_t z = tail.find('\x01');
            if (z != std::string::npos) tail = tail.substr(0, z);
            tail = Trim(tail);
            if (f.type != "DEATH") {
                std::string kw = k.kw;
                if (tail.compare(0, kw.size(), kw) != 0) { f.type.clear(); continue; }
                std::string rest = Trim(tail.substr(kw.size()));
                bool ok = rest.empty();
                if (!ok && rest.size() >= 3 && rest.front() == '(' && rest.back() == ')') {
                    ok = true;
                    for (size_t i = 1; i + 1 < rest.size(); i++)
                        if (!isdigit((unsigned char)rest[i]) && rest[i] != '/') ok = false;
                }
                if (!ok) { f.type.clear(); continue; }
            }
            f.a = NameBefore(s.substr(0, p));
            if (f.a.empty() || !NameOk(f.a)) { f.type.clear(); continue; }
            if (f.type == "JOIN" || f.type == "LEAVE") {
                // 加入/離開事件一定要是「完整、像樣的 gamertag」：
                //   * 3~16 字元、每個片段至少 2 字元（ChatNameOk）
                //   * 不含 tpmultiplayer（那是外掛橫幅被切下來的碎片）
                // 舊版只要求 2 字元，於是記憶體碎片 "gx" / "c_" / "ys"
                // 全部被當成「有人加入」送到 Discord。
                if (!ChatNameOk(f.a)) { f.type.clear(); continue; }
                std::string lowered = f.a;
                for (auto& ch : lowered) ch = (char)tolower((unsigned char)ch);
                if (lowered.find("tpmultiplayer") != std::string::npos) { f.type.clear(); continue; }
            }
            if (f.type == "DEATH") {
                if (!DeathNameOk(f.a)) { f.type.clear(); continue; }
                f.b = s;
            }
        } else {
            f.a = s;
        }
        return;
    }
}

// ---------------------------------------------------------------- 掃描

static std::string StringContaining(const std::string& s, size_t pos) {
    size_t b = s.rfind('\x01', pos);
    b = (b == std::string::npos) ? 0 : b + 1;
    size_t e = s.find('\x01', pos);
    e = (e == std::string::npos) ? s.size() : e;
    return Trim(s.substr(b, e - b));
}

static std::string NormalizeKey(const std::string& s) {
    std::string out;
    for (unsigned char c : s) if (c != ' ' && c != '\x01') out += (char)c;
    return out;
}

// 選單 / 教學 / Realm 邀請這些 UI 句子裡也會出現「加入了遊戲」「加入」，
// 用特徵詞排除，比檢查 worldCtx 可靠（實測 worldCtx 幾乎永遠是 false）。
static bool LooksLikeUiSentence(const std::string& s) {
    static const char* kUi[] = {
        u8"選單", u8"菜单", u8"Realm", u8"邀請", u8"邀请", u8"教學", u8"教学",
        u8"成就", u8"設定", u8"设定", u8"點擊", u8"点击", "Click", "Tap to",
        u8"好友的世界", u8"好友的世界和 Realm",
    };
    for (const char* k : kUi) {
        if (s.find(k) != std::string::npos) return true;
    }
    return false;
}


// JOIN/LEAVE 專用去重：記憶體裡同一則訊息常被加上不同前綴（"n DeftApollo9066"、"T DeftApollo9066"），
// 只要新名字「以某個剛出現過的名字結尾」就視為同一件事。
//
// 注意：必須把事件類型也算進比對。以前這裡只比名字，結果「某人加入後 2 分鐘內離開」
// 的 LEAVE 會被當成重複丟掉 → 玩家列表永遠留著已離線的人，狀態列人數也虛高。
static bool DuplicateByName(const std::string& type, const std::string& name) {
    struct Recent { std::string type; std::string name; DWORD at; };
    static std::vector<Recent> recent;
    DWORD now = GetTickCount();
    for (auto it = recent.begin(); it != recent.end(); ) {
        if (now - it->at > 120000) it = recent.erase(it); else ++it;
    }
    for (auto& r : recent) {
        if (r.type != type) continue;              // JOIN / LEAVE / DEATH 各自獨立
        if (r.name == name) return true;
        if (name.size() > r.name.size() + 1 &&
            name.compare(name.size() - r.name.size(), r.name.size(), r.name) == 0) return true;
        // 反向：舊名字是新名字的尾段（記憶體裡常被截頭，例如 teelhand34 / hand34）
        if (r.name.size() > name.size() + 1 &&
            r.name.compare(r.name.size() - name.size(), name.size(), name) == 0) return true;
    }

    // 這個人離開了 → 清掉他的 JOIN 記錄，否則「離開幾分鐘後重進」會被 5 分鐘去重吞掉
    if (type == "LEAVE") {
        for (auto it = recent.begin(); it != recent.end(); ) {
            if (it->type == "JOIN" && it->name == name) it = recent.erase(it); else ++it;
        }
        std::lock_guard<std::mutex> lock(g_seenMutex);
        g_seen.erase("JOIN|" + name);
    }

    recent.push_back({ type, name, now });
    if (recent.size() > 200) recent.erase(recent.begin());
    return Duplicate(type + "|" + name);
}

void HandleWindow(const char* win, size_t len, const char* tag, bool worldCtx) {
    std::string s = Sanitize(win, len);
    if (s.size() < 2 || s.size() > 600) return;
    Found f;
    Classify(s, f);
    if (f.type.empty()) return;

    if (f.type == "JOIN" || f.type == "LEAVE" || f.type == "DEATH") {
        // 舊版這裡要求 worldCtx（同一個掃描塊裡同時出現 chat_visible），
        // 但實測那個條件幾乎永遠不成立 → 「有人加入」一次都沒抓到過，
        // 連帶讓「封禁名單玩家一加入就踢」完全沒觸發。
        // 改用「排除 UI 句子」來擋誤報，實務上可靠得多。
        if (LooksLikeUiSentence(s)) return;
        (void)worldCtx;
        if (f.type == "DEATH") {
            // 這個伺服器的死亡廣播固定是 "> 名字 kill by 兇手"，只認這種格式
            std::string line = StringContaining(s, f.kwPos);
            if (line.size() < 6 || line[0] != '>') return;
            std::string rest = Trim(line.substr(1));
            size_t sp = rest.find(' ');
            std::string name = (sp == std::string::npos) ? rest : rest.substr(0, sp);
            if (name.size() < 3 || !NameOk(name)) return;
            if (DuplicateByName(f.type, name)) return;
            SendLine("DEATH|" + name);
            return;
        }

        if (DuplicateByName(f.type, f.a)) return;
        SendLine(f.type + "|" + f.a);
        return;
    }

    // BAN / TP：抽出穩定欄位（xuid / 網域）當作去重鍵，訊息本身重建成乾淨的一行
    std::string line = StringContaining(s, f.kwPos);

    if (f.type == "TPJOIN") {
        // 兩種格式都吃：
        //   玩家嘗試加入 => 名稱:Konax07 PFID:24F8AB7CBA95B146
        //   try connect: Konax07
        std::string name;
        size_t c = line.find(u8"名稱:");
        if (c == std::string::npos) c = line.find(u8"名称:");
        if (c != std::string::npos) {
            name = Trim(line.substr(c + 7));
        } else {
            size_t t = line.find("try connect:");
            if (t == std::string::npos) return;
            name = Trim(line.substr(t + 12));
        }
        size_t sp = name.find_first_of(" \t");
        if (sp != std::string::npos) name = name.substr(0, sp);
        if (name.size() < 3 || name.size() > 16 || !NameOk(name)) return;
        // 嚴格一點：基岩版名字只會是英數/底線。記憶體碎片常混進 '.' 或外掛字樣，
        // 拿去 kick 只會讓遊戲回「沒有與選擇器匹配的目標」。
        for (char c : name) {
            if (!isalnum((unsigned char)c) && c != '_') return;
        }
        {
            std::string lowered = name;
            for (auto& c : lowered) c = (char)tolower((unsigned char)c);
            if (lowered.find("tpmultiplayer") != std::string::npos) return;
        }
        std::string key = "TPJOIN|" + name;
        if (Duplicate(key)) return;
        SendLine("TPJOIN|" + name);
        return;
    }

    if (f.type == "BAN") {
        size_t x = line.find("xuid");
        if (x == std::string::npos) return;
        size_t i = x + 4;
        while (i < line.size() && line[i] == ' ') i++;
        size_t d = i;
        while (d < line.size() && isdigit((unsigned char)line[d])) d++;
        if (d - i < 15) return;                 // 真正的 xuid 是 16 位數字，殘缺片段直接丟棄
        std::string id = line.substr(i, d - i);

        std::string text = u8"玩家xuid " + id + u8" 已被封禁";
        size_t r = line.find(u8"原因");
        if (r != std::string::npos && r > x) {
            std::string reason = line.substr(r, 48);
            // 切掉尾端黏上來的二進位雜訊：只保留到最後一個中文字為止
            size_t lastCjk = std::string::npos;
            for (size_t k = 0; k + 2 < reason.size(); k++) {
                unsigned char c = (unsigned char)reason[k];
                if (c >= 0xE4 && c <= 0xE9 &&
                    ((unsigned char)reason[k + 1] & 0xC0) == 0x80 &&
                    ((unsigned char)reason[k + 2] & 0xC0) == 0x80) lastCjk = k + 3;
            }
            if (lastCjk != std::string::npos && lastCjk < reason.size()) reason.resize(lastCjk);
            text += " " + reason;
        }
        std::string key = "BAN|" + id;
        if (Duplicate(key)) return;
        SendLine("BAN|" + text);
        return;
    }

    if (f.type == "TP") {
        // 這裡的每一行都是「遊戲 / 外掛自己印的橫幅」，不是有人加入：
        //   * 外部 在 TP Multiplayer 上發現全球所有xbox好友的世界
        //     在网站查看世界列表，无需添加好友即可加入
        //     tpmultiplayer.xyz
        // 關鍵：tpmultiplayer.xyz 這個網址本來就印在橫幅上，所以「看到網域」永遠
        // 不代表有人透過 TP 進來。以前靠網域判斷 → 每次刷新都誤報一次，
        // 還會被當成「偵測到入侵」去踢不存在的玩家。
        // 真正的加入偵測走上面的 TPJOIN（try connect: / PFID:），那才是玩家名。
        (void)line;
        return;
    }
}

static bool ContainsCJK(const std::string& s) {
    for (size_t i = 0; i + 2 < s.size(); i++) {
        unsigned char c = (unsigned char)s[i];
        if (c >= 0xE4 && c <= 0xE9 &&
            ((unsigned char)s[i + 1] & 0xC0) == 0x80 &&
            ((unsigned char)s[i + 2] & 0xC0) == 0x80) return true;
    }
    return false;
}

// 聊天內容必須「像人打的字」：>=90% 是可列印/中文，且至少有一個 >=3 字母的英文詞或含中文
bool TextLike(const std::string& s) {
    size_t good = 0, total = 0, run = 0, maxRun = 0;
    for (unsigned char c : s) {
        total++;
        if (c >= 0x80) { good++; run = 0; }
        else if (isalnum(c)) { good++; run++; if (run > maxRun) maxRun = run; }
        else if (strchr(" .,!?'\"()-_+*:;<", c)) { good++; run = 0; }
        else run = 0;
    }
    if (total < 2) return false;
    if (good * 100 < total * 90) return false;

    // 記憶體碎片特徵：一堆 1~2 字元的 token（"p9 jp9 n Ej ..."）
    int tokens = 0, shortTokens = 0, cur = 0;
    for (size_t i = 0; i <= s.size(); i++) {
        if (i == s.size() || s[i] == ' ') {
            if (cur > 0) { tokens++; if (cur <= 2) shortTokens++; }
            cur = 0;
        } else {
            cur++;
        }
    }
    if (tokens >= 6 && shortTokens * 100 / tokens > 50) return false;

    return maxRun >= 3 || ContainsCJK(s);
}

// 聊天訊息品質閘門：三條掃描路徑共用同一套標準。
// 舊版只有 HandleChatWindow 這條有完整過濾，ScanChatSegments（NUL 切段）
// 幾乎裸奔，記憶體碎片 "pjT g l" 就是從那裡漏出去的。
bool ChatMessageOk(const std::string& msg) {
    if (msg.size() < 1 || msg.size() > 200) return false;
    if (!ValidUtf8(msg)) return false;
    if (!TextLike(msg)) {
        // TextLike 要求「至少一個 >=3 字元的詞」，短聊天（hi / gg / ok / 666）會被誤殺。
        // 這裡對短訊息改用「字元乾淨度」判斷。
        if (msg.size() > 6) return false;
        for (unsigned char c : msg) {
            if (c < 0x20) return false;
            if (c < 0x80 && !isalnum(c) && c != ' ' && !strchr("!?.,:;'\"-", c)) return false;
        }
    }

    int tokens = 0, singles = 0, longTokens = 0, cur = 0;
    for (size_t i = 0; i <= msg.size(); i++) {
        if (i == msg.size() || msg[i] == ' ') {
            if (cur > 0) { tokens++; if (cur == 1) singles++; if (cur >= 3) longTokens++; }
            cur = 0;
        } else cur++;
    }
    if (tokens == 0) return false;
    if (singles >= 3) return false;                          // "pjT g l"
    if (tokens > 1 && singles * 100 >= tokens * 50) return false;
    // 長訊息卻沒有任何 >=3 字元的詞 → 碎片；短訊息（hi / gg / ok）放行
    if (longTokens == 0 && !ContainsCJK(msg) && msg.size() >= 8) return false;

    // 資源路徑 / 格式字串 / 組語碎片
    if (msg.find("textures/") != std::string::npos || msg.find("textures\\") != std::string::npos) return false;
    if (msg.find(".json") != std::string::npos) return false;
    if (msg.find("%s") != std::string::npos || msg.find("%d") != std::string::npos) return false;
    if (msg.find('\\') != std::string::npos) return false;
    if (msg.find("0x") != std::string::npos) return false;
    if (msg.find("::") != std::string::npos || msg.find("__") != std::string::npos) return false;
    if (msg.find("->") != std::string::npos) return false;
    if (msg.find('{') != std::string::npos || msg.find('}') != std::string::npos) return false;
    if (msg.find('=') != std::string::npos) return false;
    return true;
}

bool ChatNameOk(const std::string& n) {
    if (n.size() < 3 || n.size() > 16) return false;
    if (!NameOk(n)) return false;

    // 每個空白分隔的片段：至少 2 個字元，且必須「有字母/中文」或「全是數字」。
    // 這樣可以擋掉 "9, NI8 VQ" 這種把符號黏在數字後面的記憶體碎片，
    // 同時保留正常玩家名（含空格的 gamertag 也照樣通過）。
    size_t tokenStart = 0;
    int tokens = 0;
    for (size_t i = 0; i <= n.size(); i++) {
        if (i == n.size() || n[i] == ' ') {
            size_t len = i - tokenStart;
            if (len < 2) return false;
            bool text = false, allDigit = true;
            for (size_t k = tokenStart; k < i; k++) {
                unsigned char c = (unsigned char)n[k];
                if (c >= 0x80) { text = true; allDigit = false; }
                else if (isalpha(c)) { text = true; allDigit = false; }
                else if (!isdigit(c)) allDigit = false;
            }
            if (!text && !allDigit) return false;
            if (++tokens > 3) return false;
            tokenStart = i + 1;
        }
    }
    return true;
}

void HandleChatWindow(const char* win, size_t len, bool atStringStart) {
    if (!atStringStart) return;          // 只接受字串開頭的 '<'（前面是 0x00）

    // 先把窗口縮回真正的字串邊界：舊版固定吃 240 位元組，
    // 字串後面的記憶體（常是二進位）就被當成訊息尾巴 → 看起來就是亂碼。
    size_t sn = StrLenNz(win, len);
    if (sn < 5 || sn > 300) return;
    if (!ValidUtf8(win, sn)) return;     // 原始位元組不是合法 UTF-8 → 一定是雜訊

    std::string s = Sanitize(win, sn);
    if (s.size() < 5 || s.size() > 300) return;
    DumpChatRaw(s);
    if (s[0] != '<') return;
    size_t gt = s.find('>');
    if (gt == std::string::npos || gt < 4 || gt > 17) return;
    if (gt + 1 >= s.size() || s[gt + 1] != ' ') return;

    std::string name = s.substr(1, gt - 1);
    if (!ChatNameOk(name)) return;

    std::string msg = Trim(s.substr(gt + 2));
    { size_t z = msg.find('\x01'); if (z != std::string::npos) msg = Trim(msg.substr(0, z)); }
    if (!ChatMessageOk(msg)) return;

    std::string payload = "CHAT|" + name + "|" + msg;
    if (!Duplicate(payload)) SendLine(payload);
}
static void HandleSayWindow(const char* win, size_t len, const char* sep) {
    size_t sn = StrLenNz(win, len);
    if (sn < 5) return;
    if (!ValidUtf8(win, sn)) return;

    std::string s = Sanitize(win, sn);
    DumpChatRaw(s);
    size_t p = s.find(sep);
    if (p == std::string::npos) return;

    std::string left = s.substr(0, p);
    size_t z = left.find_last_of('\x01');
    if (z != std::string::npos) left = left.substr(z + 1);
    left = Trim(left);
    size_t sp = left.find_last_of(' ');
    std::string name = Trim(sp == std::string::npos ? left : left.substr(sp + 1));
    if (name.size() < 3 || name.size() > 20 || !NameOk(name)) return;

    std::string msg = Trim(s.substr(p + strlen(sep)));
    size_t e = msg.find('\x01');
    if (e != std::string::npos) msg = Trim(msg.substr(0, e));
    if (!ChatMessageOk(msg)) return;

    std::string payload = "CHAT|" + name + "|" + msg;
    if (!Duplicate(payload)) SendLine(payload);
}

// 把「看起來像聊天」的字串記錄下來，方便校正（上限 300 條，寫在遊戲的 LocalState）
static bool TextyLook(const std::string& t) {
    int run = 0, maxRun = 0, cjk = 0;
    for (unsigned char c : t) {
        if (c < 0x80) {
            if (!isprint(c) && c != ' ') return false;
            if (isalnum(c)) { run++; if (run > maxRun) maxRun = run; } else run = 0;
        } else {
            if (c >= 0xE4 && c <= 0xE9) cjk++;
            run = 0;
        }
    }
    return cjk >= 2 || maxRun >= 5;
}

void DumpChatRaw(const std::string& t) {
    // 校正用：把「掃描到的每一條字串」原樣寫到 chat_raw.txt（不去重、不篩選），
    // 讓我們能看出當前版本聊天到底長什麼樣。只在校正期使用。
    static std::unordered_set<std::string> seen;
    static DWORD lastReset = GetTickCount();
    // 每 5 分鐘清空一次去重集合：否則 30000 條滿了之後 chat_raw 就不再更新，
    // 換房間 / 進多人世界後就看不到 DLL 到底掃到什麼，很難校正聊天格式。
    if (GetTickCount() - lastReset > 300000) {
        lastReset = GetTickCount();
        seen.clear();
    }
    if (seen.size() > 30000) return;
    if (!seen.insert(t).second) return;

    static HANDLE hf = INVALID_HANDLE_VALUE;
    if (hf == INVALID_HANDLE_VALUE) {
        wchar_t p[MAX_PATH]; DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", p, MAX_PATH);
        std::wstring path = std::wstring(p, n) + L"\\Packages\\Microsoft.MinecraftUWP_8wekyb3d8bbwe\\LocalState\\chat_raw.txt";
        hf = CreateFileW(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (hf == INVALID_HANDLE_VALUE) return;
    }
    std::string line = t.substr(0, 160) + "\r\n";
    DWORD w = 0;
    WriteFile(hf, line.data(), (DWORD)line.size(), &w, nullptr);
}

void DebugChat(const std::string& t) {
    static std::unordered_set<unsigned long long> seen;
    unsigned long long h = 1469598103934665603ull;
    for (unsigned char c : t) { h ^= c; h *= 1099511628211ull; }
    if (!TextyLook(t)) return;
    if (!seen.insert(h).second || seen.size() > 500) return;

    static HANDLE hf = INVALID_HANDLE_VALUE;
    if (hf == INVALID_HANDLE_VALUE) {
        wchar_t p[MAX_PATH]; DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", p, MAX_PATH);
        std::wstring path = std::wstring(p, n) + L"\\Packages\\Microsoft.MinecraftUWP_8wekyb3d8bbwe\\LocalState\\chat_seen.txt";
        hf = CreateFileW(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (hf == INVALID_HANDLE_VALUE) return;
    }
    std::string line = t + "\r\n";
    DWORD w = 0;
    WriteFile(hf, line.data(), (DWORD)line.size(), &w, nullptr);
}

// 掃描所有 NUL 分隔字串，找出聊天格式：
