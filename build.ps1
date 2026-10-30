<#
.SYNOPSIS
    Lucianke を exe（PyInstaller・onedir）にビルドし、確かめて、インストールする。配布用の zip も作れる。

.DESCRIPTION
    配布は exe 形式（師匠の判断 2026-10-31）。手順:

    1. ビルド   uv run --extra langs --with pyinstaller==<固定の版> pyinstaller packaging\lucianke.spec
                出力: dist\lucianke\lucianke.exe と dist\lucianke\lucianke_internal\
    2. 確かめ   tools\smoke_exe.py（--version・doctor・tools/list がソースと同じ・tests/fixtures/poly での tools/call・
                stdout の純度・stdin EOF で終了コード 0）。-SkipSmoke で省く
    3. インストール  lucianke.exe と lucianke_internal\ をインストール先へ置く。-NoInstall で省く
                インストール先の決定順: -InstallDir → 環境変数 AGENT_INSTALL_DIR → %USERPROFILE%\.local\bin
    4. -Zip     dist\lucianke-<版>-windows-<arch>.zip と .sha256（配布物。LICENSE・THIRD_PARTY_NOTICES.md・Python の
                ライセンス入り）

    LSP サーバー（basedpyright・ruff・typescript-language-server など）は exe に入れない。ワークスペースの venv・
    node_modules・PATH・LUCIANKE_LSP_*_CMD から探す（無ければ Tree-sitter の structural に落ちる）。
    `lucianke doctor` で何が見つかるかを確かめられる。

.PARAMETER InstallDir
    インストール先。省略時は環境変数 AGENT_INSTALL_DIR、それも無ければ %USERPROFILE%\.local\bin。

.PARAMETER Force
    インストール先の lucianke.exe が動いている（MCP のホストが使っている）とき、そのプロセスを止めて置き換える。
    省略時は止めずにエラーにする。

.PARAMETER AddToPath
    インストール先が PATH（ユーザー・システム）に無ければ、ユーザーの PATH に足す。

.PARAMETER Uninstall
    インストール先から lucianke.exe と lucianke_internal\ を消す（ビルドはしない。PATH はそのまま）。

.EXAMPLE
    .\build.ps1
    ビルド → 確かめ → %USERPROFILE%\.local\bin（または AGENT_INSTALL_DIR）へインストール。

.EXAMPLE
    .\build.ps1 -Zip -NoInstall
    配布用の zip だけ作る。

.EXAMPLE
    .\build.ps1 -InstallDir D:\tools\bin -AddToPath -Force
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [switch]$NoInstall,
    [switch]$Zip,
    [switch]$SkipSmoke,
    [switch]$Force,
    [switch]$AddToPath,
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PyInstallerVersion = '6.22.3'  # 版を固定する（同じ入力から同じ exe を作る）
$Root = $PSScriptRoot
$Dist = Join-Path $Root 'dist'
$Work = Join-Path $Root 'build\pyinstaller'
$App = Join-Path $Dist 'lucianke'
$ExeName = 'lucianke.exe'
$RuntimeName = 'lucianke_internal'  # packaging\lucianke.spec の contents_directory と同じ名前

function Resolve-InstallDir {
    # 決定順: -InstallDir → AGENT_INSTALL_DIR → ~/.local/bin（マシン固有の置き先はリポジトリに焼き込まない）
    if ($InstallDir) { return [IO.Path]::GetFullPath($InstallDir) }
    if ($env:AGENT_INSTALL_DIR) { return [IO.Path]::GetFullPath($env:AGENT_INSTALL_DIR) }
    return (Join-Path $env:USERPROFILE '.local\bin')
}

function Invoke-Native {
    # Windows PowerShell 5.1 は、出力が転送されているとき native の stderr を ErrorRecord にする（Stop だと止まる）。
    # 成否は終了コードで判定する
    param([Parameter(Mandatory)][string]$What, [Parameter(Mandatory)][scriptblock]$Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "$What に失敗しました（終了コード $LASTEXITCODE）" }
}

