#include "common.h"

SOCKET g_sock = INVALID_SOCKET;
std::mutex g_wsMutex;
volatile LONG g_reqSeq = 0;

static bool TcpSendAll(const std::string& data) {
    size_t off = 0;
    while (off < data.size()) {
        int n = send(g_sock, data.data() + off, (int)(data.size() - off), 0);
        if (n <= 0) return false;
        off += (size_t)n;
    }
    return true;
}

static bool TcpRecvAll(char* buf, size_t len) {
    size_t off = 0;
    while (off < len) {
        int n = recv(g_sock, buf + off, (int)(len - off), 0);
        if (n <= 0) return false;
        off += (size_t)n;
    }
    return true;
}

// 送出一幀文字訊息（client 端必須遮罩）
static bool WsSendFrameLocked(const std::string& payload) {
    if (g_sock == INVALID_SOCKET) return false;

    unsigned char head[14];
    size_t hlen = 0;
    head[hlen++] = 0x81;                       // FIN + text
    unsigned char mask[4];
    for (int i = 0; i < 4; i++) mask[i] = (unsigned char)(rand() & 0xFF);

    size_t len = payload.size();
    if (len < 126) {
        head[hlen++] = (unsigned char)(0x80 | len);
    } else if (len < 65536) {
        head[hlen++] = 0x80 | 126;
        head[hlen++] = (unsigned char)(len >> 8);
        head[hlen++] = (unsigned char)(len & 0xFF);
    } else {
        head[hlen++] = 0x80 | 127;
        for (int i = 7; i >= 0; i--) head[hlen++] = (unsigned char)((len >> (8 * i)) & 0xFF);
    }
    memcpy(head + hlen, mask, 4);
    hlen += 4;

    std::string frame((char*)head, hlen);
    frame.resize(hlen + len);
    for (size_t i = 0; i < len; i++) frame[hlen + i] = (char)(payload[i] ^ mask[i & 3]);
    return TcpSendAll(frame);
}

static bool WsConnectLocked() {
    if (g_sock != INVALID_SOCKET) return true;

    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return false;

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)WS_PORT);
    inet_pton(AF_INET, WS_HOST, &addr.sin_addr);

    if (connect(s, (sockaddr*)&addr, sizeof(addr)) != 0) {
        closesocket(s);
        return false;
    }
    g_sock = s;

    std::string key = "dGhlIHNhbXBsZSBub25jZQ==";
    std::string req =
        "GET / HTTP/1.1\r\n"
        "Host: 127.0.0.1:8000\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: " + key + "\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n";
    if (!TcpSendAll(req)) { closesocket(g_sock); g_sock = INVALID_SOCKET; return false; }

    // 讀到 \r\n\r\n 為止
    std::string resp;
    char c;
    while (resp.size() < 4096) {
        if (!TcpRecvAll(&c, 1)) break;
        resp += c;
        if (resp.size() >= 4 && resp.compare(resp.size() - 4, 4, "\r\n\r\n") == 0) break;
    }
    if (resp.find("101") == std::string::npos) {
        closesocket(g_sock);
        g_sock = INVALID_SOCKET;
        return false;
    }
    return true;
}

