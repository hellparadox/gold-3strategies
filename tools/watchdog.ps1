<#
    watchdog.ps1 — نگهبان ربات‌ها (فقط ری‌استارت؛ به منطق معاملات دست نمی‌زند)
 
    اگر یک ربات بیش از حد مجاز هیچ چیزی در لاگ ننویسد (یعنی داخل یک فراخوانی
    MT5 گیر کرده باشد)، همان Task ویندوزی‌اش را End و دوباره Run می‌کند —
    دقیقاً همان مسیری که خودتان با آن ربات را بالا می‌آورید.
 
    نصب (یک بار، در PowerShell با دسترسی Administrator):
      schtasks /Create /TN GoldWatchdog /SC MINUTE /MO 5 /RL HIGHEST /RU SYSTEM ^
        /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Administrator\gold-3strategies\tools\watchdog.ps1"
 
    تست دستی:
      powershell -NoProfile -ExecutionPolicy Bypass -File tools\watchdog.ps1 -WhatIfOnly
#>
param(
    [string]$Root = "C:\Users\Administrator\gold-3strategies",
    [int]$StaleMinutes = 75,          # سکوت بیشتر از این = هنگ (فیلتر خبر هر ۶۰ دقیقه می‌نویسد)
    [int]$MinRestartGapMinutes = 30,  # جلوگیری از حلقهٔ ری‌استارت
    [switch]$WhatIfOnly               # فقط گزارش بده، کاری نکن
)
 
$ErrorActionPreference = "Stop"
# Match: چون schtasks /End فقط cmd.exe را می‌بندد و پروسهٔ پایتونِ فرزند زنده می‌ماند،
# پروسهٔ هر ربات را از روی خط فرمانش پیدا و جداگانه می‌بندیم.
$bots = @(
    @{ Task = "GoldBot";  LogGlob = "bot_*.log";      Beat = "data\heartbeat_orb_gold.txt";
       Name = "ORB";      Match = { param($c) $c -match 'main_live' -and $c -notmatch 'settings_ichimoku' } },
    @{ Task = "GoldBot2"; LogGlob = "ichimoku_*.log"; Beat = "data\heartbeat_ichimoku_m15.txt";
       Name = "ICHIMOKU"; Match = { param($c) $c -match 'settings_ichimoku' } }
)
$stateFile = Join-Path $Root "data\watchdog_state.json"
$logFile   = Join-Path $Root "logs\watchdog.log"
 
function Write-Log([string]$msg) {
    $line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Output $line
    try { Add-Content -Path $logFile -Value $line -Encoding UTF8 } catch { }
}
 
function Get-LastActivity($bot) {
    # اولویت با فایل ضربان (اگر روزی به کد اضافه شد)، وگرنه زمان آخرین نوشتن لاگ
    $beat = Join-Path $Root $bot.Beat
    if (Test-Path $beat) { return (Get-Item $beat).LastWriteTime }
    $log = Get-ChildItem (Join-Path $Root "logs\$($bot.LogGlob)") -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $log) { return $null }
    return $log.LastWriteTime
}
 
function Send-Telegram([string]$text) {
    try {
        $envFile = Join-Path $Root ".env"
        if (-not (Test-Path $envFile)) { return }
        $token = $null; $chat = "84078462"
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*TELEGRAM_TOKEN_ICHIMOKU\s*=\s*(.+?)\s*$') { $token = $Matches[1] }
            elseif (-not $token -and $line -match '^\s*TELEGRAM_TOKEN\s*=\s*(.+?)\s*$') { $token = $Matches[1] }
            elseif ($line -match '^\s*ADMIN_IDS\s*=\s*([0-9]+)') { $chat = $Matches[1] }
        }
        if (-not $token) { return }
        $body = @{ chat_id = $chat; text = $text } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" `
            -ContentType "application/json; charset=utf-8" `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 15 | Out-Null
    } catch { Write-Log "telegram notify failed: $($_.Exception.Message)" }
}
 
# --- وضعیت قبلی (برای فاصلهٔ حداقلی بین ری‌استارت‌ها) ---
$state = @{}
if (Test-Path $stateFile) {
    try { (Get-Content $stateFile -Raw | ConvertFrom-Json).PSObject.Properties |
            ForEach-Object { $state[$_.Name] = $_.Value } } catch { }
}
 
foreach ($bot in $bots) {
    $last = Get-LastActivity $bot
    if ($null -eq $last) { Write-Log "$($bot.Name): no log/heartbeat found — skipped"; continue }
 
    $idle = [int]((Get-Date) - $last).TotalMinutes
    if ($idle -lt $StaleMinutes) { Write-Log "$($bot.Name): OK (idle ${idle}m)"; continue }
 
    $lastRestart = $null
    if ($state.ContainsKey($bot.Task)) { $lastRestart = [datetime]$state[$bot.Task] }
    if ($lastRestart -and ((Get-Date) - $lastRestart).TotalMinutes -lt $MinRestartGapMinutes) {
        Write-Log "$($bot.Name): stale (${idle}m) but restarted $([int]((Get-Date)-$lastRestart).TotalMinutes)m ago — waiting"
        continue
    }
 
    Write-Log "$($bot.Name): STALE ${idle}m -> restarting task $($bot.Task)"
    if ($WhatIfOnly) { Write-Log "$($bot.Name): WhatIfOnly, nothing done"; continue }
 
    & schtasks.exe /End /TN $bot.Task  2>&1 | ForEach-Object { Write-Log "  end : $_" }
    Start-Sleep -Seconds 3
 
    # پروسهٔ پایتونِ باقی‌مانده را هم ببند (schtasks فقط cmd.exe را می‌بندد)
    $leftover = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and (& $bot.Match $_.CommandLine) }
    foreach ($pr in $leftover) {
        Write-Log "  kill: pid $($pr.ProcessId) -> $($pr.CommandLine)"
        try { Stop-Process -Id $pr.ProcessId -Force -ErrorAction Stop } catch { Write-Log "  kill failed: $($_.Exception.Message)" }
    }
    Start-Sleep -Seconds 3
 
    # اگر هنوز چیزی زنده است، ری‌استارت نکن تا دو نسخه هم‌زمان اجرا نشود
    $still = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and (& $bot.Match $_.CommandLine) }
    if ($still) {
        Write-Log "$($bot.Name): ABORT — process still alive after kill, not restarting"
        Send-Telegram "⚠️ watchdog: ربات $($bot.Name) هنگ کرده و بسته هم نشد. دستی رسیدگی کنید."
        continue
    }
 
    & schtasks.exe /Run /TN $bot.Task  2>&1 | ForEach-Object { Write-Log "  run : $_" }
    Start-Sleep -Seconds 10
    $now = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -and (& $bot.Match $_.CommandLine) }
    Write-Log "$($bot.Name): after restart -> $(@($now).Count) process(es)"
 
    $state[$bot.Task] = (Get-Date).ToString("o")
    Send-Telegram "🔁 watchdog: ربات $($bot.Name) به مدت ${idle} دقیقه بی‌پاسخ بود و ری‌استارت شد."
}
 
try { ($state | ConvertTo-Json) | Set-Content -Path $stateFile -Encoding UTF8 } catch { }