function Get-ExeVersionLine([string]$Exe) {
    # `lucianke --version` は stderr に 1 行書く（stdout は MCP 専用なので使わない）
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = (& $Exe --version 2>&1 | ForEach-Object { "$_" }) -join '' } finally { $ErrorActionPreference = $prev }
    return $out.Trim()
}

function Get-SourceVersion {
    $line = Select-String -Path (Join-Path $Root 'src\lucianke\__init__.py') -Pattern '^__version__\s*=\s*"([^"]+)"'
    if (-not $line) { throw 'src\lucianke\__init__.py に __version__ が見つかりません' }
    return $line.Matches[0].Groups[1].Value
}

function Assert-NotRunning([string]$Dir) {
    # 動いている exe と lucianke_internal\ の DLL は置き換えられない（Windows はロックする）
    $exe = Join-Path $Dir $ExeName
    $procs = @(Get-Process -Name 'lucianke' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and ($_.Path -ieq $exe) })
    if ($procs.Count -eq 0) { return }
    $ids = ($procs | ForEach-Object { $_.Id }) -join ', '
    if (-not $Force) {
        throw ("$exe が動いています（PID $ids）。MCP のホスト（Claude Code など）が使っています。" +
            'ホストを閉じるか、-Force を付けてそのプロセスを止めてから置き換えてください。')
    }
    Write-Host "動いている lucianke.exe を止めます（PID $ids）"
    $procs | Stop-Process -Force
    foreach ($p in $procs) { [void]$p.WaitForExit(10000) }
}

