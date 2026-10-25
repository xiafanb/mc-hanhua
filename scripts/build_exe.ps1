param(
  [string]$PythonExe = "python",
  [ValidateSet("onefile", "onedir")]
  [string]$Mode = "onefile",
  [string]$Name = "mc汉化器"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $PythonExe -m pip show pyinstaller PySide6 *> $null
$pipShowExit = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($pipShowExit -ne 0) {
  Write-Host "Installing packaging dependencies..."
  & $PythonExe -m pip install -e ".[gui,packaging]"
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to install packaging dependencies."
  }
}

$entry = "scripts\gui_entry.py"
$assetDir = Join-Path $Root "assets"
$webui = Join-Path $Root "src\mc_hanhua\webui"
$icon = Join-Path $assetDir "app_icon.ico"
$tubiao = Join-Path $assetDir "tubiao.png"
$font = Join-Path $assetDir "NotoSansSC-VF.ttf"
$terms = Join-Path $assetDir "official-terms"
$allowlist = Join-Path $assetDir "mechanism-allowlist.json"
$displayAllowlist = Join-Path $assetDir "display-argument-allowlist.json"
$fragmentAllowlist = Join-Path $assetDir "fragment-merge-allowlist.json"
$glyphWidths = Join-Path $assetDir "glyph-widths.json"

$bundle = if ($Mode -eq "onedir") { "--onedir" } else { "--onefile" }
$pyiArgs = @(
  "--noconfirm",
  "--clean",
  $bundle,
  "--windowed",
  "--paths", "src",
  "--specpath", "build",
  "--name", $name,
  "--icon", $icon,
  "--hidden-import", "PySide6.QtWebEngineCore",
  "--hidden-import", "PySide6.QtWebEngineWidgets",
  "--hidden-import", "PySide6.QtWebChannel",
  "--add-data", "$icon;assets",
  "--add-data", "$tubiao;assets",
  "--add-data", "$font;assets",
  "--add-data", "$terms;assets/official-terms",
  # ADD-DATA 的 DEST 是目录：单文件必须写到所在的文件夹（文件名自带），
  # 否则数据落在 assets/<name>.json/<name>.json，加载器读不到那张表。
  "--add-data", "$allowlist;assets",
  "--add-data", "$displayAllowlist;assets",
  "--add-data", "$fragmentAllowlist;assets",
  "--add-data", "$glyphWidths;assets",
  "--add-data", "$webui;src/mc_hanhua/webui",
  $entry
)
& $PythonExe -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller build failed."
}
if ($Mode -eq "onedir") {
  Write-Host "Built dist\$name\$name.exe (directory bundle; ship the whole folder)"
} else {
  Write-Host "Built dist\$name.exe"
}
