#!/usr/bin/with-contenv bashio

OCR_LANG="$(bashio::config 'ocr_lang' 'es')"
export OCR_LANG

API_TOKEN="$(bashio::config 'api_token' '')"
if [[ ${#API_TOKEN} -lt 32 ]]; then
    bashio::log.fatal "api_token must contain at least 32 characters. Generate one with: openssl rand -hex 32"
    exit 1
fi
export API_TOKEN

bashio::log.info "Starting Concierge OCR API on port 8099 (ocr_lang=${OCR_LANG})"
exec uvicorn app.main:app --host 0.0.0.0 --port 8099
