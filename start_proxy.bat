@echo off
title Claudian Bridge Proxy (15722 -^> StepFun /v1)
echo ==================================
echo   Claudian Bridge Proxy
echo   Port: 15722 -^> StepFun /v1
echo   (hardcoded in proxy.py line 550)
echo ==================================
echo.
echo Starting proxy...
python "D:\claudian-bridge\proxy.py" --port 15722 --model "step-3.7-flash"
pause
