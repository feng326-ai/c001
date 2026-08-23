param(
    [string]$StateDir = 'C:\ProgramData\WxSearchControl'
)

$ErrorActionPreference = 'Stop'
$configPath = Join-Path $StateDir 'agent.json'
$statePath = Join-Path $StateDir 'state.json'
$auditPath = Join-Path $StateDir 'audit.jsonl'

function Write-Audit([hashtable]$Event) {
    $Event.ts = (Get-Date).ToUniversalTime().ToString('o')
    ($Event | ConvertTo-Json -Compress -Depth 8) | Add-Content -LiteralPath $auditPath -Encoding utf8
}

function Read-Json($Path, $Fallback) {
    if (-not (Test-Path -LiteralPath $Path)) { return $Fallback }
    return Get-Content -LiteralPath $Path -Raw -Encoding utf8 | ConvertFrom-Json
}

function Save-Json($Path, $Value) {
    $tmp = "$Path.tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tmp -Encoding utf8
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
$cfg = Read-Json $configPath $null
if ($null -eq $cfg -or -not $cfg.device_id -or -not $cfg.hmac_key_b64 -or -not $cfg.control_url) {
    throw '缺少受控发布 agent.json（device_id/hmac_key_b64/control_url）'
}
$state = Read-Json $statePath ([pscustomobject]@{sequence=0; current_release=''; previous_release=''; stop_requested=$false})
$key = [Convert]::FromBase64String([string]$cfg.hmac_key_b64)

function Test-Envelope($Envelope) {
    $payload = [Convert]::FromBase64String([string]$Envelope.payload_b64)
    $actual = [Convert]::FromBase64String([string]$Envelope.signature_b64)
    $hmac = [System.Security.Cryptography.HMACSHA256]::new($key)
    $expected = $hmac.ComputeHash($payload)
    if (-not [System.Security.Cryptography.CryptographicOperations]::FixedTimeEquals($actual, $expected)) { throw '签名无效' }
    $command = [Text.Encoding]::UTF8.GetString($payload) | ConvertFrom-Json
    if ($command.device_id -ne $cfg.device_id) { throw '节点身份不匹配' }
    if ([int64]$command.sequence -le [int64]$state.sequence) { throw '拒绝重放命令' }
    $now = [DateTime]::UtcNow
    if ($now -lt [DateTime]::Parse($command.issued_at).ToUniversalTime() -or $now -gt [DateTime]::Parse($command.expires_at).ToUniversalTime()) { throw '命令已过期或尚未生效' }
    if (@('status','stop_after_current','stage_release','activate_release','rollback') -notcontains [string]$command.action) { throw '动作不在白名单' }
    return $command
}

function Invoke-ControlledAction($Command) {
    $action = [string]$Command.action
    if ($action -eq 'status') { return @{status='ok'; release=$state.current_release; stop_requested=[bool]$state.stop_requested} }
    if ($action -eq 'stop_after_current') {
        $state.stop_requested = $true
        New-Item -ItemType File -Path (Join-Path $StateDir 'stop.requested') -Force | Out-Null
        return @{status='accepted'}
    }
    if ($action -eq 'stage_release') {
        $sha = [string]$Command.args.sha256
        $url = [string]$Command.args.url
        if ($sha -notmatch '^[a-f0-9]{64}$' -or $url -notmatch '^https?://') { throw '发布参数非法' }
        $zip = Join-Path $StateDir ("$sha.zip")
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sha) { Remove-Item -LiteralPath $zip -Force; throw '发布包摘要不匹配' }
        $target = Join-Path (Join-Path $StateDir 'releases') $sha
        if (Test-Path $target) { Remove-Item -LiteralPath $target -Recurse -Force }
        Expand-Archive -LiteralPath $zip -DestinationPath $target -Force
        return @{status='staged'; sha256=$sha}
    }
    if ($action -eq 'activate_release') { throw 'activate_release 需由节点运行器在关键词边界执行' }
    if ($action -eq 'rollback') { throw 'rollback 需由节点运行器在关键词边界执行' }
}

while ($true) {
    try {
        $raw = Invoke-WebRequest -Uri ("$($cfg.control_url.TrimEnd('/'))/control-$($cfg.device_id).json") -UseBasicParsing -TimeoutSec 10
        $command = Test-Envelope (($raw.Content | ConvertFrom-Json))
        $result = Invoke-ControlledAction $command
        $state.sequence = [int64]$command.sequence
        Save-Json $statePath $state
        Write-Audit @{device_id=$cfg.device_id; sequence=$state.sequence; action=$command.action; result=$result}
    } catch { Write-Audit @{device_id=$cfg.device_id; event='rejected_or_failed'; error=$_.Exception.Message} }
    Start-Sleep -Seconds 5
}
