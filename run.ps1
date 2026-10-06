# Hedge Insight 실행: 서버를 띄우고 브라우저를 엽니다.
Set-Location $PSScriptRoot
Start-Process "http://127.0.0.1:8765"
& .\.venv\Scripts\python.exe -X utf8 -m hedge.server
