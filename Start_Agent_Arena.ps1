[CmdletBinding()]
param([string]$PublicOrigin)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$arenaRoot = $PSScriptRoot
if (-not $PublicOrigin) { $PublicOrigin = $env:ARENA_PUBLIC_ORIGIN }
$arenaOriginFile = Join-Path $arenaRoot 'data\public-origin.txt'
if (-not $PublicOrigin -and (Test-Path -LiteralPath $arenaOriginFile)) {
    $PublicOrigin = [System.IO.File]::ReadAllText($arenaOriginFile).Trim()
}
$arenaCommand = Get-Command py -ErrorAction SilentlyContinue
$arenaArguments = @('-3')
if (-not $arenaCommand) {
    $arenaCommand = Get-Command python -ErrorAction Stop
    $arenaArguments = @()
}
$arenaArguments += @((Join-Path $arenaRoot 'server.py'), '--data-dir', (Join-Path $arenaRoot 'data'))
if ($PublicOrigin) { $arenaArguments += @('--public-origin', $PublicOrigin) }
Push-Location $arenaRoot
try { & $arenaCommand.Source @arenaArguments }
finally { Pop-Location }
