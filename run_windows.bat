@echo off
cd /d "%~dp0"
echo Installing/confirming dependencies...
python -m pip install -r requirements.txt
echo Starting AMANA Modash Lite...
python -m streamlit run app.py
pause
