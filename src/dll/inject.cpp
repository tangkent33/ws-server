#include "common.h"

static bool GameIsForeground() {
    HWND fg = GetForegroundWindow();
    if (!fg) return false;
    DWORD pid = 0;
    GetWindowThreadProcessId(fg, &pid);
    return pid == GetCurrentProcessId();
}

static void SendKey(WORD vk) {
    INPUT in{}; in.type = INPUT_KEYBOARD; in.ki.wVk = vk;
    SendInput(1, &in, sizeof(in));
    in.ki.dwFlags = KEYEVENTF_KEYUP;
    SendInput(1, &in, sizeof(in));
}

static void TypeUnicode(const std::wstring& text) {
    for (wchar_t ch : text) {
        INPUT in{}; in.type = INPUT_KEYBOARD;
        in.ki.wScan = ch; in.ki.dwFlags = KEYEVENTF_UNICODE;
        SendInput(1, &in, sizeof(in));
        in.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
        SendInput(1, &in, sizeof(in));
        Sleep(4);
    }
}

static bool RunInjectedCommand(const std::string& cmd) {
    if (cmd.rfind("ban ", 0) != 0 && cmd.rfind("kick ", 0) != 0) return false;
    if (!GameIsForeground()) {
        SendLine("CMDRESULT|fail|game not focused");
        return false;
    }
    int n = MultiByteToWideChar(CP_UTF8, 0, cmd.c_str(), -1, nullptr, 0);
    std::wstring w(n > 0 ? n - 1 : 0, 0);
    if (n > 0) MultiByteToWideChar(CP_UTF8, 0, cmd.c_str(), -1, &w[0], n);

    SendKey('T');            // 開啟聊天
    Sleep(250);
    TypeUnicode(L"/" + w);
    Sleep(120);
    SendKey(VK_RETURN);
    Sleep(120);
    SendKey(VK_ESCAPE);      // 確保聊天關閉
    SendLine("CMDRESULT|ok|injected");
    return true;
}

DWORD WINAPI CmdThread(LPVOID) {
    while (true) {
        std::string cmd;
        if (HttpGetCommand(cmd)) {
            while (!cmd.empty() && (cmd.back() == '\n' || cmd.back() == '\r')) cmd.pop_back();
            if (!cmd.empty()) RunInjectedCommand(cmd);
        } else {
            Sleep(1000);      // 沒有指令就每秒問一次
        }
    }
    return 0;
}