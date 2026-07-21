param(
    [Parameter(Mandatory = $true)]
    [string]$Config,
    [string]$Python = 'py'
)

$ErrorActionPreference = 'Stop'
$resolved = (Resolve-Path -LiteralPath $Config).Path
if (-not $env:WECHAT_RECEIVER_TOKEN) {
    throw 'Set WECHAT_RECEIVER_TOKEN in the process or user environment first.'
}
& $Python -3 (Join-Path $PSScriptRoot '..\..\windows_agent.py') --config $resolved
exit $LASTEXITCODE

