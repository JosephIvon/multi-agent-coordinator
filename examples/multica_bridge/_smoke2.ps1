Set-Location 'D:\WorkSpace\multi-agent-coordinator'
$env:PYTHONPATH = "D:\WorkSpace\multi-agent-coordinator\src"
$env:MAC_DB = "mac.db"
Remove-Item mac.db, server.out, server.err -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath python -ArgumentList "examples\multica_bridge\server.py" -RedirectStandardOutput server.out -RedirectStandardError server.err -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 4

function Post { param($name,$file)
  Write-Host "===== STEP $name ====="
  $body = Get-Content -Raw -Path $file
  try { $r = Invoke-RestMethod -Uri http://127.0.0.1:8765/webhook/multica -Method Post -ContentType "application/json" -Body $body -ErrorAction Stop }
  catch { Write-Host ("error: " + $_.Exception.Message); Write-Host ""; return }
  Write-Host ($r | ConvertTo-Json -Compress)
  Write-Host ""
}

Write-Host "===== STEP 3a: GET /healthz ====="
$r = Invoke-RestMethod -Uri http://127.0.0.1:8765/healthz -Method Get
Write-Host ($r | ConvertTo-Json -Compress)
Write-Host ""

Post "3b issue.created"   "examples/multica_bridge/_events/01_issue_created.json"
Post "3c agent.started"   "examples/multica_bridge/_events/02_agent_started.json"
Post "3d agent.completed" "examples/multica_bridge/_events/03_agent_completed.json"
Post "3e unknown event"   "examples/multica_bridge/_events/04_unknown.json"

Write-Host "===== STEP 4: mac-agent queries ====="
Write-Host "--- mac-agent tasks ---"
python -m mac.cli tasks --db mac.db 2>&1
Write-Host "--- mac-agent status multica-TEST-1 ---"
python -m mac.cli status --db mac.db --task-id multica-TEST-1 2>&1

Write-Host "===== cleanup ====="
Stop-Process -Id $proc.Id -ErrorAction SilentlyContinue
