$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dist = Join-Path $root "dist"
if (!(Test-Path $dist)) {
  New-Item -ItemType Directory -Path $dist | Out-Null
}
$out = Join-Path $dist "newsroom-crawler-$stamp.zip"
$items = @(
  "app",
  "config",
  "docs",
  "scripts",
  "README.md",
  "requirements.txt",
  "install.bat",
  "run_server.bat",
  "run_crawler.bat"
)
$paths = $items | ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path $_ }
Compress-Archive -Path $paths -DestinationPath $out -Force
Write-Host "Created $out"
