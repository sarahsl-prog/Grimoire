# seed_corpus.ps1 — clone the three public security corpora that Grimoire's
# security domain understands out of the box (Sigma rules, MITRE ATT&CK
# STIX bundles, and one year of NVD CVE bulk JSON) into $CORPUS_DIR.
#
# This is idempotent: re-running it pulls updates rather than re-cloning,
# and an already-downloaded NVD feed is left alone.
#
# NVD feed version: the annual bulk feed moved from the JSON 1.1 schema
# (feeds/json/cve/1.1/nvdcve-1.1-{year}.json.gz, now 403) to the JSON 2.0
# schema (feeds/json/cve/2.0/nvdcve-2.0-{year}.json.gz). Same URL structure,
# same one-complete-year-per-file shape — only the schema changed. This
# matches what Grimoire's NVD parser already expects, so nothing downstream
# of this script needs to change.
#
# Windows/PowerShell port of seed_corpus.sh. Unlike the shell version this
# script needs no curl.exe or gzip.exe: downloads use Invoke-WebRequest and
# decompression uses .NET's System.IO.Compression.GZipStream.
#
# Usage:
#   $env:CORPUS_DIR = "C:\srv\grimoire\security-corpus"; .\scripts\security\seed_corpus.ps1
#   .\scripts\security\seed_corpus.ps1         # defaults to .\security-corpus
#
# Configuration (all overridable via environment variables):
#   CORPUS_DIR, NVD_YEAR, SIGMA_REPO, MITRE_REPO, NVD_BASE_URL
#
# Required tools: git.
# Compatible with Windows PowerShell 5.1 and PowerShell 7+.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Suppress the Invoke-WebRequest progress bar; on Windows PowerShell 5.1 it
# slows large downloads down by an order of magnitude.
$ProgressPreference = "SilentlyContinue"

$CorpusDir  = if ($env:CORPUS_DIR)   { $env:CORPUS_DIR }   else { ".\security-corpus" }
# .Year.ToString() rather than ToString("yyyy"): the latter is culture-sensitive
# and would yield a Buddhist/Hijri year (2569, 1448) under th-TH or ar-SA, where
# bash's `date -u +%Y` is always Gregorian.
$NvdYear    = if ($env:NVD_YEAR)     { $env:NVD_YEAR }     else { [DateTime]::UtcNow.Year.ToString([System.Globalization.CultureInfo]::InvariantCulture) }
$SigmaRepo  = if ($env:SIGMA_REPO)   { $env:SIGMA_REPO }   else { "https://github.com/SigmaHQ/sigma.git" }
$MitreRepo  = if ($env:MITRE_REPO)   { $env:MITRE_REPO }   else { "https://github.com/mitre/cti.git" }
$NvdBaseUrl = if ($env:NVD_BASE_URL) { $env:NVD_BASE_URL } else { "https://nvd.nist.gov/feeds/json/cve/2.0" }

function Write-Log {
    param([string]$Message = "")
    # Mirrors the shell script's `printf ... >&2`.
    [Console]::Error.WriteLine("[seed_corpus] $Message")
}

function Require-Tool {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Log "ERROR: required tool '$Name' is not installed"
        exit 1
    }
}

