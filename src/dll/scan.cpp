#include "common.h"

static std::vector<Region> g_hot;
static std::mutex g_hotMutex;

static void MarkHot(unsigned long long base, size_t size) {
    std::lock_guard<std::mutex> lock(g_hotMutex);
    for (auto& r : g_hot) {
        if (r.base == base) { r.lastHit = GetTickCount(); return; }
    }
    g_hot.push_back({ base, size, GetTickCount() });
    if (g_hot.size() > 12) {
        size_t worst = 0;
        for (size_t i = 1; i < g_hot.size(); i++) if (g_hot[i].lastHit < g_hot[worst].lastHit) worst = i;
        g_hot.erase(g_hot.begin() + worst);
    }
}
static const unsigned char* FindBytes(const unsigned char* hay, size_t hayLen, const void* needle, size_t needleLen, size_t from) {
    if (needleLen == 0 || hayLen < needleLen) return nullptr;
    const unsigned char* p = hay + from;
    const unsigned char* end = hay + hayLen - needleLen;
    while (p <= end) {
        const unsigned char* q = (const unsigned char*)memchr(p, *(const unsigned char*)needle, (size_t)(end - p) + 1);
        if (!q) return nullptr;
        if (memcmp(q, needle, needleLen) == 0) return q;
        p = q + 1;
    }
    return nullptr;
}

static void ScanChatSegments(const unsigned char* buf, size_t len) {
    size_t i = 0;
    while (i < len) {
        while (i < len && buf[i] == 0) i++;
        size_t s = i;
        while (i < len && buf[i] != 0) i++;
        size_t n = i - s;
        if (n < 4 || n > 300) continue;

        // 原始位元組必須是合法 UTF-8；記憶體裡混著指標/二進位的「假字串」在這裡就被擋掉。
        // （舊版先 Sanitize 再判斷，非法位元組被換成空格後看起來就合法了，垃圾因此漏出去）
        if (!ValidUtf8((const char*)buf + s, n)) continue;

        std::string t = Sanitize((const char*)buf + s, n);
        if (t.size() < 4 || t.size() > 200) continue;

        // 只接受「整條字串就是 <名字> 訊息」的情況（色碼已被 Sanitize 去掉），
        // 不接受 "xxx> yyy" 這種，否則會把 UI/資源字串全抓進來。
        if (t[0] != '<') continue;
        size_t gt = t.find('>');
        if (gt == std::string::npos || gt < 3 || gt > 17 || gt + 1 >= t.size() || t[gt + 1] != ' ') continue;
        std::string name = t.substr(1, gt - 1);
        std::string msg = Trim(t.substr(gt + 2));
        if (!ChatNameOk(name)) continue;
        DebugChat(t);                                   // 只記錄「通過名字檢查」的候選，方便校正
        if (!ChatMessageOk(msg)) continue;

        std::string payload = "CHAT|" + name + "|" + msg;
        if (!Duplicate(payload)) SendLine(payload);
    }
}
static void ScanChunk(unsigned long long base, const unsigned char* buf, size_t len) {
    static const char* anchors[] = {
        u8"加入了遊戲", u8"加入了游戏", "joined the game",
        u8"離開了遊戲", u8"离开了游戏", "left the game",
        u8"已被封禁", u8"被封禁", "has been banned",
        " kill by ", " death!", u8" 死了",
        "tpmultiplayer",
    };
    // 只有世界內才會有聊天 HUD 的 UI 元素名稱
    bool worldCtx = FindBytes(buf, len, "chat_visible", 12, 0) != nullptr;
    bool hit = false;
    for (const char* a : anchors) {
        size_t alen = strlen(a);
        size_t pos = 0;
        while (true) {
            const unsigned char* h = FindBytes(buf, len, a, alen, pos);
            if (!h) break;
            size_t off = (size_t)(h - buf);
            pos = off + 1;
            size_t b = off > 200 ? off - 200 : 0;
            size_t e = off + 260 < len ? off + 260 : len;
            HandleWindow((const char*)buf + b, e - b, "EVT", worldCtx);
            hit = true;
        }
    }
    // 聊天：<name> msg（只接受「字串開頭」的 '<'）
    size_t pos = 0;
    int guard = 0;
    while (guard++ < 4000) {
        const unsigned char* h = FindBytes(buf, len, "<", 1, pos);
        if (!h) break;
        size_t off = (size_t)(h - buf);
        pos = off + 1;
        bool atStart = (off == 0) || (buf[off - 1] == 0x00);
        if (!atStart) continue;
        size_t e = off + 240 < len ? off + 240 : len;
        HandleChatWindow((const char*)buf + off, e - off, true);
    }

    ScanChatSegments(buf, len);

    if (hit) MarkHot(base, len);
}

