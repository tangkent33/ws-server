// http.cpp — 事件輸出與指令接收（HTTP 取代原本的 named pipe）
#include "common.h"

#include <winhttp.h>
#pragma comment(lib, "winhttp.lib")

// 埠號由 DLL 檔名決定：827dll_<hex>.dll → 8770 + (hex % 200)
// 這樣同時存在多個版本時互不干擾。
int g_port = 8770;
volatile LONG g_connectFail = 0;
HMODULE g_hSelf = nullptr;

static std::mutex g_httpMutex;

// ---------------------------------------------------------------- 基本請求
static bool HttpRequest(const wchar_t* method, const wchar_t* path,
                        const std::string& body, std::string* response) {
    HINTERNET session = WinHttpOpen(L"MC Bridge DLL/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY,
                                    WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session) return false;

    bool ok = false;
    HINTERNET conn = WinHttpConnect(session, L"127.0.0.1", (INTERNET_PORT)g_port, 0);
    if (conn) {
        HINTERNET req = WinHttpOpenRequest(conn, method, path, NULL, WINHTTP_NO_REFERER,
                                           WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
        if (req) {
            const wchar_t* headers = L"Content-Type: text/plain; charset=utf-8\r\n";
            BOOL sent = WinHttpSendRequest(req, headers, -1L,
                                           body.empty() ? WINHTTP_NO_REQUEST_DATA : (LPVOID)body.data(),
                                           (DWORD)body.size(), (DWORD)body.size(), 0);
            if (sent && WinHttpReceiveResponse(req, NULL)) {
                ok = true;
                if (response) {
                    for (;;) {
                        DWORD avail = 0;
                        if (!WinHttpQueryDataAvailable(req, &avail) || avail == 0) break;
                        std::string chunk(avail, 0);
                        DWORD read = 0;
                        if (!WinHttpReadData(req, &chunk[0], avail, &read) || read == 0) break;
                        chunk.resize(read);
                        *response += chunk;
                        if (response->size() > 8192) break;
                    }
                }
            }
            WinHttpCloseHandle(req);
        }
        WinHttpCloseHandle(conn);
    }
    WinHttpCloseHandle(session);
    return ok;
}

// ---------------------------------------------------------------- 初始化
void InitPipeName(HMODULE hModule) {
    wchar_t path[MAX_PATH] = { 0 };
    if (!GetModuleFileNameW(hModule, path, MAX_PATH)) return;

    std::wstring name(path);
    size_t dot = name.find_last_of(L'.');
    size_t us = name.find_last_of(L'_');
    if (dot == std::wstring::npos || us == std::wstring::npos || us > dot) return;

    std::wstring hex = name.substr(us + 1, dot - us - 1);
    if (hex.size() != 8) return;
    for (wchar_t c : hex) if (!iswxdigit(c)) return;

    unsigned idx = (unsigned)wcstoul(hex.c_str(), nullptr, 16);
    g_port = 8770 + (int)(idx % 200);
}

// ---------------------------------------------------------------- 事件輸出
void SendLine(const std::string& line) {
    std::lock_guard<std::mutex> lock(g_httpMutex);
    if (HttpRequest(L"POST", L"/event", line + "\n", nullptr)) {
        InterlockedExchange(&g_connectFail, 0);
    } else {
        InterlockedIncrement(&g_connectFail);
    }
}

// ---------------------------------------------------------------- 指令接收（輪詢）
bool HttpGetCommand(std::string& out) {
    std::lock_guard<std::mutex> lock(g_httpMutex);
    out.clear();
    return HttpRequest(L"GET", L"/command", "", &out) && !out.empty();
}
