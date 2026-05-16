@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
set "COUNT=0"

pushd "%ROOT%" >nul 2>&1
if errorlevel 1 (
    echo Failed to enter "%ROOT%".
    exit /b 1
)

echo Deleting all *.pyt.xml files under "%ROOT%" ...

for /r "%ROOT%" %%F in (*.pyt.xml) do (
    del /f /q "%%~fF" >nul 2>&1
    if not exist "%%~fF" set /a COUNT+=1
)

popd >nul 2>&1

echo Deleted %COUNT% file(s).
pause
