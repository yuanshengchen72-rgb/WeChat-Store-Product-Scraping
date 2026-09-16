$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment was not found."
}

& $python -c "from wechat_shop_scraper.selftest import run_self_test; raise SystemExit(run_self_test())"
if ($LASTEXITCODE -ne 0) {
    throw "Portable dependency self-test failed. Run INSTALL.bat before building."
}

$stageDist = Join-Path $PSScriptRoot "build\portable-dist"
$stageWork = Join-Path $PSScriptRoot "build\portable-work"

& $python -m PyInstaller --noconfirm --clean --distpath $stageDist --workpath $stageWork portable.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE."
}

$releaseDirs = @(Get-ChildItem -LiteralPath $stageDist -Directory)
if ($releaseDirs.Count -ne 1) {
    throw "Expected exactly one portable release directory."
}
$releaseDir = $releaseDirs[0].FullName
$guide = Join-Path $PSScriptRoot "PORTABLE_README.txt"
Copy-Item -LiteralPath $guide -Destination $releaseDir -Force
$motorolaGuide = Join-Path $PSScriptRoot "摩托罗拉手机采集配置-Codex协作详细说明.md"
if (Test-Path -LiteralPath $motorolaGuide) {
    Copy-Item -LiteralPath $motorolaGuide -Destination $releaseDir -Force
}

$zipPath = Join-Path $PSScriptRoot "dist\WeChatShopScraper-portable-win64.zip"
Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -Force

$zip = Get-Item -LiteralPath $zipPath
Write-Host "Portable build created: $($zip.FullName)"
Write-Host "Size: $([Math]::Round($zip.Length / 1MB, 2)) MB"
