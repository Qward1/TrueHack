@echo off
echo Checking LM Studio...
curl -s http://localhost:1234/v1/models >nul 2>&1
if errorlevel 1 (
    echo ERROR: LM Studio not running! Start LM Studio and load a model.
    pause
    exit /b 1
)
echo LM Studio OK

echo Starting LocalScript server...
call .venv\Scripts\activate
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000
