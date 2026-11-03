"""Inspect a local HAR without sending requests or emitting response values."""

import argparse
import base64
import binascii
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

MAX_HAR_BYTES = 64 * 1024 * 1024
MAX_BODY_CHARS = 1024 * 1024
MAX_FIELDS = 200
MAX_ENTRIES = 5000
LABEL = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.-]{0,47}\Z")
HEX_ID = re.compile(r"[0-9a-fA-F]{16,}\Z")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT"}


def safe_label(value: str) -> str:
    """Mask common IDs; arbitrary route names still require human review."""
    if not LABEL.fullmatch(value) or HEX_ID.fullmatch(value):
        return "{redacted}"
    if len(value) >= 24 and any(c.isdigit() for c in value):
        return "{redacted}"
    return value


def describe_url(raw: object) -> tuple[str, list[str]]:
    if not isinstance(raw, str):
        return "{invalid-url}", []
    try:
        url = urlsplit(raw)
        if url.scheme not in {"http", "https", "ws", "wss"} or not url.hostname:
            return "{invalid-url}", []
        host = url.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{url.port}" if url.port else ""
        path = "/".join(safe_label(unquote(part)) if part else "" for part in url.path.split("/"))
        keys = sorted({safe_label(key) for key, _ in parse_qsl(url.query, keep_blank_values=True)})
        return f"{url.scheme}://{host}{port}{path}", keys
    except ValueError:
        return "{invalid-url}", []


def json_shape(value: object) -> tuple[dict[str, list[str]], bool]:
    fields: dict[str, set[str]] = {}
    truncated = False

    def visit(item: object, path: str, depth: int) -> None:
        nonlocal truncated
        if depth > 6 or (path not in fields and len(fields) >= MAX_FIELDS):
            truncated = True
            return
        kind = (
            "null"
            if item is None
            else "boolean"
            if isinstance(item, bool)
            else "object"
            if isinstance(item, dict)
            else "array"
            if isinstance(item, list)
            else "string"
            if isinstance(item, str)
            else "number"
        )
        fields.setdefault(path, set()).add(kind)
        if isinstance(item, dict):
            for index, (key, child) in enumerate(item.items()):
                if index >= MAX_FIELDS:
                    truncated = True
                    break
                visit(child, f"{path}.{safe_label(key)}", depth + 1)
        elif isinstance(item, list):
            if len(item) > 20:
                truncated = True
            for child in item[:20]:
                visit(child, f"{path}[]", depth + 1)

    visit(value, "$", 0)
    return {path: sorted(types) for path, types in sorted(fields.items())}, truncated


def describe_body(content: dict) -> dict:
    text = content.get("text")
    if not isinstance(text, str):
        return {"state": "not_exported"}
    if len(text) > MAX_BODY_CHARS:
        return {"state": "body_limit_exceeded"}
    encoding = content.get("encoding")
    if encoding not in (None, "base64"):
        return {"state": "unsupported_encoding"}
    try:
        if encoding == "base64":
            text = base64.b64decode(text, validate=True).decode("utf-8")
        decoded = json.loads(text)
        shape, truncated = json_shape(decoded)
        return {"state": "json_shape", "fields": shape, "truncated": truncated}
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        return {"state": "not_json_or_invalid"}


def summarize_har(har: object) -> dict:
    if not isinstance(har, dict) or not isinstance(har.get("log"), dict):
        raise ValueError("Expected a HAR object with log.entries")
    entries = har["log"].get("entries")
    if not isinstance(entries, list):
        raise ValueError("Expected a HAR object with log.entries")
    records = []
    skipped = 0
    for index, entry in enumerate(entries[:MAX_ENTRIES]):
        if not isinstance(entry, dict):
            skipped += 1
            continue
        request, response = entry.get("request"), entry.get("response")
        if not isinstance(request, dict) or not isinstance(response, dict):
            skipped += 1
            continue
        url, query_keys = describe_url(request.get("url"))
        method = request.get("method")
        method = method if isinstance(method, str) and method in METHODS else "UNKNOWN"
        status = response.get("status")
        status = status if type(status) is int and 0 <= status <= 599 else None
        content = response.get("content")
        content = content if isinstance(content, dict) else {}
        websocket = (
            url.startswith(("ws://", "wss://"))
            or entry.get("_resourceType") == "websocket"
            or status == 101
        )
        frames = entry.get("_webSocketMessages")
        records.append(
            {
                "entry_index": index,
                "method": method,
                "url_without_query_values": url,
                "query_keys": query_keys,
                "http_status": status,
                "transport": "websocket" if websocket else "http",
                "response": describe_body(content),
                "websocket_frames_exported": isinstance(frames, list),
                "websocket_frame_count": len(frames) if isinstance(frames, list) else None,
            }
        )
    return {
        "schema_version": "1.0",
        "source": "local_har",
        "records": records,
        "entries_total": len(entries),
        "entries_skipped": skipped,
        "entries_truncated": len(entries) > MAX_ENTRIES,
        "notice": (
            "Headers, cookies, request bodies, query values, response values and WebSocket "
            "payloads are omitted. Review route names and JSON keys before sharing. "
            "A HAR may omit WebSocket frames; absence does not prove there is no WebSocket."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("har", type=Path, help="Local HAR exported by your browser")
    parser.add_argument("--out", type=Path, help="Write a structural report instead of stdout")
    args = parser.parse_args()
    try:
        if args.out and args.out.resolve() == args.har.resolve():
            raise ValueError("Output must not overwrite the source HAR")
        with args.har.open("rb") as stream:
            data = stream.read(MAX_HAR_BYTES + 1)
        if len(data) > MAX_HAR_BYTES:
            raise ValueError("HAR exceeds the 64 MiB input limit")
        try:
            report = summarize_har(json.loads(data))
        except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
            raise ValueError("Invalid HAR JSON; no source content was printed") from error
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, ValueError) as error:
        parser.exit(2, f"Cannot inspect HAR: {error}\n")


if __name__ == "__main__":
    main()
