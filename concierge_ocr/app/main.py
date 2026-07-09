import ipaddress
import json
import logging
import os
import re
import socket
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import cv2
import httpx
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pdf2image import convert_from_bytes
from paddleocr import PaddleOCR

app = FastAPI(title="Concierge OCR API", version="0.3.0")
logger = logging.getLogger("concierge_ocr.api")

_OCR_INSTANCE: PaddleOCR | None = None
HOMEASSISTANT_LOCAL_ALIAS = "homeassistant"
LOCAL_ALLOWED_BASE_DIRS = tuple(Path(path) for path in os.getenv("LOCAL_PDF_BASE_PATHS", "/config,/share,/media").split(","))
RESOLVED_LOCAL_BASE_DIRS = tuple(
    path.expanduser().resolve() for path in LOCAL_ALLOWED_BASE_DIRS if path.expanduser().exists()
)

WEB_UI_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Concierge OCR Web UI</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 1rem; max-width: 900px; }
    h1 { margin-top: 0; }
    form { display: grid; gap: .75rem; margin-bottom: 1rem; }
    select, input, button, textarea { font: inherit; padding: .5rem; }
    textarea { min-height: 240px; width: 100%; }
    .row { display: grid; gap: .5rem; }
    .hint { color: #666; font-size: .9rem; }
    .actions { display: flex; gap: .5rem; flex-wrap: wrap; }
    [hidden] { display: none !important; }
  </style>
</head>
<body>
  <h1>Concierge OCR Web UI</h1>
  <p>Enter a URL (<code>http/https</code>), a local path mounted in Home Assistant (<code>/config</code>, <code>/share</code>, <code>/media</code> or <code>/homeassistant</code> as alias of <code>/config</code>), or upload a PDF file directly.</p>
  <form id="ocrForm">
    <div class="row">
      <label for="sourceType">Source type</label>
      <select id="sourceType" name="sourceType">
        <option value="url">URL</option>
        <option value="local_path">Local path</option>
        <option value="file_upload">Upload file</option>
      </select>
    </div>
    <div class="row" id="sourceValueRow">
      <label for="sourceValue">PDF URL or path</label>
      <input id="sourceValue" name="sourceValue" placeholder="https://.../file.pdf or /config/file.pdf (/homeassistant/... also supported)" required />
      <span class="hint">Only PDF files are supported.</span>
    </div>
    <div class="row" id="fileUploadRow" hidden>
      <label for="fileInput">PDF file</label>
      <input id="fileInput" name="fileInput" type="file" accept=".pdf,application/pdf" />
      <span class="hint">Only PDF files are supported.</span>
    </div>
    <div class="actions">
      <button type="submit">Analyze PDF</button>
      <button id="downloadBtn" type="button" disabled>Download JSON</button>
    </div>
  </form>

  <label for="result">JSON result</label>
  <textarea id="result" readonly placeholder="The JSON response will appear here..."></textarea>

  <script>
    const form = document.getElementById('ocrForm');
    const sourceType = document.getElementById('sourceType');
    const sourceValue = document.getElementById('sourceValue');
    const fileInput = document.getElementById('fileInput');
    const sourceValueRow = document.getElementById('sourceValueRow');
    const fileUploadRow = document.getElementById('fileUploadRow');
    const result = document.getElementById('result');
    const downloadBtn = document.getElementById('downloadBtn');
    let latestJson = null;

    sourceType.addEventListener('change', () => {
      const isUpload = sourceType.value === 'file_upload';
      sourceValueRow.hidden = isUpload;
      fileUploadRow.hidden = !isUpload;
      sourceValue.required = !isUpload;
      fileInput.required = isUpload;
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      result.value = 'Processing...';
      downloadBtn.disabled = true;
      latestJson = null;

      const payload = new FormData();

      try {
        const basePath = window.location.pathname.replace(/\/+$/, '');
        let endpoint;
        if (sourceType.value === 'file_upload') {
          const file = fileInput.files[0];
          if (!file) {
            throw new Error('Please select a PDF file to upload');
          }
          if (file.type && file.type !== 'application/pdf') {
            throw new Error('The selected file is not a PDF');
          }
          payload.append('file', file, file.name);
          endpoint = new URL(`${basePath}/ocr`, window.location.origin);
        } else {
          payload.append('source_type', sourceType.value);
          payload.append('source_value', sourceValue.value.trim());
          endpoint = new URL(`${basePath}/ocr/source`, window.location.origin);
        }

        const response = await fetch(endpoint, { method: 'POST', body: payload });
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.detail || 'Unexpected error');
        }
        latestJson = data;
        result.value = JSON.stringify(data, null, 2);
        downloadBtn.disabled = false;
      } catch (error) {
        result.value = `Error: ${error.message}`;
      }
    });

    downloadBtn.addEventListener('click', () => {
      if (!latestJson) return;
      const blob = new Blob([JSON.stringify(latestJson, null, 2)], { type: 'application/json' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'ocr_result.json';
      link.click();
      URL.revokeObjectURL(link.href);
    });
  </script>
</body>
</html>
"""

DEFAULT_TEMPLATE: dict[str, Any] = {
    "template_id": "gasto_comun_section_example_v1",
    "document_type": "gasto_comun",
    "matching": {
        "normalize_accents": True,
        "ignore_case": True,
        "collapse_whitespace": True,
        "default_fuzzy_threshold": 0.82,
    },
    "output": {
        "include_raw_text": False,
        "include_boxes": False,
        "include_confidence": False,
    },
    "sections": [
        {
            "id": "datos_comunidad",
            "name": "Datos de la comunidad",
            "anchors": [
                "Comunidad",
                "Dirección",
                "Fecha de último pago",
            ],
            "min_score": 0.8,
            "lines": [
                {
                    "id": "linea_comunidad",
                    "boxes": [
                        {
                            "role": "fixed",
                            "key": "comunidad_label",
                            "canonical_text": "Comunidad",
                            "required": True,
                            "overwrite_ocr_text": True,
                        },
                        {
                            "role": "variable",
                            "key": "comunidad",
                            "value_type": "string",
                            "required": False,
                            "locator": {
                                "strategy": "nearest_right_or_below",
                                "max_distance": 2,
                            },
                        },
                    ],
                },
                {
                    "id": "linea_direccion",
                    "boxes": [
                        {
                            "role": "fixed",
                            "key": "direccion_label",
                            "canonical_text": "Dirección",
                            "required": True,
                            "overwrite_ocr_text": True,
                        },
                        {
                            "role": "variable",
                            "key": "direccion",
                            "value_type": "string",
                            "required": False,
                            "locator": {
                                "strategy": "nearest_right_or_below",
                                "max_distance": 2,
                            },
                        },
                    ],
                },
                {
                    "id": "linea_fecha_ultimo_pago",
                    "boxes": [
                        {
                            "role": "fixed",
                            "key": "fecha_ultimo_pago_label",
                            "canonical_text": "Fecha de último pago",
                            "required": True,
                            "overwrite_ocr_text": True,
                        },
                        {
                            "role": "variable",
                            "key": "fecha_ultimo_pago",
                            "value_type": "date",
                            "required": False,
                            "locator": {
                                "strategy": "nearest_right_or_below",
                                "max_distance": 2,
                            },
                        },
                    ],
                },
            ],
        },
        {
            "id": "tabla_nota_cobro",
            "name": "Tabla nota de cobro",
            "anchors": [
                "Nota de Cobro Mes",
                "Copropietario",
                "Fecha último Pago",
                "Alícuota Total",
                "Monto último Pago",
                "Gasto común a Prorratear",
                "Folio último Pago",
            ],
            "min_score": 0.78,
            "lines": [
                {
                    "id": "linea_nota_cobro_mes_depto",
                    "boxes": [
                        {
                            "role": "mixed",
                            "key": "nota_cobro_mes_depto",
                            "text": "Nota de Cobro Mes ???? Depto. ???",
                        }
                    ],
                },
                {
                    "id": "linea_copropietario",
                    "boxes": [
                        {
                            "role": "fixed",
                            "key": "copropietario_label",
                            "canonical_text": "Copropietario",
                            "required": True,
                            "overwrite_ocr_text": True,
                        },
                        {
                            "role": "variable",
                            "key": "copropietario",
                            "value_type": "string",
                            "required": False,
                            "locator": {
                                "strategy": "nearest_right_or_below",
                                "max_distance": 2,
                            },
                        },
                    ],
                },
                {
                    "id": "linea_fecha_ultimo_pago_tabla",
                    "boxes": [
                        {
                            "role": "fixed",
                            "key": "fecha_ultimo_pago_tabla_label",
                            "canonical_text": "Fecha último Pago",
                            "required": True,
                            "overwrite_ocr_text": True,
                        },
                        {
                            "role": "variable",
                            "key": "fecha_ultimo_pago_tabla",
                            "value_type": "date",
                            "required": False,
                            "locator": {
                                "strategy": "nearest_right_or_below",
                                "max_distance": 2,
                            },
                        },
                    ],
                },
                {
                    "id": "linea_alicuota_total",
                    "boxes": [
                        {
                            "role": "fixed",
                            "key": "alicuota_total_label",
                            "canonical_text": "Alícuota Total",
                            "required": True,
                            "overwrite_ocr_text": True,
                        },
                        {
                            "role": "variable",
                            "key": "alicuota_total",
                            "value_type": "string",
                            "required": False,
                            "locator": {
                                "strategy": "nearest_right_or_below",
                                "max_distance": 2,
                            },
                        },
                    ],
                },
                {
                    "id": "linea_monto_ultimo_pago",
                    "boxes": [
                        {
                            "role": "fixed",
                            "key": "monto_ultimo_pago_label",
                            "canonical_text": "Monto último Pago",
                            "required": True,
                            "overwrite_ocr_text": True,
                        },
                        {
                            "role": "variable",
                            "key": "monto_ultimo_pago",
                            "value_type": "string",
                            "required": False,
                            "locator": {
                                "strategy": "nearest_right_or_below",
                                "max_distance": 2,
                            },
                        },
                    ],
                },
                {
                    "id": "linea_gasto_comun_prorratear",
                    "boxes": [
                        {
                            "role": "fixed",
                            "key": "gasto_comun_a_prorratear_label",
                            "canonical_text": "Gasto común a Prorratear",
                            "required": True,
                            "overwrite_ocr_text": True,
                        },
                        {
                            "role": "variable",
                            "key": "gasto_comun_a_prorratear",
                            "value_type": "string",
                            "required": False,
                            "locator": {
                                "strategy": "nearest_right_or_below",
                                "max_distance": 2,
                            },
                        },
                    ],
                },
                {
                    "id": "linea_folio_ultimo_pago",
                    "boxes": [
                        {
                            "role": "fixed",
                            "key": "folio_ultimo_pago_label",
                            "canonical_text": "Folio último Pago",
                            "required": True,
                            "overwrite_ocr_text": True,
                        },
                        {
                            "role": "variable",
                            "key": "folio_ultimo_pago",
                            "value_type": "string",
                            "required": False,
                            "locator": {
                                "strategy": "nearest_right_or_below",
                                "max_distance": 2,
                            },
                        },
                    ],
                },
            ],
        },
    ],
}
BUILTIN_TEMPLATES: dict[str, dict[str, Any]] = {DEFAULT_TEMPLATE["template_id"]: DEFAULT_TEMPLATE}


def get_ocr() -> PaddleOCR:
    """Create and cache the PaddleOCR instance used by all requests."""
    global _OCR_INSTANCE
    if _OCR_INSTANCE is None:
        _OCR_INSTANCE = PaddleOCR(use_angle_cls=True, lang=os.getenv("OCR_LANG", "es"))
    return _OCR_INSTANCE


def _extract_page_lines(ocr_result: Any) -> list[dict[str, Any]]:
    """Transform PaddleOCR output into a normalized list of OCR line objects."""
    lines: list[dict[str, Any]] = []
    for block in ocr_result or []:
        for item in block or []:
            box = item[0]
            text = item[1][0]
            confidence = float(item[1][1])
            lines.append(
                {
                    "text": text,
                    "confidence": confidence,
                    "box": box,
                }
            )
    return lines


def _normalize_text(text: str, matching_cfg: dict[str, Any]) -> str:
    """Normalize OCR/template text according to matching settings before fuzzy comparison."""
    normalized = text
    if matching_cfg.get("normalize_accents", True):
        normalized = _remove_accents(normalized)
    if matching_cfg.get("ignore_case", True):
        normalized = normalized.lower()
    if matching_cfg.get("collapse_whitespace", True):
        normalized = " ".join(normalized.split())
    return normalized.strip()


def _similarity(left: str, right: str) -> float:
    """Return string similarity ratio in the [0.0, 1.0] range using SequenceMatcher."""
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _remove_accents(text: str) -> str:
    """Remove accents/diacritics by filtering Unicode combining marks."""
    return "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))


def _most_frequent_page(pages: list[int]) -> int | None:
    """Return the most frequent page number from a list, or None if empty."""
    if not pages:
        return None
    return max(set(pages), key=pages.count)


def _box_center(box: Any) -> tuple[float | None, float | None]:
    """Return center (x, y) for a quadrilateral-like OCR box, or (None, None) if invalid."""
    if not isinstance(box, list) or not box:
        return None, None
    coordinates: list[tuple[float, float]] = []
    for point in box:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        x, y = point
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            coordinates.append((float(x), float(y)))
    if not coordinates:
        return None, None
    x_center = sum(point[0] for point in coordinates) / len(coordinates)
    y_center = sum(point[1] for point in coordinates) / len(coordinates)
    return x_center, y_center


def _flatten_ocr_lines(ocr_payload: dict[str, Any], matching_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten OCR pages/lines into searchable line records with normalized text and coordinates."""
    flattened: list[dict[str, Any]] = []
    for page in ocr_payload.get("pages", []):
        page_number = int(page.get("page", 0))
        for line_index, line in enumerate(page.get("lines", [])):
            text = str(line.get("text", "")).strip()
            center_x, center_y = _box_center(line.get("box"))
            flattened.append(
                {
                    "page": page_number,
                    "line_index": line_index,
                    "text": text,
                    "normalized_text": _normalize_text(text, matching_cfg),
                    "confidence": float(line.get("confidence", 0.0)),
                    "box": line.get("box"),
                    "center_x": center_x,
                    "center_y": center_y,
                    "used_variable": False,
                }
            )
    return flattened


def _find_best_fixed_match(
    canonical_text: str,
    flattened_lines: list[dict[str, Any]],
    matching_cfg: dict[str, Any],
    threshold: float,
    page_hint: int | None = None,
) -> dict[str, Any] | None:
    """Find the best OCR line for a fixed canonical label above a similarity threshold."""
    normalized_canonical = _normalize_text(canonical_text, matching_cfg)
    best_line: dict[str, Any] | None = None
    best_score = 0.0
    for line in flattened_lines:
        if page_hint is not None and line["page"] != page_hint:
            continue
        score = _similarity(normalized_canonical, line["normalized_text"])
        if score >= threshold and score > best_score:
            best_score = score
            best_line = line
    if best_line is None:
        return None
    return {"line": best_line, "score": best_score}


def _calculate_candidate_priority(
    anchor_line: dict[str, Any], candidate_line: dict[str, Any], strategy: str, max_distance: int
) -> tuple[int, float] | None:
    """Return candidate priority tuple for variable extraction or None if candidate is invalid."""
    if anchor_line["page"] != candidate_line["page"]:
        return None

    line_delta = candidate_line["line_index"] - anchor_line["line_index"]
    anchor_x = anchor_line["center_x"]
    anchor_y = anchor_line["center_y"]
    candidate_x = candidate_line["center_x"]
    candidate_y = candidate_line["center_y"]

    if strategy == "same_line_right":
        if line_delta != 0:
            return None
        if anchor_x is not None and candidate_x is not None and candidate_x <= anchor_x:
            return None
        return 0, abs((candidate_x or 0.0) - (anchor_x or 0.0))

    if strategy == "below":
        if line_delta <= 0 or line_delta > max_distance:
            return None
        return 1, float(line_delta)

    if line_delta == 0:
        if anchor_x is not None and candidate_x is not None and candidate_x <= anchor_x:
            return None
        return 0, abs((candidate_x or 0.0) - (anchor_x or 0.0))

    if line_delta <= 0 or line_delta > max_distance:
        return None

    if anchor_y is not None and candidate_y is not None:
        return 1, abs(candidate_y - anchor_y)
    return 1, float(line_delta)


def _find_variable_value(
    anchor_line: dict[str, Any] | None,
    flattened_lines: list[dict[str, Any]],
    locator: dict[str, Any],
    section_page_hint: int | None,
) -> dict[str, Any] | None:
    """Find a variable-value OCR line using the configured locator strategy and optional anchor line."""
    strategy = str(locator.get("strategy", "nearest_right_or_below"))
    max_distance = int(locator.get("max_distance", 2))

    if anchor_line is None:
        for line in flattened_lines:
            if section_page_hint is not None and line["page"] != section_page_hint:
                continue
            if not line["used_variable"] and line["text"]:
                return line
        return None

    candidates: list[tuple[tuple[int, float], dict[str, Any]]] = []
    for line in flattened_lines:
        if line["used_variable"] or not line["text"]:
            continue
        if line["page"] != anchor_line["page"]:
            continue
        if line is anchor_line:
            continue
        cost = _calculate_candidate_priority(anchor_line, line, strategy, max_distance)
        if cost is None:
            continue
        candidates.append((cost, line))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0][0], item[0][1], -item[1]["confidence"]))
    return candidates[0][1]


