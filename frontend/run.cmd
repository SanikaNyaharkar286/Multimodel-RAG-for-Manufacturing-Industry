@echo off
setlocal
pushd "%~dp0"
call npm.cmd run dev
set "frontendExit=%errorlevel%"
popd
exit /b %frontendExit%
