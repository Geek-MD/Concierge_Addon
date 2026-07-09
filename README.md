# Concierge addon

[![Publish](https://github.com/Geek-MD/Concierge_addon/actions/workflows/publish.yml/badge.svg)](https://github.com/Geek-MD/Concierge_addon/actions/workflows/publish.yml)
[![Release](https://img.shields.io/github/v/release/Geek-MD/Concierge_addon.svg)](https://github.com/Geek-MD/Concierge_addon/releases)
[![Stars](https://img.shields.io/github/stars/Geek-MD/Concierge_addon.svg)](https://github.com/Geek-MD/Concierge_addon/stargazers)

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)
![Supports armv7 Architecture](https://img.shields.io/badge/armv7-yes-green.svg)

Home Assistant add-on that exposes a REST API and a Web UI to analyze PDFs with PaddleOCR and return the result as JSON.

## Compatibility note

The add-on Docker image uses the multi-architecture Home Assistant Debian base image (`ghcr.io/home-assistant/base-debian:bookworm`).
This provides a glibc environment so that `paddlepaddle` and other ML dependencies can be installed from pre-built `manylinux` wheels on all supported architectures (including `aarch64`).
A previous Alpine-based image caused a silent build timeout on aarch64 because `paddlepaddle` has no musl/Alpine wheel and pip fell back to a source compilation that exceeded the HA Supervisor build timeout.

FastAPI endpoints that accept `multipart/form-data` (such as `/ocr` and `/ocr/source`) require `python-multipart`; this dependency is included to avoid startup failures during `uvicorn` boot.
The image also includes `libgl1` and `libglib2.0-0` so OpenCV can import correctly at startup on Debian-based Home Assistant environments.
The runtime dependencies explicitly include `paddlepaddle` to ensure `paddleocr` can initialize without startup import errors.

## Structure

- `repository.yaml`: add-on repository metadata.
- `concierge_ocr/`: add-on definition.
  - `config.yaml`: Home Assistant add-on configuration.
  - `Dockerfile`: add-on image.
  - `run.sh`: API startup script.
  - `requirements.txt`: Python dependencies.
  - `app/main.py`: REST API (`/health`, `/templates`, `/ocr`, `/ocr/source`) and Web UI (`/`).

## Usage

1. Add this repository as an **Add-on repository** in Home Assistant.
2. Install the **Concierge OCR API** add-on.
3. Start the add-on.
4. Open the Web UI from the add-on page. If you enable **Show in sidebar**, it will also appear in the side panel.
5. Enter:
   - a PDF URL (`http/https`), or
   - a local Home Assistant path (`/config`, `/share`, `/media`).
   - If your integration returns `/homeassistant/...`, it is also accepted and treated as an alias for `/config/...`.
6. Run the analysis and download the JSON if needed.
7. If an analysis fails, review the add-on logs from Home Assistant; request and PDF processing errors are now logged with diagnostic details.

## API REST

### `POST /ocr`

Send a PDF over REST:

```bash
curl -X POST "http://HOME_ASSISTANT_HOST:8099/ocr" \
  -F "file=@document.pdf"
```

Raw PDF content in the request body is also supported (`Content-Type: application/pdf`).

Optional query params:

- `template_id=<builtin_template_id>`
- `template_json=<json_template_string>`

When one of these template parameters is provided, the endpoint returns a structured JSON output (section-based) instead of the raw OCR payload.

Example response:

```json
{
  "page_count": 1,
  "pages": [
    {
      "page": 1,
      "lines": [
        {
          "text": "Hello world",
          "confidence": 0.99,
          "box": [[0, 0], [100, 0], [100, 20], [0, 20]]
        }
      ],
      "text": "Hello world"
    }
  ],
  "text": "Hello world"
}
```

### `POST /ocr/source`

Allows processing a PDF from a remote or local source:

- `source_type=url` and `source_value=https://.../file.pdf`
- `source_type=local_path` and `source_value=/config/file.pdf`
- `source_type=local_path` and `source_value=/homeassistant/file.pdf` (alias to `/config/file.pdf`)

Optional form fields:

- `template_id`
- `template_json`

If provided, the OCR result is transformed into a template-structured output.

### `GET /templates`

Lists built-in template IDs available for `template_id`.

### Template shape (`template_json`)

Templates now represent real PDF sections explicitly:

- `sections[]`: each logical/visual section in the PDF.
- `sections[].lines[]`: logical lines inside the section.
- `sections[].lines[].boxes[]`: text boxes with semantic `role`.

Supported `role` values:

- `fixed`: canonical label text (supports fuzzy matching and overwrite of OCR text).
- `variable`: value that changes from PDF to PDF and is associated using `locator.strategy`.
- `mixed`: one line containing fixed + variable text using `?` placeholders in template text.
- `ignore`: currently ignored in output.

### Shorthand template (section/line/type)

The API also accepts a shorthand schema where each line uses a simple `type`:

- `fixed`: static text anchor
- `mixed`: static + variable in one line, where `?` marks the variable segment(s)

Example:

```json
{
  "section": {
    "id": "encabezado_pago",
    "name": "Encabezado",
    "lines": [
      { "text": "¡Paga tu Gasto Común en línea!", "type": "fixed" },
      { "text": "Ingresa a: https://pagos.kastor.cl", "type": "fixed" },
      { "text": "Código cliente: ??????-?????", "type": "mixed", "key": "codigo_cliente" }
    ]
  }
}
```

This shorthand is internally converted to the canonical `sections -> lines -> boxes` structure.

Minimal example:

```json
{
  "template_id": "gasto_comun_v1",
  "document_type": "gasto_comun",
  "matching": {
    "normalize_accents": true,
    "ignore_case": true,
    "collapse_whitespace": true,
    "default_fuzzy_threshold": 0.82
  },
  "sections": [
    {
      "id": "datos_comunidad",
      "anchors": ["Comunidad", "Dirección", "Fecha de último pago"],
      "lines": [
        {
          "id": "linea_comunidad",
          "boxes": [
            { "role": "fixed", "key": "comunidad_label", "canonical_text": "Comunidad", "overwrite_ocr_text": true },
            { "role": "variable", "key": "comunidad", "locator": { "strategy": "nearest_right_or_below", "max_distance": 2 } }
          ]
        }
      ]
    }
  ]
}
```
