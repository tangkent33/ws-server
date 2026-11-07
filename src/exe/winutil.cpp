#include "common.h"

bool GrantUwpAccess(const std::wstring& filePath) {
    PACL pOldDACL = nullptr, pNewDACL = nullptr;
    PSECURITY_DESCRIPTOR pSD = nullptr;
    PSID pSID = nullptr;
    if (!ConvertStringSidToSidW(L"S-1-15-2-1", &pSID)) return false;
    DWORD dwRes = GetNamedSecurityInfoW(filePath.c_str(), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, nullptr, nullptr, &pOldDACL, nullptr, &pSD);
    if (dwRes != ERROR_SUCCESS) { LocalFree(pSID); return false; }
    EXPLICIT_ACCESSW ea = {};
    ea.grfAccessPermissions = GENERIC_READ | GENERIC_EXECUTE;
    ea.grfAccessMode = SET_ACCESS;
    ea.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT;
    ea.Trustee.TrusteeForm = TRUSTEE_IS_SID;
    ea.Trustee.TrusteeType = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea.Trustee.ptstrName = (LPWSTR)pSID;
    dwRes = SetEntriesInAclW(1, &ea, pOldDACL, &pNewDACL);
    if (dwRes != ERROR_SUCCESS) { LocalFree(pSD); LocalFree(pSID); return false; }
    dwRes = SetNamedSecurityInfoW((LPWSTR)filePath.c_str(), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, nullptr, nullptr, pNewDACL, nullptr);
    LocalFree(pSD); LocalFree(pSID); LocalFree(pNewDACL);
    return dwRes == ERROR_SUCCESS;
}

bool EnableDebugPrivilege() {
    HANDLE hToken;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &hToken)) return false;
    TOKEN_PRIVILEGES tp{};
    tp.PrivilegeCount = 1;
    LookupPrivilegeValueW(nullptr, L"SeDebugPrivilege", &tp.Privileges[0].Luid);
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;
    BOOL ok = AdjustTokenPrivileges(hToken, FALSE, &tp, sizeof(tp), nullptr, nullptr);
    CloseHandle(hToken);
    return ok;
}

DWORD FindProcessId(const wchar_t* name) {
    DWORD pid = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W pe{}; pe.dwSize = sizeof(pe);
    if (Process32FirstW(snap, &pe)) {
        do { if (_wcsicmp(pe.szExeFile, name) == 0) { pid = pe.th32ProcessID; break; } } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return pid;
}

bool InjectDllFile(DWORD pid, const wchar_t* dllPath) {
    HANDLE hProc = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ, FALSE, pid);
    if (!hProc) return false;
    SIZE_T pathLen = (wcslen(dllPath) + 1) * sizeof(wchar_t);
    void* remoteBuf = VirtualAllocEx(hProc, nullptr, pathLen, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remoteBuf) { CloseHandle(hProc); return false; }
    WriteProcessMemory(hProc, remoteBuf, dllPath, pathLen, nullptr);
    HMODULE hKernel = GetModuleHandleA("kernel32.dll");
    auto pLoadLibrary = (LPTHREAD_START_ROUTINE)GetProcAddress(hKernel, "LoadLibraryW");
    HANDLE hThread = CreateRemoteThread(hProc, nullptr, 0, pLoadLibrary, remoteBuf, 0, nullptr);
    if (!hThread) { VirtualFreeEx(hProc, remoteBuf, 0, MEM_RELEASE); CloseHandle(hProc); return false; }
    WaitForSingleObject(hThread, 4000);
    DWORD exitCode = 0; GetExitCodeThread(hThread, &exitCode);
    VirtualFreeEx(hProc, remoteBuf, 0, MEM_RELEASE);
    CloseHandle(hThread); CloseHandle(hProc);
    return exitCode != 0;
}

