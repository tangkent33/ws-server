// httpserver.cpp — 取代原本的 named pipe：收 DLL 的 POST、發指令給 DLL
#include "common.h"

#include <deque>

int g_port = 8770;

static std::deque<std::string> g_cmds;
static std::mutex g_cmdMutex;

// ---------------------------------------------------------------- 除錯紀錄
void CmdLog(const std::string& s) {
    FILE* f = fopen("cmdlog.txt", "a");
    if (f) { fprintf(f, "%s\n", s.c_str()); fclose(f); }
}

// ---------------------------------------------------------------- 指令佇列
bool SendCommandToDll(const std::string& cmd) {
    std::lock_guard<std::mutex> lock(g_cmdMutex);
    if (g_cmds.size() > 50) return false;      // 沒人來拿就別無限長
    g_cmds.push_back(cmd);
    return true;
}

static bool PopCommand(std::string& out) {
    std::lock_guard<std::mutex> lock(g_cmdMutex);
    if (g_cmds.empty()) return false;
    out = g_cmds.front();
    g_cmds.pop_front();
    return true;
}

// ---------------------------------------------------------------- HTTP 工具
static bool RecvAll(SOCKET s, char* buf, int len) {
    int got = 0;
    while (got < len) {
        int n = recv(s, buf + got, len - got, 0);
        if (n <= 0) return false;
        got += n;
    }
    return true;
}

static void SendAll(SOCKET s, const std::string& data) {
    size_t off = 0;
    while (off < data.size()) {
        int n = send(s, data.data() + off, (int)(data.size() - off), 0);
        if (n <= 0) return;
        off += (size_t)n;
    }
}

static void Respond(SOCKET s, int code, const std::string& body) {
    const char* status = (code == 200) ? "200 OK" : (code == 204 ? "204 No Content" : "400 Bad Request");
    std::string head = std::string("HTTP/1.1 ") + status + "\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Connection: close\r\n"
        "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n";
    SendAll(s, head + body);
}

static void HandleClient(SOCKET s) {
    std::string req;
    char buf[2048];

    // 讀到 header 結束
    while (req.find("\r\n\r\n") == std::string::npos && req.size() < 16384) {
        int n = recv(s, buf, sizeof(buf), 0);
        if (n <= 0) return;
        req.append(buf, n);
    }

    size_t headEnd = req.find("\r\n\r\n");
    if (headEnd == std::string::npos) return;

    std::string head = req.substr(0, headEnd);
    std::string body = req.substr(headEnd + 4);

    // 第一行：METHOD /path HTTP/1.1
    std::string method, path;
    {
        size_t sp1 = head.find(' ');
        size_t sp2 = head.find(' ', sp1 + 1);
        if (sp1 == std::string::npos) return;
        method = head.substr(0, sp1);
        path = (sp2 == std::string::npos) ? head.substr(sp1 + 1) : head.substr(sp1 + 1, sp2 - sp1 - 1);
    }

    // Content-Length
    size_t cl = 0;
    {
        std::string lower = head;
        for (auto& c : lower) c = (char)tolower((unsigned char)c);
        size_t p = lower.find("content-length:");
        if (p != std::string::npos) cl = (size_t)strtoul(head.c_str() + p + 15, nullptr, 10);
    }
    while (body.size() < cl) {
        int n = recv(s, buf, sizeof(buf), 0);
        if (n <= 0) break;
        body.append(buf, n);
    }

    if (method == "POST" && path.rfind("/event", 0) == 0) {
        size_t start = 0;
        while (start < body.size()) {
            size_t nl = body.find('\n', start);
            std::string line = (nl == std::string::npos) ? body.substr(start) : body.substr(start, nl - start);
            while (!line.empty() && line.back() == '\r') line.pop_back();
            if (!line.empty()) HandleEventLine(line);
            if (nl == std::string::npos) break;
            start = nl + 1;
        }
        Respond(s, 204, "");
        return;
    }

    if (method == "GET" && path.rfind("/command", 0) == 0) {
        std::string cmd;
        if (PopCommand(cmd)) Respond(s, 200, cmd);
        else Respond(s, 204, "");
        return;
    }

    Respond(s, 400, "bad request");
}

// ---------------------------------------------------------------- 伺服器
DWORD WINAPI HttpServerThread(LPVOID) {
    WSADATA wsa{};
    WSAStartup(MAKEWORD(2, 2), &wsa);

    SOCKET srv = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (srv == INVALID_SOCKET) return 0;

    int reuse = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, (const char*)&reuse, sizeof(reuse));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)g_port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

    if (bind(srv, (sockaddr*)&addr, sizeof(addr)) != 0) {
        SetColor(C_RED);
        printf("[!] HTTP %d 綁定失敗（可能已有另一個橋接器）\n", g_port);
        SetColor(C_RESET);
        return 0;
    }
    listen(srv, 16);
    SetColor(C_GREEN); printf("[+] HTTP 事件端點 http://127.0.0.1:%d/event\n", g_port); SetColor(C_RESET);

    for (;;) {
        SOCKET cli = accept(srv, nullptr, nullptr);
        if (cli == INVALID_SOCKET) { Sleep(50); continue; }
        HandleClient(cli);
        closesocket(cli);
    }
    return 0;
}