def _find_mixed_value(
    mixed_text: str,
    flattened_lines: list[dict[str, Any]],
    matching_cfg: dict[str, Any],
    page_hint: int | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Extract mixed-line variable value using '?' placeholders in the template text."""
    normalized_template = _normalize_text(mixed_text, matching_cfg)
    escaped_template = re.escape(normalized_template)
    mixed_pattern = re.sub(r"(\\\?)+", "(.+?)", escaped_template)
    matcher = re.compile(f"^{mixed_pattern}$")

    best_line: dict[str, Any] | None = None
    best_re_match: re.Match[str] | None = None
    best_score = -1.0
    for line in flattened_lines:
        if line["used_variable"] or not line["text"]:
            continue
        if page_hint is not None and line["page"] != page_hint:
            continue
        match = matcher.match(line["normalized_text"])
        if match is None:
            continue
        candidate_score = _similarity(normalized_template.replace("?", ""), line["normalized_text"])
        if candidate_score > best_score:
            best_score = candidate_score
            best_line = line
            best_re_match = match

    if best_line is None or best_re_match is None:
        return None, None

    if best_re_match.lastindex and best_re_match.lastindex > 0:
        start_index = best_re_match.start(1)
        end_index = best_re_match.end(best_re_match.lastindex)
        extracted = best_line["normalized_text"][start_index:end_index].strip()
    else:
        extracted = best_line["normalized_text"].strip()

    return extracted or None, best_line


def _section_anchor_score(section: dict[str, Any], flattened_lines: list[dict[str, Any]], matching_cfg: dict[str, Any]) -> tuple[float, int | None]:
    """Return section anchor average score and the most frequent matched page as hint."""
    anchors = section.get("anchors") or section.get("match", {}).get("anchors") or []
    if not anchors:
        return 1.0, None

    min_threshold = float(section.get("min_score") or section.get("match", {}).get("min_score") or matching_cfg.get("default_fuzzy_threshold", 0.82))
    scores: list[float] = []
    pages: list[int] = []

    for anchor in anchors:
        match = _find_best_fixed_match(str(anchor), flattened_lines, matching_cfg, threshold=min_threshold, page_hint=None)
        if match:
            scores.append(float(match["score"]))
            pages.append(int(match["line"]["page"]))
        else:
            scores.append(0.0)

    if not scores:
        return 0.0, None

    section_score = sum(scores) / len(scores)
    page_hint = _most_frequent_page(pages)
    return section_score, page_hint


def _apply_template(ocr_payload: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """Transform raw OCR payload into section-based structured output using a template."""
    matching_cfg = template.get("matching", {})
    output_cfg = template.get("output", {})
    default_threshold = float(matching_cfg.get("default_fuzzy_threshold", 0.82))
    flattened_lines = _flatten_ocr_lines(ocr_payload, matching_cfg)

    sections_output: dict[str, Any] = {}
    sections_meta: dict[str, Any] = {}

    for section in template.get("sections", []):
        section_id = str(section.get("id", "section"))
        section_score, section_page_hint = _section_anchor_score(section, flattened_lines, matching_cfg)
        section_min_score = float(section.get("min_score") or section.get("match", {}).get("min_score") or default_threshold)
        is_section_match = section_score >= section_min_score

        fields: dict[str, Any] = {}
        line_results: dict[str, Any] = {}

        if is_section_match:
            for line_template in section.get("lines", []):
                fixed_matches: dict[int, dict[str, Any]] = {}

                for box_index, box in enumerate(line_template.get("boxes", [])):
                    if str(box.get("role", "")).lower() != "fixed":
                        continue
                    canonical_text = str(box.get("canonical_text") or box.get("text") or "").strip()
                    if not canonical_text:
                        continue
                    threshold = float(box.get("fuzzy_threshold", default_threshold))
                    match = _find_best_fixed_match(canonical_text, flattened_lines, matching_cfg, threshold=threshold, page_hint=section_page_hint)
                    if match is None:
                        continue
                    fixed_matches[box_index] = {
                            "box": box,
                            "line": match["line"],
                            "score": match["score"],
                            "text": canonical_text if box.get("overwrite_ocr_text", True) else match["line"]["text"],
                        }

                for box_index, box in enumerate(line_template.get("boxes", [])):
                    role = str(box.get("role", "")).lower()
                    key = str(box.get("key", "")).strip()
                    if role == "fixed" and key and box.get("include_in_output", False):
                        fixed_match = fixed_matches.get(box_index)
                        fields[key] = fixed_match["text"] if fixed_match else None
                        continue

                    if role == "ignore":
                        canonical_text = str(box.get("canonical_text") or box.get("text") or "").strip()
                        if canonical_text:
                            threshold = float(box.get("fuzzy_threshold", default_threshold))
                            match = _find_best_fixed_match(
                                canonical_text,
                                flattened_lines,
                                matching_cfg,
                                threshold=threshold,
                                page_hint=section_page_hint,
                            )
                            if match is not None:
                                match["line"]["used_variable"] = True
                        continue

                    if role != "variable" or not key:
                        if role != "mixed" or not key:
                            continue
                        mixed_value, mixed_line = _find_mixed_value(
                            mixed_text=str(box.get("text", "")),
                            flattened_lines=flattened_lines,
                            matching_cfg=matching_cfg,
                            page_hint=section_page_hint,
                        )
                        if mixed_line is not None:
                            mixed_line["used_variable"] = True
                        fields[key] = mixed_value
                        line_results[key] = {
                            "value": fields[key],
                            "page": mixed_line["page"] if mixed_line else None,
                            "line_index": mixed_line["line_index"] if mixed_line else None,
                        }
                        continue

                    locator_value = box.get("locator")
                    locator = locator_value if isinstance(locator_value, dict) else {}
                    first_fixed_match = next(iter(fixed_matches.values()), None)
                    anchor_line = first_fixed_match["line"] if first_fixed_match else None
                    variable_line = _find_variable_value(anchor_line, flattened_lines, locator, section_page_hint)
                    if variable_line is not None:
                        variable_line["used_variable"] = True
                    fields[key] = variable_line["text"] if variable_line else None
                    line_results[key] = {
                        "value": fields[key],
                        "page": variable_line["page"] if variable_line else None,
                        "line_index": variable_line["line_index"] if variable_line else None,
                    }

        sections_output[section_id] = fields
        sections_meta[section_id] = {
            "matched": is_section_match,
            "score": round(section_score, 4),
            "line_results": line_results,
        }

    response: dict[str, Any] = {
        "template_id": template.get("template_id"),
        "document_type": template.get("document_type"),
        "sections": sections_output,
        "meta": {
            "page_count": ocr_payload.get("page_count"),
            "matched_sections": sections_meta,
        },
    }

    if output_cfg.get("include_raw_text", False):
        response["text"] = ocr_payload.get("text", "")
    if output_cfg.get("include_boxes", False):
        response["pages"] = ocr_payload.get("pages", [])
    elif output_cfg.get("include_confidence", False):
        response["pages"] = [
            {
                "page": page.get("page"),
                "lines": [{"text": line.get("text"), "confidence": line.get("confidence")} for line in page.get("lines", [])],
            }
            for page in ocr_payload.get("pages", [])
        ]

    return response


def _slugify_key(value: str, default_key: str) -> str:
    """Convert text into a safe key using lowercase alnum and underscores, with fallback."""
    normalized = _remove_accents(value).lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return normalized or default_key


def _coerce_template_schema(template: dict[str, Any]) -> dict[str, Any]:
    """Coerce shorthand template schemas into the canonical sections/lines/boxes schema."""
    if "sections" in template:
        return template
    if "section" not in template:
        return template

    section_source = template.get("section")
    section_items = section_source if isinstance(section_source, list) else [section_source]
    coerced_sections: list[dict[str, Any]] = []

    for section_index, section_item in enumerate(section_items, start=1):
        if not isinstance(section_item, dict):
            continue
        lines_source = section_item.get("lines", section_item.get("line", []))
        if isinstance(lines_source, dict):
            lines_source = [lines_source]
        if not isinstance(lines_source, list):
            lines_source = []

        coerced_lines: list[dict[str, Any]] = []
        anchors: list[str] = []

        for line_index, line_item in enumerate(lines_source, start=1):
            if not isinstance(line_item, dict):
                continue
            if "boxes" in line_item and isinstance(line_item.get("boxes"), list):
                coerced_lines.append(line_item)
                continue

            text = str(line_item.get("text", "")).strip()
            role_type = str(line_item.get("type", "fixed")).strip().lower()
            key = str(line_item.get("key", "")).strip()

            if role_type == "fixed":
                anchors.append(text)
                coerced_lines.append(
                    {
                        "id": f"line_{line_index}",
                        "boxes": [{"role": "fixed", "key": key or f"fixed_{line_index}", "canonical_text": text, "overwrite_ocr_text": True}],
                    }
                )
                continue

            if role_type == "mixed":
                static_label = text.split("?")[0].strip(" :.-")
                mixed_key = key or _slugify_key(static_label or f"mixed_{line_index}", f"mixed_{line_index}")
                anchors.append(static_label or text)
                coerced_lines.append(
                    {
                        "id": f"line_{line_index}",
                        "boxes": [{"role": "mixed", "key": mixed_key, "text": text}],
                    }
                )
                continue

            if role_type == "ignore":
                coerced_lines.append(
                    {
                        "id": f"line_{line_index}",
                        "boxes": [{"role": "ignore", "key": key or f"ignore_{line_index}", "canonical_text": text}],
                    }
                )
                continue

            variable_key = key or _slugify_key(text, f"variable_{line_index}")
            coerced_lines.append(
                {
                    "id": f"line_{line_index}",
                    "boxes": [{"role": "variable", "key": variable_key, "locator": {"strategy": "nearest_right_or_below", "max_distance": 2}}],
                }
            )

        coerced_sections.append(
            {
                "id": str(section_item.get("id", f"section_{section_index}")),
                "name": section_item.get("name", f"section_{section_index}"),
                "anchors": section_item.get("anchors", anchors),
                "min_score": section_item.get("min_score", template.get("matching", {}).get("default_fuzzy_threshold", 0.82)),
                "lines": coerced_lines,
            }
        )

    coerced_template = dict(template)
    coerced_template.pop("section", None)
    coerced_template["sections"] = coerced_sections
    return coerced_template


def _validate_template(template: dict[str, Any]) -> dict[str, Any]:
    """Validate minimum template contract: object with a non-empty sections array."""
    if not isinstance(template, dict):
        raise HTTPException(status_code=400, detail="Invalid template: expected an object")
    coerced_template = _coerce_template_schema(template)
    sections = coerced_template.get("sections")
    if not isinstance(sections, list) or not sections:
        raise HTTPException(status_code=400, detail="Invalid template: 'sections' must be a non-empty array")
    return coerced_template


def _resolve_template(template_id: str | None, template_json: str | None) -> dict[str, Any] | None:
    if template_json:
        try:
            template = json.loads(template_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid template JSON at position {exc.pos}: {exc.msg}") from exc
        return _validate_template(template)

    if template_id:
        builtin = BUILTIN_TEMPLATES.get(template_id)
        if builtin is None:
            raise HTTPException(status_code=404, detail=f"Unknown template_id '{template_id}'")
        return _validate_template(builtin)

    return None


def _is_pdf_bytes(pdf_bytes: bytes) -> bool:
    return pdf_bytes.startswith(b"%PDF")


def _is_public_http_url(pdf_url: str) -> bool:
    parsed = urlparse(pdf_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.hostname.lower() == "localhost":
        return False

    try:
        resolved = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False

    for entry in resolved:
        raw_ip = entry[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False

    return True


def _validate_local_pdf_path(local_path: str) -> Path:
    requested_path = Path(local_path.strip()).expanduser()

    if (
        requested_path.is_absolute()
        and len(requested_path.parts) > 1
        and requested_path.parts[1] == HOMEASSISTANT_LOCAL_ALIAS
    ):
        requested_path = Path("/config").joinpath(*requested_path.parts[2:])

    if not requested_path.is_absolute():
        raise HTTPException(status_code=400, detail="The local path must be absolute")

    if not RESOLVED_LOCAL_BASE_DIRS:
        raise HTTPException(status_code=500, detail="No allowed local paths are configured")

    for base in RESOLVED_LOCAL_BASE_DIRS:
        try:
            relative_path = requested_path.relative_to(base)
        except ValueError:
            continue

        try:
            resolved_path = (base / relative_path).resolve(strict=True)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Local file not found") from exc

        if not resolved_path.is_relative_to(base):
            raise HTTPException(status_code=403, detail="The local path is not allowed")
        if not resolved_path.is_file():
            raise HTTPException(status_code=400, detail="The local path does not point to a file")
        if resolved_path.suffix.lower() != ".pdf":
            raise HTTPException(status_code=400, detail="The local path must point to a PDF file")
        return resolved_path

    raise HTTPException(status_code=403, detail="The local path is not allowed")


async def _fetch_pdf_from_url(pdf_url: str) -> bytes:
    if not _is_public_http_url(pdf_url):
        raise HTTPException(status_code=400, detail="The URL is not valid or safe to download")

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(pdf_url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"Could not download the PDF: {exc}") from exc

    pdf_bytes = response.content
    if not pdf_bytes or not _is_pdf_bytes(pdf_bytes):
        raise HTTPException(status_code=400, detail="The URL did not return a valid PDF")

    return pdf_bytes


def _load_local_pdf(local_path: str) -> bytes:
    validated_path = _validate_local_pdf_path(local_path)
    try:
        return validated_path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not read the local PDF: {exc}") from exc


def _process_pdf_bytes(pdf_bytes: bytes, template: dict[str, Any] | None = None) -> dict[str, Any]:
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="No PDF was received")

    if not _is_pdf_bytes(pdf_bytes):
        raise HTTPException(status_code=400, detail="Invalid content: it does not look like a PDF")

    try:
        images = convert_from_bytes(pdf_bytes)
    except Exception as exc:  # pragma: no cover
        logger.exception("PDF processing failed while converting bytes to images")
        raise HTTPException(status_code=400, detail=f"Could not process the PDF: {exc}") from exc

    ocr = get_ocr()
    pages: list[dict[str, Any]] = []

    for index, image in enumerate(images, start=1):
        bgr_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        result = ocr.ocr(bgr_image, cls=True)
        lines = _extract_page_lines(result)
        pages.append(
            {
                "page": index,
                "lines": lines,
                "text": "\n".join(line["text"] for line in lines),
            }
        )

    raw_payload = {
        "page_count": len(pages),
        "pages": pages,
        "text": "\n\n".join(page["text"] for page in pages if page["text"]),
    }

    if template is None:
        return raw_payload

    return _apply_template(raw_payload, template)


@app.get("/", response_class=HTMLResponse)
def web_ui() -> HTMLResponse:
    return HTMLResponse(content=WEB_UI_HTML)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/templates")
def list_templates() -> dict[str, Any]:
    return {
        "templates": [
            {
                "template_id": template["template_id"],
                "document_type": template.get("document_type"),
                "section_count": len(template.get("sections", [])),
            }
            for template in BUILTIN_TEMPLATES.values()
        ]
    }


@app.post("/ocr")
async def handle_ocr_request(
    request: Request,
    file: UploadFile | None = File(default=None),
    template_id: str | None = None,
    template_json: str | None = None,
) -> dict[str, Any]:
    try:
        template = _resolve_template(template_id=template_id, template_json=template_json)
        if file is not None:
            if not file.filename or not file.filename.lower().endswith(".pdf"):
                raise HTTPException(status_code=400, detail="The uploaded file must be a PDF")
            pdf_bytes = await file.read()
        else:
            pdf_bytes = await request.body()

        return _process_pdf_bytes(pdf_bytes, template=template)
    except HTTPException as exc:
        logger.warning("Request to /ocr failed: %s", exc.detail)
        raise
    except Exception:
        logger.exception("Unhandled error in /ocr")
        raise


@app.post("/ocr/source")
async def handle_ocr_source_request(
    source_type: str = Form(...),
    source_value: str = Form(...),
    template_id: str | None = Form(default=None),
    template_json: str | None = Form(default=None),
) -> dict[str, Any]:
    normalized_source = source_type.strip().lower()
    normalized_value = source_value.strip()

    try:
        template = _resolve_template(template_id=template_id, template_json=template_json)
        if not normalized_value:
            raise HTTPException(status_code=400, detail="You must provide a URL or local path")

        if normalized_source == "url":
            pdf_bytes = await _fetch_pdf_from_url(normalized_value)
        elif normalized_source == "local_path":
            pdf_bytes = _load_local_pdf(normalized_value)
        else:
            raise HTTPException(status_code=400, detail="source_type must be 'url' or 'local_path'")

        return _process_pdf_bytes(pdf_bytes, template=template)
    except HTTPException as exc:
        logger.warning("Request to /ocr/source failed: %s (source_type=%s)", exc.detail, normalized_source)
        raise
    except Exception:
        logger.exception("Unhandled error in /ocr/source")
        raise