function Test-OnPath([string]$Dir) {
    $want = $Dir.TrimEnd('\')
    $entries = @(
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        [Environment]::GetEnvironmentVariable('Path', 'Machine')
    ) -join ';' -split ';' | Where-Object { $_ } |
        ForEach-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') }
    return [bool]($entries | Where-Object { $_ -ieq $want })
}

function Add-UserPath([string]$Dir) {
    # レジストリの生の値（%USERPROFILE% などを展開しない）に足し、REG_EXPAND_SZ のまま書き戻す
    $key = Get-Item 'HKCU:\Environment'
    $raw = $key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $new = if ($raw) { $raw.TrimEnd(';') + ';' + $Dir } else { $Dir }
    Set-ItemProperty -Path 'HKCU:\Environment' -Name 'Path' -Value $new -Type ExpandString
    # 環境変数の変更を通知する（新しく開くターミナルが拾う）。ダミーの変数を足して消すと WM_SETTINGCHANGE が飛ぶ
    [Environment]::SetEnvironmentVariable('LUCIANKE_BUILD_PATH_NOTIFY', '1', 'User')
    [Environment]::SetEnvironmentVariable('LUCIANKE_BUILD_PATH_NOTIFY', $null, 'User')
    $env:Path = "$env:Path;$Dir"
}

function Remove-Installed([string]$Dir) {
    Assert-NotRunning $Dir
    $exe = Join-Path $Dir $ExeName
    $runtime = Join-Path $Dir $RuntimeName
    if (Test-Path $exe) { Remove-Item -Force $exe }
    if (Test-Path $runtime) { Remove-Item -Recurse -Force $runtime }
}

# --- アンインストール ---------------------------------------------------------------------------------------
if ($Uninstall) {
    $target = Resolve-InstallDir
    Remove-Installed $target
    Write-Host "アンインストールしました: $target（PATH はそのままです）"
    return
}

# --- 1. ビルド ------------------------------------------------------------------------------------------
$version = Get-SourceVersion
Write-Host "== ビルド: lucianke $version（PyInstaller $PyInstallerVersion、onedir）"
$started = Get-Date
Push-Location $Root
try {
    Invoke-Native 'PyInstaller のビルド' {
        uv run -q --extra langs --with "pyinstaller==$PyInstallerVersion" `
            pyinstaller --noconfirm --clean --log-level WARN `
            --distpath $Dist --workpath $Work (Join-Path $Root 'packaging\lucianke.spec')
    }
} finally {
    Pop-Location
}
$exePath = Join-Path $App $ExeName
if (-not (Test-Path $exePath)) { throw "ビルドの出力がありません: $exePath" }
# 配布物の根にもライセンスを置く（同じものが lucianke_internal\licenses\ にもある。インストールは exe と実行時の中身だけ）
Copy-Item -Force (Join-Path $Root 'LICENSE') $App
Copy-Item -Force (Join-Path $Root 'THIRD_PARTY_NOTICES.md') $App
$size = (Get-ChildItem -Recurse -File $App | Measure-Object -Sum Length).Sum / 1MB
Write-Host ("   {0}（{1:N1} MB、{2:N0} 秒）" -f $App, $size, ((Get-Date) - $started).TotalSeconds)

# --- 2. 確かめ ------------------------------------------------------------------------------------------
if ($SkipSmoke) {
    Write-Host '== 確かめ: 省略（-SkipSmoke）'
} else {
    Write-Host '== 確かめ: tools\smoke_exe.py'
    Push-Location $Root
    try {
        Invoke-Native '確かめ（smoke）' { uv run -q --extra langs python (Join-Path $Root 'tools\smoke_exe.py') $exePath }
    } finally {
        Pop-Location
    }
}

# --- 3. 配布用 zip ---------------------------------------------------------------------------------------
if ($Zip) {
    $arch = switch ($env:PROCESSOR_ARCHITECTURE) {
        'AMD64' { 'x64' }
        'ARM64' { 'arm64' }
        default { $env:PROCESSOR_ARCHITECTURE.ToLowerInvariant() }
    }
    $zipPath = Join-Path $Dist "lucianke-$version-windows-$arch.zip"
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    # zip の中は lucianke/ フォルダ 1 つ（展開先を散らかさない）。項目名の区切りは zip の仕様どおり '/'
    # （Windows PowerShell 5.1 の ZipFile.CreateFromDirectory は '\' で書き、Windows 以外の展開で壊れる）
    $zipFile = [IO.Compression.ZipFile]::Open($zipPath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $base = Split-Path -Parent $App
        foreach ($f in Get-ChildItem -Recurse -File $App) {
            $entry = $f.FullName.Substring($base.Length + 1).Replace('\', '/')
            [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zipFile, $f.FullName, $entry, [IO.Compression.CompressionLevel]::Optimal)
        }
    } finally {
        $zipFile.Dispose()
    }
    $hash = (Get-FileHash -Algorithm SHA256 $zipPath).Hash.ToLowerInvariant()
    Set-Content -Path "$zipPath.sha256" -Value "$hash  $(Split-Path -Leaf $zipPath)" -Encoding ascii
    Write-Host "== 配布用 zip: $zipPath"
    Write-Host "   sha256 $hash"
}

# --- 4. インストール --------------------------------------------------------------------------------------
if ($NoInstall) {
    Write-Host '== インストール: 省略（-NoInstall）'
    return
}
$target = Resolve-InstallDir
Write-Host "== インストール: $target"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Assert-NotRunning $target
$runtime = Join-Path $target $RuntimeName
$staging = "$runtime.new"
if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
Copy-Item -Recurse (Join-Path $App $RuntimeName) $staging  # 先に写してから入れ替える（途中で失敗しても旧版が残る）
if (Test-Path $runtime) { Remove-Item -Recurse -Force $runtime }
Rename-Item $staging $RuntimeName
Copy-Item -Force $exePath (Join-Path $target $ExeName)

$installed = Join-Path $target $ExeName
$line = Get-ExeVersionLine $installed
if ($line -ne "lucianke $version") { throw "インストールした exe の版が合いません: '$line'（期待 'lucianke $version'）" }
Write-Host "   $installed → $line"

if (Test-OnPath $target) {
    Write-Host '   PATH: 通っています'
} elseif ($AddToPath) {
    Add-UserPath $target
    Write-Host '   PATH: ユーザーの PATH に足しました（新しく開くターミナルから有効）'
} else {
    Write-Host "   PATH: $target は PATH にありません。-AddToPath を付けて再実行するか、MCP の設定で exe のフルパスを指定してください。"
}
Write-Host "   MCP の設定例: { `"command`": `"$($installed -replace '\\', '\\')`" }"
