#!/usr/bin/with-contenv bashio

OCR_LANG="$(bashio::config 'ocr_lang' 'es')"
export OCR_LANG

GENERATE_API_TOKEN="$(bashio::config 'generate_api_token' 'false')"
if bashio::var.true "${GENERATE_API_TOKEN}"; then
    GENERATED_TOKEN="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
    bashio::log.warning "Generated Concierge OCR API token: ${GENERATED_TOKEN}"
    bashio::log.warning "Copy this token into the api_token add-on option, set generate_api_token to false, save, and start the add-on again."
    exit 0
fi

API_TOKEN="$(bashio::config 'api_token' '')"
if [[ ${#API_TOKEN} -lt 32 ]]; then
    bashio::log.fatal "api_token must contain at least 32 characters. Set generate_api_token to true, start the add-on once, copy the generated token from the logs, then paste it into api_token and set generate_api_token back to false."
    exit 1
fi
export API_TOKEN

bashio::log.info "Starting Concierge OCR API on port 8099 (ocr_lang=${OCR_LANG})"
exec uvicorn app.main:app --host 0.0.0.0 --port 8099
