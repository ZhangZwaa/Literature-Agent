@echo off
chcp 65001 > nul
cd /d D:\Literature-Agent
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
C:\Python313\python.exe literature_agent.py >> agent.log 2>&1