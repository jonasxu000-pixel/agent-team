@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m agent_team %*
exit /b %errorlevel%
