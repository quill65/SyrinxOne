@echo off
setlocal
set ELECTRON_RUN_AS_NODE=
set "PATH=%USERPROFILE%\.local\node;%PATH%"
if "%SYRINX_DEFAULT_DIR%"=="" set "SYRINX_DEFAULT_DIR=C:\Users\john\Documents\projects\syrinxone_ref\cleansongs"
cd /d "%~dp0apps\desktop-electron"
start "" "%~dp0node_modules\electron\dist\electron.exe" .
endlocal
