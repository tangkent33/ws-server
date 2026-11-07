#include "common.h"

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);
        g_hSelf = hModule;
        InitPipeName(hModule);
        CreateThread(nullptr, 0, ScanThread, nullptr, 0, nullptr);
        CreateThread(nullptr, 0, CmdThread, nullptr, 0, nullptr);   // 遊戲 WebSocket 沒連線時的最後手段
    }
    return TRUE;
}








