// 接收執行緒：回 pong、處理 close、回覆 commandRequest（本版不執行指令）
// 常駐的指令管線：由專用執行緒維持連線，送指令時只做 WriteFile（不阻塞接收執行緒）
DWORD WINAPI WsRecvThread(LPVOID) {
    while (true) {
        if (g_sock == INVALID_SOCKET) { Sleep(500); continue; }

        unsigned char h[2];
        if (!TcpRecvAll((char*)h, 2)) { Sleep(300); continue; }

        int opcode = h[0] & 0x0F;
        bool masked = (h[1] & 0x80) != 0;
        unsigned long long len = h[1] & 0x7F;
        if (len == 126) {
            unsigned char e[2];
            if (!TcpRecvAll((char*)e, 2)) continue;
            len = ((unsigned long long)e[0] << 8) | e[1];
        } else if (len == 127) {
            unsigned char e[8];
            if (!TcpRecvAll((char*)e, 8)) continue;
            len = 0;
            for (int i = 0; i < 8; i++) len = (len << 8) | e[i];
        }
        unsigned char mask[4] = { 0, 0, 0, 0 };
        if (masked && !TcpRecvAll((char*)mask, 4)) continue;
        if (len > (1u << 20)) { Sleep(100); continue; }

        std::string payload((size_t)len, 0);
        if (len && !TcpRecvAll(&payload[0], (size_t)len)) continue;
        if (masked) for (size_t i = 0; i < payload.size(); i++) payload[i] ^= mask[i & 3];

        if (opcode == 0x9) {                       // ping → 必須回「有遮罩」的 pong
            std::lock_guard<std::mutex> lock(g_wsMutex);
            if (g_sock != INVALID_SOCKET) {
                unsigned char pkt[8];
                size_t n = len > 4 ? 4 : (size_t)len;
                pkt[0] = 0x8A;
                pkt[1] = (unsigned char)(0x80 | n);
                unsigned char mk[4] = { 0x11, 0x22, 0x33, 0x44 };
                memcpy(pkt + 2, mk, 4);
                for (size_t i = 0; i < n; i++) pkt[6 + i] = (unsigned char)payload[i] ^ mk[i & 3];
                send(g_sock, (const char*)pkt, (int)(6 + n), 0);
            }
        } else if (opcode == 0x8) {                // close
            std::lock_guard<std::mutex> lock(g_wsMutex);
            if (g_sock != INVALID_SOCKET) { closesocket(g_sock); g_sock = INVALID_SOCKET; }
        } else if (opcode == 0x1) {
            // bot 送來的指令：本版單向，回覆不支援，避免 Python 端等到逾時
            auto field = [&](const char* key) -> std::string {
                std::string pat = std::string("\"") + key + "\"";
                size_t p = payload.find(pat);
                if (p == std::string::npos) return "";
                size_t c = payload.find(':', p + pat.size());
                if (c == std::string::npos) return "";
                size_t q = payload.find('"', c);
                if (q == std::string::npos) return "";
                std::string out;
                for (size_t i = q + 1; i < payload.size() && payload[i] != '"'; ) {
                    if (payload[i] == '\\' && i + 1 < payload.size()) {
                        char n = payload[i + 1];
                        if (n == 'u' && i + 5 < payload.size()) {
                            unsigned code = (unsigned)strtoul(payload.substr(i + 2, 4).c_str(), nullptr, 16);
                            if (code < 0x80) out += (char)code;
                            else if (code < 0x800) { out += (char)(0xC0 | (code >> 6)); out += (char)(0x80 | (code & 0x3F)); }
                            else { out += (char)(0xE0 | (code >> 12)); out += (char)(0x80 | ((code >> 6) & 0x3F)); out += (char)(0x80 | (code & 0x3F)); }
                            i += 6;
                        } else { out += n; i += 2; }
                    } else { out += payload[i]; i++; }
                }
                return out;
            };
            std::string id = field("requestId");
            std::string cmd = field("commandLine");
            if (payload.find("\"INJECT\"") != std::string::npos) {
                bool ok = !cmd.empty() && SendCommandToDll(cmd);
                CmdLog(std::string("inject: ") + cmd + (ok ? " -> ok" : " -> fail"));
                continue;
            }
            CmdLog("recv cmd: " + cmd);
            if (!id.empty()) {
                bool ok = !cmd.empty();   // 交給 bot 廣播給所有連線（含遊戲原生 WebSocket）
                std::string resp =
                    "{\"header\":{\"version\":1,\"requestId\":\"" + id +
                    "\",\"messageType\":\"commandResponse\",\"messagePurpose\":\"commandResponse\"},"
                    "\"body\":{\"statusCode\":" + (ok ? "0" : "-1") + ",\"statusMessage\":\"" +
                    (ok ? "sent" : "empty command") + "\"}}";
                std::lock_guard<std::mutex> lock(g_wsMutex);
                if (g_sock != INVALID_SOCKET) WsSendFrameLocked(resp);
            }
        }
    }
    return 0;
}

std::string EscapeJson(const std::string& s) {
    std::string out;
    for (unsigned char c : s) {
        switch (c) {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20) { char b[8]; sprintf_s(b, "\\u%04X", c); out += b; }
            else out += (char)c;
        }
    }
    return out;
}

void SendEvent(const std::string& eventName, const std::string& bodyJson) {
    char rid[64];
    sprintf_s(rid, "%08X-%08X", GetTickCount(), (unsigned)InterlockedIncrement(&g_reqSeq));
    std::string json =
        "{\"header\":{\"version\":1,\"requestId\":\"" + std::string(rid) +
        "\",\"messageType\":\"event\",\"messagePurpose\":\"event\",\"eventName\":\"" + eventName +
        "\"},\"body\":" + bodyJson + "}";

    std::lock_guard<std::mutex> lock(g_wsMutex);
    if (g_sock == INVALID_SOCKET && !WsConnectLocked()) return;
    if (!WsSendFrameLocked(json)) {
        closesocket(g_sock);
        g_sock = INVALID_SOCKET;
    }
}

void EnsureConnected() {
    bool reconnected = false;
    {
        std::lock_guard<std::mutex> lock(g_wsMutex);
        if (g_sock == INVALID_SOCKET) reconnected = WsConnectLocked();
    }
    if (reconnected) SendEvent("BridgeHello", "{}");
}

// ---------------------------------------------------------------- 事件轉換
