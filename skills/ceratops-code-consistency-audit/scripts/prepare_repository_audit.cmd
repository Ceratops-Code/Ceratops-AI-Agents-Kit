@echo off
rem PATH-only bootstrap; Python owns repository inspection and filesystem safety.
setlocal

for %%I in (py.exe) do if not "%%~$PATH:I"=="" (
    call "%%~$PATH:I" -3 -c "import sys;raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "CERATOPS_PREFLIGHT_PYTHON_EXECUTABLE=%%~$PATH:I"
        set "CERATOPS_PREFLIGHT_PYTHON_ARGUMENT=-3"
        call "%%~$PATH:I" -3 "%~dp0prepare_repository_audit.py" %*
        exit /b
    )
)

for %%I in (python3.exe python3.cmd python3.bat) do if not "%%~$PATH:I"=="" (
    call "%%~$PATH:I" -c "import sys;raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "CERATOPS_PREFLIGHT_PYTHON_EXECUTABLE=%%~$PATH:I"
        set "CERATOPS_PREFLIGHT_PYTHON_ARGUMENT="
        call "%%~$PATH:I" "%~dp0prepare_repository_audit.py" %*
        exit /b
    )
)

for %%I in (python.exe python.cmd python.bat) do if not "%%~$PATH:I"=="" (
    call "%%~$PATH:I" -c "import sys;raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "CERATOPS_PREFLIGHT_PYTHON_EXECUTABLE=%%~$PATH:I"
        set "CERATOPS_PREFLIGHT_PYTHON_ARGUMENT="
        call "%%~$PATH:I" "%~dp0prepare_repository_audit.py" %*
        exit /b
    )
)

echo {"error":{"code":"python_not_discoverable","message":"Python 3.11+ is not discoverable on PATH; use the Codex bundled workspace dependency loader"},"missing_runtimes":["python"],"workspace_dependency_loader_required":true}
exit /b 2