static void ScanRegion(unsigned long long base, size_t size) {
    const size_t CHUNK = 2u * 1024 * 1024;
    const size_t OVERLAP = 1024;
    std::vector<unsigned char> buf;
    size_t done = 0;
    while (done < size) {
        size_t take = min(CHUNK, size - done);
        size_t want = take + (done + take < size ? OVERLAP : 0);
        buf.resize(want);
        SIZE_T got = 0;
        if (ReadProcessMemory(GetCurrentProcess(), (void*)(base + done), buf.data(), want, &got) && got > 64) {
            ScanChunk(base + done, buf.data(), got);
        }
        done += take;
    }
}

// ---------------------------------------------------------------- 主迴圈

// ---------------------------------------------------------------- 指令注入（模擬鍵盤）
DWORD WINAPI ScanThread(LPVOID) {
    SYSTEM_INFO si{};
    GetNativeSystemInfo(&si);
    MEMORY_BASIC_INFORMATION mbi{};
    std::vector<Region> cold;
    size_t coldCursor = 0;
    DWORD lastEnum = 0;

    SendLine("SYS|dll_ready");

    while (true) {
        DWORD now = GetTickCount();

        // 每 15 秒重建一次可掃描區域清單
        if (now - lastEnum > 15000) {
            lastEnum = now;
            cold.clear();
            unsigned char* addr = (unsigned char*)si.lpMinimumApplicationAddress;
            unsigned char* maxAddr = (unsigned char*)si.lpMaximumApplicationAddress;
            while (addr < maxAddr) {
                if (VirtualQueryEx(GetCurrentProcess(), addr, &mbi, sizeof(mbi)) != sizeof(mbi)) break;
                DWORD pr = mbi.Protect & 0xFF;
                if (mbi.State == MEM_COMMIT && mbi.Type == MEM_PRIVATE &&
                    !(mbi.Protect & PAGE_GUARD) && !(mbi.Protect & PAGE_NOACCESS) &&
                    (pr == PAGE_READWRITE || pr == PAGE_WRITECOPY || pr == PAGE_EXECUTE_READWRITE) &&
                    mbi.RegionSize >= 4096 && mbi.RegionSize <= 128ull * 1024 * 1024) {
                    cold.push_back({ (unsigned long long)mbi.BaseAddress, (size_t)mbi.RegionSize, 0 });
                }
                addr += mbi.RegionSize ? mbi.RegionSize : 0x1000;
            }
            coldCursor = 0;
        }

        // 1) 熱區：全部重掃（通常 < 100MB，50~100ms）
        std::vector<Region> hot;
        {
            std::lock_guard<std::mutex> lock(g_hotMutex);
            hot = g_hot;
        }
        for (auto& r : hot) ScanRegion(r.base, r.size);

        // 2) 冷區：還沒找到聊天區時全速找（每次 96MB）；找到後只做低速巡邏（每次 8MB）
        size_t budget = hot.empty() ? (96u * 1024 * 1024) : (8u * 1024 * 1024);
        size_t scanned = 0;
        int guard = 0;
        while (scanned < budget && !cold.empty() && guard++ < 64) {
            if (coldCursor >= cold.size()) coldCursor = 0;
            Region& r = cold[coldCursor];
            size_t take = r.size;
            if (take > budget - scanned) take = budget - scanned;
            ScanRegion(r.base, take);
            scanned += take;
            coldCursor++;
        }

        static DWORD lastPing = 0;
        static int deadTicks = 0;
        if (GetTickCount() - lastPing > 20000) {
            lastPing = GetTickCount();
            SendLine("SYS|ping");
            // 以前連不上就立刻 return 0：遇到橋接器重啟（幾秒鐘）也會永久停掃。
            // 現在改成「連續 2 分鐘都連不上」才退出——這樣橋接器短暫重啟能自動恢復，
            // 真的關掉時舊 DLL 也不會在遊戲裡一直殘留。
            if (g_connectFail > 5) {
                if (++deadTicks >= 6) return 0;
                InterlockedExchange(&g_connectFail, 0);
            } else {
                deadTicks = 0;
            }
        }
        // 橋接器連不上時放慢，避免殘留的 DLL 在背景全速空掃（它不會再自行退出）
        Sleep(g_connectFail > 5 ? 2000 : (hot.empty() ? 20 : 120));
    }
    return 0;
}
