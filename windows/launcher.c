// AI Meter.exe: starts the bundled Python with no console window.
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE h, HINSTANCE p, PWSTR args, int show) {
    wchar_t dir[MAX_PATH], exe[MAX_PATH], cmd[MAX_PATH * 2];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH);
    if (!n || n >= MAX_PATH) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;
    swprintf(exe, MAX_PATH, L"%ls\\python\\pythonw.exe", dir);
    if (GetFileAttributesW(exe) == INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL, L"The \"python\" folder next to AI Meter.exe is missing.\n\nUnzip the whole AI Meter folder and run AI Meter.exe from inside it.",
                    L"AI Meter could not start", MB_ICONERROR);
        return 1;
    }
    swprintf(cmd, MAX_PATH * 2, L"\"%ls\" -I -m ai_meter.desktop", exe);
    STARTUPINFOW si = { sizeof(si) };
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(exe, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"Windows refused to start the bundled Python. If antivirus software quarantined files in the AI Meter folder, restore them and try again.",
                    L"AI Meter could not start", MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
