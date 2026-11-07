#include "common.h"
#include <cwctype>

// 從「實際注入的 DLL 檔名」推導事件埠，和 DLL 端 InitPipeName 用同一套規則，
// 保證兩邊永遠一致：帶 _XXXXXXXX 後綴就用 8770+(hex%200)，否則退回預設 8770。
static int PortFromDllName(const wchar_t* path) {
    std::wstring name(path);
    size_t dot = name.find_last_of(L'.');
    size_t us = name.find_last_of(L'_');
    if (dot == std::wstring::npos || us == std::wstring::npos || us > dot) return 8770;
    std::wstring hex = name.substr(us + 1, dot - us - 1);
    if (hex.size() != 8) return 8770;
    for (wchar_t c : hex) if (!iswxdigit(c)) return 8770;
    return 8770 + (int)(wcstoul(hex.c_str(), nullptr, 16) % 200);
}

int main() {
    setvbuf(stdout, NULL, _IONBF, 0);
    SetConsoleOutputCP(CP_UTF8);
    SetColor(C_CYAN);
    printf("==============================================\n");
    printf("  MC 聊天橋接 v10  (DLL 擷取 → WebSocket → bot.py)\n");
    printf("==============================================\n");
    SetColor(C_RESET);

    // 單一實例：避免同時跑兩個橋接器（會重複注入 DLL、重複送事件）
    HANDLE hOnce = CreateMutexW(nullptr, TRUE, L"Local\\MCWS_Bridge_v10");
    if (hOnce && GetLastError() == ERROR_ALREADY_EXISTS) {
        SetColor(C_YELLOW); printf("[!] 已經有一個橋接器在執行，這個視窗會關閉\n"); SetColor(C_RESET);
        Sleep(2500);
        return 0;
    }

    WSADATA wsa{};
    WSAStartup(MAKEWORD(2, 2), &wsa);
    srand(GetTickCount());

    GetModuleFileNameW(NULL, g_dllPath, MAX_PATH);
    std::wstring::size_type pos = std::wstring(g_dllPath).find_last_of(L"\\/");
    if (pos != std::wstring::npos) wcscpy_s(g_dllPath + pos + 1, MAX_PATH - pos - 1, DLL_NAME);

    wchar_t dir[MAX_PATH];
    wcscpy_s(dir, MAX_PATH, g_dllPath);
    wchar_t* slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;

    DWORD tag = GetTickCount();
    swprintf_s(g_loadedDll, MAX_PATH, L"%s\\827dll_%08X.dll", dir, tag);

    if (!CopyFileW(g_dllPath, g_loadedDll, FALSE)) {
        // 複製失敗（DLL 被鎖住 / 權限問題）時退回原始檔名注入。
        // 注意：這時 DLL 檔名沒有 _XXXXXXXX 後綴，它會用預設 port 8770，
        // 所以橋接器也必須跟著用 8770（下面統一由檔名推導），
        // 否則兩邊各用不同 port，事件會全部丟掉而且完全沒有錯誤訊息。
        SetColor(C_YELLOW);
        printf("[!] 無法複製 DLL（可能正被鎖住），改用原始檔名注入，事件埠固定 8770\n");
        SetColor(C_RESET);
        wcscpy_s(g_loadedDll, MAX_PATH, g_dllPath);
    } else {
        wchar_t pattern[MAX_PATH];
        swprintf_s(pattern, MAX_PATH, L"%s\\827dll_*.dll", dir);
        WIN32_FIND_DATAW fd{};
        HANDLE hFind = FindFirstFileW(pattern, &fd);
        if (hFind != INVALID_HANDLE_VALUE) {
            const wchar_t* base = wcsrchr(g_loadedDll, L'\\');
            base = base ? base + 1 : g_loadedDll;
            do {
                // 只比檔名，不要比整條路徑：路徑寫法（斜線/大小寫）稍有不同就會把
                // 剛複製好的檔案誤刪，接著注入失敗 → 退回原始檔名 → port 對不上。
                if (_wcsicmp(fd.cFileName, base) != 0) {
                    wchar_t full[MAX_PATH];
                    _snwprintf_s(full, MAX_PATH, _TRUNCATE, L"%s\\%s", dir, fd.cFileName);
                    DeleteFileW(full);
                }
            } while (FindNextFileW(hFind, &fd));
            FindClose(hFind);
        }
    }

    // 一律依「實際要注入的檔名」決定監聽埠（不要用 tag 自己算）
    g_port = PortFromDllName(g_loadedDll);

    EnableDebugPrivilege();
    if (GrantUwpAccess(g_loadedDll)) { SetColor(C_GREEN); printf("[+] UWP 檔案權限已授予\n"); SetColor(C_RESET); }

    CreateThread(nullptr, 0, HttpServerThread, nullptr, 0, nullptr);
    CreateThread(nullptr, 0, WsRecvThread, nullptr, 0, nullptr);
    
    Sleep(200);

    // 遊戲還沒開就等它，開好後自動注入（不用重跑 start.bat）
    DWORD pid = 0;
    bool injected = false;
    while (!injected) {
        pid = FindProcessId(PROCESS_NAME);
        if (pid != 0) {
            injected = InjectDllFile(pid, g_loadedDll);
        }

        if (!injected) {
            SetColor(C_YELLOW);
            if (pid == 0) {
                printf("[*] 等待 Minecraft.Windows.exe ...（開好遊戲、進世界後會自動注入）\n");
            } else {
                printf("[!] 注入失敗（pid=%lu），3 秒後重試；請確認 %ls 在同一個資料夾\n", pid, DLL_NAME);
            }
            SetColor(C_RESET);
            Sleep(3000);
        }
    }

    SetColor(C_GREEN); printf("[+] 已注入 %ls (pid=%lu)，等待事件...\n\n", g_loadedDll, pid); SetColor(C_RESET);
    SendEvent("BridgeStatus", "{\"message\":\"bridge online\"}");
    // 一定要在第一時間送 BridgeHello 讓 bot 辨識「這條是橋接器」。
    // 舊版只有 EnsureConnected() 在「重連」時才送；若是 bot 先開、橋接器後開，
    // 初次連線不會送 BridgeHello → bot 分不清誰是橋接器，會把 tellraw/訂閱誤發到橋接器。
    SendEvent("BridgeHello", "{}");

    int tick = 0;
    while (true) {
        Sleep(1000);
        if (++tick % 5 == 0) EnsureConnected();
    }
    return 0;
}













