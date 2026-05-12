#!/usr/bin/env bash
# seed_corpus.sh — clone the three public security corpora that Grimoire's
# security domain understands out of the box (Sigma rules, MITRE ATT&CK
# STIX bundles, and one year of NVD CVE bulk JSON) into $CORPUS_DIR.
#
# This is idempotent: re-running it pulls updates rather than re-cloning.
# Designed for a fresh Hetzner box; safe to run as a non-root user in
# the home directory.
#
# Usage:
#   CORPUS_DIR=/srv/grimoire/security-corpus ./scripts/security/seed_corpus.sh
#   ./scripts/security/seed_corpus.sh         # defaults to ./security-corpus
#
# Required tools: git, curl, gzip (or gunzip), bash 4+.

set -euo pipefail

CORPUS_DIR="${CORPUS_DIR:-./security-corpus}"
NVD_YEAR="${NVD_YEAR:-$(date -u +%Y)}"
SIGMA_REPO="${SIGMA_REPO:-https://github.com/SigmaHQ/sigma.git}"
MITRE_REPO="${MITRE_REPO:-https://github.com/mitre/cti.git}"
NVD_BASE_URL="${NVD_BASE_URL:-https://nvd.nist.gov/feeds/json/cve/1.1}"

log() {
    printf '[seed_corpus] %s\n' "$*" >&2
}

require() {
    if ! command -v "$1" >/dev/null 2>&1; then
        log "ERROR: required tool '$1' is not installed"
        exit 1
    fi
}

require git
require curl
require gzip

mkdir -p "${CORPUS_DIR}"

# ---------------------------------------------------------------------------
# Sigma rules
# ---------------------------------------------------------------------------

SIGMA_DIR="${CORPUS_DIR}/sigma-rules"
if [[ -d "${SIGMA_DIR}/.git" ]]; then
    log "Updating existing Sigma checkout: ${SIGMA_DIR}"
    git -C "${SIGMA_DIR}" pull --ff-only
else
    log "Cloning Sigma rules to ${SIGMA_DIR}"
    git clone --depth 1 "${SIGMA_REPO}" "${SIGMA_DIR}"
fi

# ---------------------------------------------------------------------------
# MITRE ATT&CK (STIX 2.1 bundles)
# ---------------------------------------------------------------------------

MITRE_DIR="${CORPUS_DIR}/mitre-attack"
if [[ -d "${MITRE_DIR}/.git" ]]; then
    log "Updating existing MITRE ATT&CK checkout: ${MITRE_DIR}"
    git -C "${MITRE_DIR}" pull --ff-only
else
    log "Cloning MITRE CTI repo to ${MITRE_DIR}"
    git clone --depth 1 "${MITRE_REPO}" "${MITRE_DIR}"
fi

# ---------------------------------------------------------------------------
# NVD CVE bulk JSON
# ---------------------------------------------------------------------------

NVD_DIR="${CORPUS_DIR}/nvd-cve"
mkdir -p "${NVD_DIR}"

NVD_FILE="${NVD_DIR}/nvdcve-1.1-${NVD_YEAR}.json"
NVD_GZ="${NVD_FILE}.gz"
NVD_URL="${NVD_BASE_URL}/nvdcve-1.1-${NVD_YEAR}.json.gz"

if [[ -f "${NVD_FILE}" ]]; then
    log "NVD ${NVD_YEAR} already present at ${NVD_FILE} (delete to force re-download)"
else
    log "Downloading NVD ${NVD_YEAR} bulk feed: ${NVD_URL}"
    # ``-f`` makes curl fail on 4xx/5xx so the script exits non-zero rather
    # than leaving a half-written file. ``-L`` follows redirects.
    curl -fSL "${NVD_URL}" -o "${NVD_GZ}"
    log "Decompressing ${NVD_GZ}"
    gzip -d "${NVD_GZ}"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

log "Done. Corpus layout under ${CORPUS_DIR}:"
log "  $(find "${SIGMA_DIR}/rules" -name '*.yml' 2>/dev/null | wc -l) Sigma rules"
log "  $(find "${MITRE_DIR}/enterprise-attack" -name 'attack-pattern--*.json' 2>/dev/null | wc -l) MITRE ATT&CK technique objects"
log "  $(du -h "${NVD_FILE}" 2>/dev/null | awk '{print $1}') NVD ${NVD_YEAR} JSON"
log ""
log "Next step: point Grimoire at the corpus, e.g."
log "  grimoire ingest --source-type sigma_rule  ${SIGMA_DIR}/rules"
log "  grimoire ingest --source-type mitre_attack ${MITRE_DIR}/enterprise-attack"
log "  grimoire ingest --source-type nvd_cve     ${NVD_FILE}"
