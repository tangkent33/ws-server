// common.h — MC-WS 各模組共用宣告
#pragma once
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#define _WINSOCK_DEPRECATED_NO_WARNINGS

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <tlhelp32.h>
#include <aclapi.h>
#include <sddl.h>
#include <string>
#include <vector>
#include <mutex>
#include <cstdio>

#pragma execution_character_set("utf-8")
#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "advapi32.lib")

// 必須和 src/dll/common.h 的 BUILD_ID 一致；DLL 報上來的版本不同就代表
// 遊戲裡跑的是舊版 DLL（舊 DLL 會繼續吐舊格式事件），要重開遊戲。
#define BUILD_ID "10.3"

// ---------------------------------------------------------------- 常數
extern const wchar_t* PROCESS_NAME;
extern const wchar_t* DLL_NAME;
extern const char* WS_HOST;
extern const int WS_PORT;

extern wchar_t g_dllPath[MAX_PATH];
extern wchar_t g_loadedDll[MAX_PATH];
extern int g_port;


extern SOCKET g_sock;

// ---------------------------------------------------------------- 主控台
#define C_RESET 7
#define C_GRAY 8
#define C_CYAN 11
#define C_RED 12
#define C_GREEN 10
#define C_YELLOW 14

void SetColor(int c);

// ---------------------------------------------------------------- WebSocket
void SendEvent(const std::string& eventName, const std::string& bodyJson);
void EnsureConnected();
DWORD WINAPI WsRecvThread(LPVOID);
std::string EscapeJson(const std::string& s);

// ---------------------------------------------------------------- 事件
void HandleEventLine(const std::string& line);

// ---------------------------------------------------------------- 管線
DWORD WINAPI HttpServerThread(LPVOID);

bool SendCommandToDll(const std::string& cmd);
void CmdLog(const std::string& s);

// ---------------------------------------------------------------- Windows 工具
bool GrantUwpAccess(const std::wstring& filePath);
bool EnableDebugPrivilege();
DWORD FindProcessId(const wchar_t* name);
bool InjectDllFile(DWORD pid, const wchar_t* dllPath);
