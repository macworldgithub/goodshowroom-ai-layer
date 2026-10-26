"""Offline public OpenAPI export. Run with --check to detect schema drift."""

import argparse
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from goodshowroom_ai.api_contracts import (
    COMMIT_ERROR_CODES,
    ERROR_CATALOG,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_REQUEST_BYTES,
    MAX_SAFE_INTEGER,
    MAX_WIRE_BYTES,
    PUBLIC_MODELS,
)

ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = ROOT / "contracts" / "openapi.json"


def public_openapi() -> dict:
    _, definitions = models_json_schema(
        [(model, "validation") for model in PUBLIC_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    schemas = definitions["$defs"]

    def wire_rules(node):
        if isinstance(node, dict):
            if node.get("type") == "integer":
                node["maximum"] = min(
                    node.get("maximum", MAX_SAFE_INTEGER), MAX_SAFE_INTEGER
                )
            if node.get("type") == "object" and "properties" in node:
                # Wire output contains explicit nulls/arrays/defaults. The sole
                # omitted optional field is error commit_state when inapplicable.
                node["required"] = [
                    key for key in node["properties"] if key != "commit_state"
                ]
            if node.get("format") == "uuid":
                node["pattern"] = (
                    r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
                )
            if node.get("format") == "date-time":
                node["pattern"] = (
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
                )
            for value in node.values():
                wire_rules(value)
        elif isinstance(node, list):
            for value in node:
                wire_rules(value)

    wire_rules(schemas)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Good Showroom public contract foundation",
            "version": "1.0.0",
        },
        "paths": {},
        "components": {"schemas": schemas},
        "x-contract-stage": "Q-01 schemas only; no active business endpoints",
        "x-contract-limits": {
            "request_bytes": MAX_REQUEST_BYTES,
            "response_bytes": MAX_WIRE_BYTES,
            "depth": MAX_JSON_DEPTH,
            "nodes": MAX_JSON_NODES,
            "max_integer": MAX_SAFE_INTEGER,
        },
        "x-error-catalog": {
            code.value: {
                "status": status,
                "message": message,
                "retryable": retryable,
                "allows_commit_state": code in COMMIT_ERROR_CODES,
            }
            for code, (status, message, retryable) in ERROR_CATALOG.items()
        },
    }


def export_text() -> str:
    return (
        json.dumps(public_openapi(), ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = export_text()
    if args.check:
        if (
            not OPENAPI_PATH.is_file()
            or OPENAPI_PATH.read_text(encoding="utf-8") != expected
        ):
            raise SystemExit("Public OpenAPI export is stale. Regenerate contracts.")
        print("Public OpenAPI export matches backend schemas.")
    else:
        OPENAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
        OPENAPI_PATH.write_text(expected, encoding="utf-8", newline="\n")
        print("Public OpenAPI export generated.")


if __name__ == "__main__":
    main()
