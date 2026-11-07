#include "common.h"

const wchar_t* PROCESS_NAME = L"Minecraft.Windows.exe";
const wchar_t* DLL_NAME = L"827dll.dll";
const char* WS_HOST = "127.0.0.1";
const int WS_PORT = 8000;

wchar_t g_dllPath[MAX_PATH] = { 0 };
wchar_t g_loadedDll[MAX_PATH] = { 0 };
static wchar_t g_pipeName[128] = L"\\\\.\\pipe\\mc_chat_827";
static wchar_t g_cmdPipeName[128] = L"\\\\.\\pipe\\mc_cmd_827";

#define C_RESET 7
#define C_GRAY 8
#define C_CYAN 11
#define C_RED 12
#define C_GREEN 10
#define C_YELLOW 14

void SetColor(int c) { SetConsoleTextAttribute(GetStdHandle(STD_OUTPUT_HANDLE), (WORD)c); }
