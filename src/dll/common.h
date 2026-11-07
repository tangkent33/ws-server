// common.h — 827dll 各模組共用的宣告
#pragma once
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS

#include <windows.h>
#include <cstdio>
#include <cstdarg>
#include <cstdlib>
#include <cctype>
#include <cwchar>
#include <string>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <mutex>
#include <algorithm>

#pragma execution_character_set("utf-8")
#pragma comment(lib, "user32.lib")

// 版本戳：DLL 啟動時會把自己的版本報給橋接器（SYS|dll_ready|<BUILD_ID>）。
// 遊戲開久了記憶體裡會同時存在好幾份舊版 DLL（每次注入的檔名都不同 → 不會被覆蓋），
// 舊版會繼續送舊格式事件（例如把 TP 橫幅印成「TP Multiplayer (tpmultiplayer1mj)」）。
// 有這個戳記，橋接器就能直接告訴你「現在講話的是舊版，請重開遊戲」。
#define BUILD_ID "10.1"

// ---------------------------------------------------------------- 共用型別
struct Found {
    std::string type;
    std::string a;
    std::string b;
    size_t kwPos = 0;
};

struct Region {
    unsigned long long base;
    size_t size;
    DWORD lastHit;
};

// ---------------------------------------------------------------- 管線
extern int g_port;
extern HMODULE g_hSelf;
void InitPipeName(HMODULE hModule);
void SendLine(const std::string& line);
bool HttpGetCommand(std::string& out);

// ---------------------------------------------------------------- 分析層
void HandleWindow(const char* win, size_t len, const char* tag, bool worldCtx);

// ---------------------------------------------------------------- 文字 / 分類（analysis.cpp）
std::string Trim(const std::string& s);
std::string Sanitize(const char* p, size_t n);
bool ValidUtf8(const std::string& s);
bool ValidUtf8(const char* p, size_t n);
bool ChatMessageOk(const std::string& msg);
bool TextLike(const std::string& s);
bool ChatNameOk(const std::string& n);
bool Duplicate(const std::string& key);
void DebugChat(const std::string& t);
void HandleChatWindow(const char* win, size_t len, bool atStringStart);

extern volatile LONG g_connectFail;
// ---------------------------------------------------------------- 掃描層
DWORD WINAPI ScanThread(LPVOID);

// ---------------------------------------------------------------- 指令注入
DWORD WINAPI CmdThread(LPVOID);
