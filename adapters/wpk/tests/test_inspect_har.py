import base64
import json

import pytest

from texas_wpk.inspect_har import describe_body, describe_url, json_shape, summarize_har


def make_har(body, *, url="https://api.example.test/table/state", frames=None):
    entry = {
        "request": {
            "method": "GET",
            "url": url,
            "headers": [{"name": "Authorization", "value": "SECRET_HEADER"}],
            "cookies": [{"name": "session", "value": "SECRET_COOKIE"}],
            "postData": {"text": "SECRET_BODY"},
        },
        "response": {"status": 200, "content": {"text": json.dumps(body)}},
    }
    if frames is not None:
        entry["_webSocketMessages"] = frames
    return {"log": {"entries": [entry]}}


def test_report_preserves_structure_without_values_or_credentials():
    har = make_har(
        {"players": [{"name": "SECRET_NAME", "stack": 12345}], "token": "SECRET_TOKEN"},
        url="https://user:SECRET_PASSWORD@api.example.test/table/12345?token=SECRET_QUERY#SECRET_HASH",
        frames=[{"data": "SECRET_FRAME", "opcode": 1}],
    )
    report = summarize_har(har)
    serialized = json.dumps(report)
    assert "SECRET_" not in serialized
    assert "12345" not in serialized
    record = report["records"][0]
    assert record["url_without_query_values"] == "https://api.example.test/table/{redacted}"
    assert record["query_keys"] == ["token"]
    assert record["response"]["fields"]["$.players[].stack"] == ["number"]
    assert record["websocket_frame_count"] == 1


def test_shapes_merge_array_types_and_bound_depth():
    shape, truncated = json_shape({"data": [None, True, 1, "text"]})
    assert shape["$.data[]"] == ["boolean", "null", "number", "string"]
    assert not truncated
    nested = {"child": {"child": {"child": {"child": {"child": {"child": {"end": 1}}}}}}}
    assert json_shape(nested)[1]


def test_binary_missing_and_base64_bodies_are_distinguished():
    assert describe_body({})["state"] == "not_exported"
    assert describe_body({"text": "not JSON"})["state"] == "not_json_or_invalid"
    text = base64.b64encode(b'{"event": "sample"}').decode()
    result = describe_body({"text": text, "encoding": "base64"})
    assert result["fields"]["$.event"] == ["string"]
    assert "sample" not in json.dumps(result)
    assert describe_body({"text": "bad base64!", "encoding": "base64"})["state"] != "json_shape"
    assert describe_body({"text": "x" * (1024 * 1024 + 1)})["state"] == "body_limit_exceeded"


def test_websocket_endpoint_without_frames_does_not_claim_frames_were_captured():
    har = make_har({}, url="wss://socket.example.test/live?session=SECRET")
    record = summarize_har(har)["records"][0]
    assert record["transport"] == "websocket"
    assert record["websocket_frame_count"] is None
    assert not record["websocket_frames_exported"]


@pytest.mark.parametrize("value", [None, {}, {"log": []}, {"log": {"entries": None}}])
def test_invalid_har_is_rejected(value):
    with pytest.raises(ValueError):
        summarize_har(value)


def test_malformed_entries_and_urls_are_handled_without_echoing_inputs():
    report = summarize_har({"log": {"entries": [None, {}, {"request": [], "response": {}}]}})
    assert report["entries_skipped"] == 3
    assert describe_url("https://example.test:invalid/path?secret=value") == ("{invalid-url}", [])
