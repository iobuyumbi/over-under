<#
.SYNOPSIS
  Reproducibly bootstrap a GitHub Actions self-hosted runner for this repo
  on a new Windows machine.

.DESCRIPTION
  You should NOT commit a live GitHub Actions runner folder to git because:
    - It is 150-300 MB of per-platform binaries.
    - It contains .credentials / .credentials_rsaparams files that hold the
      runner's long-lived OAuth access token for YOUR repository.
    - It includes machine-specific state (work dirs, labels, installed svc).

  Instead, run this ONE script on any new Windows machine. It will:
    1. Download a pinned GitHub Actions runner release for Windows x64.
    2. Extract it into a SIBLING folder next to your repo (NOT inside it),
       keeping your repo clean and gitignore-uncluttered.
    3. Ask you for the 1-hour-expiring registration token that GitHub gives
       you at Settings -> Actions -> Runners -> New self-hosted runner ->
       Windows. (That token is never persisted anywhere by this script.)
    4. Run config.cmd with the repo URL, token, and a unique machine-scoped
       runner name.
    5. Print next steps (start the runner interactively OR install as a
       Windows service so it auto-starts at boot).

.PARAMETER Repo
  Repository slug in OWNER/REPO form, e.g. "iobuyumbi/over-under".
  If omitted the script tries to read it from `git remote get-url origin`.

.PARAMETER RunnerDir
  Absolute or parent-relative path where the runner will be installed.
  Defaults to "..\actions-runner" (SIBLING of this repo, NOT inside it).

.PARAMETER Labels
  Comma-separated extra labels to assign to the runner in addition to the
  built-in "self-hosted,Windows,X64". The current workflow uses
  `runs-on: self-hosted` so you rarely need to change this.

.PARAMETER RunnerVersion
  Runner release version to download. Defaults to 2.320.0. Update this when
  GitHub prompts for a newer runner release (they auto-update anyway, but
  pinning avoids an update cycle on first launch).

.EXAMPLE
  .\actions-runner-bootstrap.ps1
  # Auto-detects repo from git remote; prompts for the GitHub token.

.EXAMPLE
  .\actions-runner-bootstrap.ps1 -Repo "iobuyumbi/over-under"
  # Specifies the repo explicitly.
#>

[CmdletBinding()]
param(
  [string]$Repo,
  [string]$RunnerDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "actions-runner"),
  [string]$Labels = "",
  [string]$RunnerVersion = "2.320.0"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Info([string]$m) { Write-Host "    $m" }
