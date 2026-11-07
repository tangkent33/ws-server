#include "common.h"

static std::vector<std::string> Split(const std::string& s, char sep, size_t maxParts) {
    std::vector<std::string> out;
    size_t start = 0;
    while (out.size() + 1 < maxParts) {
        size_t p = s.find(sep, start);
        if (p == std::string::npos) break;
        out.push_back(s.substr(start, p - start));
        start = p + 1;
    }
    out.push_back(s.substr(start));
    return out;
}

static void PrintLine(const std::string& type, const std::string& a, const std::string& b) {
    SYSTEMTIME st; GetLocalTime(&st);
    char t[16]; sprintf_s(t, "%02d:%02d:%02d", st.wHour, st.wMinute, st.wSecond);
    SetColor(C_GRAY); printf("[%s] ", t);
    if (type == "CHAT") {
        SetColor(C_CYAN); printf("%s", a.c_str());
        SetColor(C_GRAY); printf(": ");
        SetColor(C_RESET); printf("%s\n", b.c_str());
    } else if (type == "JOIN") {
        SetColor(C_GREEN); printf("++ %s 加入了遊戲\n", a.c_str()); SetColor(C_RESET);
    } else if (type == "LEAVE") {
        SetColor(C_YELLOW); printf("-- %s 離開了遊戲\n", a.c_str()); SetColor(C_RESET);
    } else if (type == "DEATH") {
        SetColor(C_RED); printf("xx %s\n", a.c_str()); SetColor(C_RESET);
    } else if (type == "TPJOIN") {
        SetColor(C_YELLOW); printf("!! [TP加入] %s\n", a.c_str()); SetColor(C_RESET);
    } else if (type == "BAN") {
        SetColor(C_RED); printf("!! %s\n", a.c_str()); SetColor(C_RESET);
    } else if (type == "TP") {
        SetColor(C_YELLOW); printf("!! [TP] %s\n", a.c_str()); SetColor(C_RESET);
    } else {
        SetColor(C_GRAY); printf(".. %s\n", a.c_str()); SetColor(C_RESET);
    }
}

void HandleEventLine(const std::string& line) {
    if (line.empty()) return;
    auto f = Split(line, '|', 3);
    std::string type = f.size() > 0 ? f[0] : "";
    std::string a = f.size() > 1 ? f[1] : "";
    std::string b = f.size() > 2 ? f[2] : "";

    if (type == "CHAT") {
        if (a.empty() || b.empty()) return;
        PrintLine(type, a, b);
        SendEvent("PlayerMessage", "{\"sender\":\"" + EscapeJson(a) + "\",\"message\":\"" + EscapeJson(b) + "\",\"type\":\"chat\"}");
    } else if (type == "JOIN" || type == "LEAVE") {
        if (a.empty()) return;
        PrintLine(type, a, b);
        SendEvent(type == "JOIN" ? "PlayerJoin" : "PlayerLeave", "{\"player\":{\"name\":\"" + EscapeJson(a) + "\"}}");
    } else if (type == "DEATH") {
        if (a.empty()) return;
        PrintLine(type, a, b);
        SendEvent("PlayerDied", "{\"player\":{\"name\":\"" + EscapeJson(a) + "\"},\"message\":\"" + EscapeJson(b) + "\"}");
    } else if (type == "TPJOIN") {
        if (a.empty()) return;
        PrintLine("TP", a, b);
        SendEvent("TP_JOIN", "{\"player\":{\"name\":\"" + EscapeJson(a) + "\"}}");
    } else if (type == "BAN") {
        PrintLine(type, a, b);
        SendEvent("PlayerBan", "{\"message\":\"" + EscapeJson(a) + "\"}");
    } else if (type == "TP") {
        PrintLine(type, a, b);
        SendEvent("TP_DETECT", "{\"message\":\"" + EscapeJson(a) + "\"}");
    } else if (type == "CMDRESULT") {
        // DLL 用鍵盤注入送指令後的結果（ok|injected / fail|game not focused）
        SetColor(C_GRAY); printf("[cmd] %s %s\n", a.c_str(), b.c_str()); SetColor(C_RESET);
        SendEvent("KickResult", "{\"message\":\"" + EscapeJson(a + "|" + b) + "\"}");
    } else if (type == "SYS") {
        if (a == "ping") return;

        if (a == "dll_ready") {
            // b = DLL 的版本戳。空白 = 遊戲裡跑的是「沒有戳記」的舊版 DLL；
            // 與本體不同 = 剛 rebuild，但遊戲記憶體裡還是舊的那份。
            if (b.empty()) {
                SetColor(C_RED);
                printf("[!] DLL 沒有回報版本 → 遊戲裡幾乎確定是舊版 DLL 殘留\n");
                printf("    舊版會把 TP 橫幅切成 \"tpmultiplayer1mj\" 這種假玩家名，\n");
                printf("    也會把加入事件的玩家名截斷。請關掉 Minecraft 再重開，然後重跑 start.bat\n");
                SetColor(C_RESET);
            } else if (b != BUILD_ID) {
                SetColor(C_RED);
                printf("[!] DLL 版本 %s 與橋接器版本 %s 不同，事件格式可能不相容\n", b.c_str(), BUILD_ID);
                printf("    請關掉 Minecraft 再重開，然後重跑 start.bat\n");
                SetColor(C_RESET);
            } else {
                SetColor(C_GREEN); printf("[sys] dll_ready（v%s）\n", b.c_str()); SetColor(C_RESET);
            }
            SendEvent("BridgeStatus", "{\"message\":\"" + EscapeJson(a + (b.empty() ? "" : "|" + b)) + "\"}");
            return;
        }

        SetColor(C_GRAY); printf("[sys] %s\n", a.c_str()); SetColor(C_RESET);
        SendEvent("BridgeStatus", "{\"message\":\"" + EscapeJson(a) + "\"}");
    }
}

// ---------------------------------------------------------------- 管線