function Invoke-Git {
    # PowerShell does not fail on a non-zero exit code from a native .exe the
    # way `set -e` does in bash, so every git call is checked explicitly.
    # Arguments are passed as a single array so that git's own flags (-C, ...)
    # are never mistaken for parameters of this function.
    param([Parameter(Mandatory = $true, Position = 0)][string[]]$GitArgs)
    & git @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Format-HumanSize {
    # Approximates `du -h` output for a single file. Formatting is pinned to the
    # invariant culture so the decimal separator is always '.' (de-DE would
    # otherwise render 2.5G as "2,5G").
    param([Parameter(Mandatory = $true)][long]$Bytes)
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    if ($Bytes -ge 1GB) { return [string]::Format($invariant, "{0:0.0}G", ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return [string]::Format($invariant, "{0:0}M",   ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return [string]::Format($invariant, "{0:0}K",   ($Bytes / 1KB)) }
    return "${Bytes}B"
}

function Expand-GzipFile {
    # gzip -d replacement: decompress $Source to $Destination, then remove the
    # .gz just as `gzip -d` does on success.
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $input__ = $null
    $gzip    = $null
    $output  = $null
    try {
        $input__ = [System.IO.File]::OpenRead($Source)
        $gzip    = New-Object System.IO.Compression.GZipStream($input__, [System.IO.Compression.CompressionMode]::Decompress)
        $output  = [System.IO.File]::Create($Destination)
        $gzip.CopyTo($output)
    }
    finally {
        if ($output)  { $output.Dispose() }
        if ($gzip)    { $gzip.Dispose() }
        if ($input__) { $input__.Dispose() }
    }
    Remove-Item -LiteralPath $Source -Force
}

Require-Tool git

# Create the corpus root and pin $CorpusDir to its absolute path. This matters:
# the .NET file APIs used by Expand-GzipFile resolve relative paths against
# [Environment]::CurrentDirectory, which PowerShell's Set-Location does NOT
# update, so a relative CORPUS_DIR (the documented default, .\security-corpus)
# would send decompression to the wrong directory. Every path below is derived
# from $CorpusDir, so resolving once here fixes all of them.
$CorpusDir = (New-Item -ItemType Directory -Path $CorpusDir -Force).FullName

# ---------------------------------------------------------------------------
# Sigma rules
# ---------------------------------------------------------------------------

$SigmaDir = Join-Path $CorpusDir "sigma-rules"
if (Test-Path -LiteralPath (Join-Path $SigmaDir ".git") -PathType Container) {
    Write-Log "Updating existing Sigma checkout: $SigmaDir"
    Invoke-Git @("-C", $SigmaDir, "pull", "--ff-only")
}
else {
    Write-Log "Cloning Sigma rules to $SigmaDir"
    Invoke-Git @("clone", "--depth", "1", $SigmaRepo, $SigmaDir)
}

# ---------------------------------------------------------------------------
# MITRE ATT&CK (STIX 2.1 bundles)
# ---------------------------------------------------------------------------

$MitreDir = Join-Path $CorpusDir "mitre-attack"
if (Test-Path -LiteralPath (Join-Path $MitreDir ".git") -PathType Container) {
    Write-Log "Updating existing MITRE ATT&CK checkout: $MitreDir"
    Invoke-Git @("-C", $MitreDir, "pull", "--ff-only")
}
else {
    Write-Log "Cloning MITRE CTI repo to $MitreDir"
    Invoke-Git @("clone", "--depth", "1", $MitreRepo, $MitreDir)
}

# ---------------------------------------------------------------------------
# NVD CVE bulk JSON
# ---------------------------------------------------------------------------

$NvdDir = Join-Path $CorpusDir "nvd-cve"
New-Item -ItemType Directory -Path $NvdDir -Force | Out-Null

$NvdFile = Join-Path $NvdDir "nvdcve-2.0-$NvdYear.json"
$NvdGz   = "$NvdFile.gz"
$NvdUrl  = "$NvdBaseUrl/nvdcve-2.0-$NvdYear.json.gz"

if (Test-Path -LiteralPath $NvdFile -PathType Leaf) {
    Write-Log "NVD $NvdYear already present at $NvdFile (delete to force re-download)"
}
else {
    Write-Log "Downloading NVD $NvdYear bulk feed: $NvdUrl"
    # Windows PowerShell 5.1 may still default to TLS 1.0; nvd.nist.gov requires 1.2+.
    try {
        [Net.ServicePointManager]::SecurityProtocol = `
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    }
    catch {
        # PowerShell 7 on some platforms disallows setting this; the default is already fine.
    }

    # Invoke-WebRequest throws on 4xx/5xx (like `curl -f`) and follows
    # redirects by default (like `curl -L`). Remove any half-written file so
    # a failed run never leaves a truncated archive behind.
    try {
        Invoke-WebRequest -Uri $NvdUrl -OutFile $NvdGz -UseBasicParsing
    }
    catch {
        if (Test-Path -LiteralPath $NvdGz) { Remove-Item -LiteralPath $NvdGz -Force }
        Write-Log "ERROR: failed to download $NvdUrl : $($_.Exception.Message)"
        exit 1
    }

    Write-Log "Decompressing $NvdGz"
    try {
        Expand-GzipFile -Source $NvdGz -Destination $NvdFile
    }
    catch {
        # Clear both sides: the partial .json and the .gz that failed to
        # decompress, so the next run re-downloads cleanly rather than
        # retrying a corrupt archive.
        Remove-Item -LiteralPath $NvdFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $NvdGz -Force -ErrorAction SilentlyContinue
        Write-Log "ERROR: failed to decompress $NvdGz : $($_.Exception.Message)"
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

$SigmaRulesDir = Join-Path $SigmaDir "rules"
$SigmaCount = 0
if (Test-Path -LiteralPath $SigmaRulesDir -PathType Container) {
    $SigmaCount = @(Get-ChildItem -LiteralPath $SigmaRulesDir -Recurse -File -Filter "*.yml" -ErrorAction SilentlyContinue).Count
}

$MitreEnterpriseDir = Join-Path $MitreDir "enterprise-attack"
$MitreCount = 0
if (Test-Path -LiteralPath $MitreEnterpriseDir -PathType Container) {
    $MitreCount = @(Get-ChildItem -LiteralPath $MitreEnterpriseDir -Recurse -File -Filter "attack-pattern--*.json" -ErrorAction SilentlyContinue).Count
}

$NvdSize = ""
if (Test-Path -LiteralPath $NvdFile -PathType Leaf) {
    $NvdSize = Format-HumanSize -Bytes (Get-Item -LiteralPath $NvdFile).Length
}

Write-Log "Done. Corpus layout under ${CorpusDir}:"
Write-Log "  $SigmaCount Sigma rules"
Write-Log "  $MitreCount MITRE ATT&CK technique objects"
Write-Log "  $NvdSize NVD $NvdYear JSON"
Write-Log ""
Write-Log "Next step: point Grimoire at the corpus, e.g."
Write-Log "  grimoire ingest --source-type sigma_rule  $SigmaRulesDir"
Write-Log "  grimoire ingest --source-type mitre_attack $MitreEnterpriseDir"
Write-Log "  grimoire ingest --source-type nvd_cve     $NvdFile"