function Write-Warn([string]$m) { Write-Host "!!  $m" -ForegroundColor Yellow }
function Write-Err([string]$m)  { Write-Host "X   $m" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# 1. Resolve repo slug
# ---------------------------------------------------------------------------
if (-not $Repo) {
  Write-Step "Detecting repository from git remote"
  Push-Location $PSScriptRoot
  try {
    $origin = & git remote get-url origin 2>$null
  } finally {
    Pop-Location
  }
  if (-not $origin) {
    throw "Could not read 'git remote get-url origin'. Pass -Repo OWNER/REPO explicitly."
  }
  # ssh: git@github.com:OWNER/REPO.git
  # https: https://github.com/OWNER/REPO.git / OWNER/REPO
  $slug = if ($origin -match 'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$') { $Matches[1] } else { $null }
  if (-not $slug) {
    throw "Could not parse OWNER/REPO from git origin URL '$origin'. Pass -Repo explicitly."
  }
  $Repo = $slug
}
$RepoUrl = "https://github.com/$Repo"
Write-Info "Repository: $RepoUrl"

# ---------------------------------------------------------------------------
# 2. Ensure runner directory
# ---------------------------------------------------------------------------
$RunnerDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($RunnerDir)
Write-Info "Target runner directory: $RunnerDir"

if (Test-Path (Join-Path $RunnerDir ".runner")) {
  Write-Warn ".runner already exists at $RunnerDir"
  Write-Warn "   This runner looks registered. If you want a fresh install, delete"
  Write-Warn "   that folder first (running config.cmd twice is not supported)."
  Write-Warn "   Skipping download + config; we'll print next steps only."
  $alreadyInstalled = $true
} else {
  $alreadyInstalled = $false
  New-Item -ItemType Directory -Force -Path $RunnerDir | Out-Null
}

# ---------------------------------------------------------------------------
# 3. Download + extract (if needed)
# ---------------------------------------------------------------------------
if (-not $alreadyInstalled) {
  $archive = "actions-runner-win-x64-$RunnerVersion.zip"
  $archivePath = Join-Path $RunnerDir $archive
  $dlUrl = "https://github.com/actions/runner/releases/download/v$RunnerVersion/actions-runner-win-x64-$RunnerVersion.zip"

  if (-not (Test-Path $archivePath)) {
    Write-Step "Downloading GitHub Actions runner v$RunnerVersion for Windows x64"
    Write-Info "   URL: $dlUrl"
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $dlUrl -OutFile $archivePath -UseBasicParsing
  } else {
    Write-Step "Using existing archive $archivePath"
  }

  if (-not (Test-Path (Join-Path $RunnerDir "config.cmd"))) {
    Write-Step "Extracting archive"
    Expand-Archive -Path $archivePath -DestinationPath $RunnerDir -Force
    Write-Info "   Extracted."
  } else {
    Write-Info "config.cmd already present; skipping extract."
  }
}

# ---------------------------------------------------------------------------
# 4. Registration token prompt + config
# ---------------------------------------------------------------------------
if (-not $alreadyInstalled) {
  Write-Host ""
  Write-Step "Runner token required"
  Write-Info "Get a 1-hour-expiring token from:"
  Write-Info "   $RepoUrl/settings/actions/runners/new?arch=x64&os=win"
  Write-Info "Look for the box labelled:"
  Write-Info "   ./config.cmd --url $RepoUrl --token AAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  Write-Info ""
  $Token = Read-Host -Prompt "Paste the token (or the entire config.cmd line) and press Enter"
  if ($Token -match '--token\s+([A-Za-z0-9_\-]+)') {
    $Token = $Matches[1]
  }
  $Token = $Token.Trim()
  if (-not $Token) { throw "No token provided. Aborting." }

  $runnerName = "over-under-$($env:COMPUTERNAME)-" + (Get-Date -Format "yyyyMMdd")
  Write-Host ""
  Write-Step "Running config.cmd"
  Push-Location $RunnerDir
  try {
    $configArgs = @(
      "--unattended",
      "--url", $RepoUrl,
      "--token", $Token,
      "--name", $runnerName,
      "--replace",
      "--runasservice", "false",
      "--work", "_work"
    )
    if ($Labels) { $configArgs += @("--labels", $Labels) }
    & .\config.cmd @configArgs
    if ($LASTEXITCODE -ne 0) {
      throw "config.cmd exited with code $LASTEXITCODE"
    }
  } finally {
    Pop-Location
  }
}

# ---------------------------------------------------------------------------
# 5. Print next steps
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  Runner bootstrap complete."                                     -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Info ""
Write-Info "Option A — Run foreground (simplest, closes if you log out):"
Write-Info "   cd $RunnerDir"
Write-Info "   .\run.cmd"
Write-Info ""
Write-Info "Option B — Install as a Windows service (auto-runs at boot):"
Write-Info "   cd $RunnerDir"
Write-Info "   .\svc install"
Write-Info "   .\svc start"
Write-Info "   (optional: .\svc status, .\svc stop)"
Write-Info ""
Write-Info "Workflow file targeting this runner is already committed at:"
Write-Info "   .github/workflows/run_daily.yml   (runs-on: self-hosted)"
Write-Info ""
Write-Info "To RELOCATE the runner to another machine:"
Write-Info "   1. On the old machine: cd $RunnerDir ; .\config.cmd remove"
Write-Info "      (deletes the registration token from GitHub cleanly)"
Write-Info "   2. Copy ONLY this script to the new machine (git clone the repo)."
Write-Info "   3. Run:  .\actions-runner-bootstrap.ps1   on the new machine."
Write-Info ""
Write-Host "DONE." -ForegroundColor Green
