"""
Pytest suite for the Gmail inbox poller organ.

Covers the pure decision logic extracted from discovery-engine
``lib/dataflow_core/gmail_inbox/poller.py``:

  - MIME body extraction (text/plain, text/html, attachments, multipart)
  - first-message summary projection
  - empty-thread drop
  - belt-and-braces skip-label drop
  - fail-open behaviour on malformed input
  - the JSON output envelope shape
"""

import base64
import json
import subprocess
import sys

import pytest

from organ import decide, extract_mime_body, parse_message


def b64(text: str) -> str:
    """Encode text the way Gmail encodes body part data (base64url)."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def simple_message(
    msg_id="m1",
    thread_id="t1",
    subject="Hello",
    sender="alice@example.com",
    body="Plain body",
    label_ids=None,
    snippet="snippet",
):
    """Build a minimal raw Gmail message resource with a text/plain body."""
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": label_ids or ["UNREAD", "INBOX"],
        "snippet": snippet,
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@example.com"},
            ],
            "body": {"data": b64(body)},
        },
    }


class TestExtractMimeBody:
    def test_plain_text(self):
        payload = {"mimeType": "text/plain", "body": {"data": b64("hi there")}}
        out = extract_mime_body(payload)
        assert out["body_text"] == "hi there"
        assert out["body_html"] == ""
        assert out["attachments"] == []

    def test_html(self):
        payload = {"mimeType": "text/html", "body": {"data": b64("<p>hi</p>")}}
        out = extract_mime_body(payload)
        assert out["body_html"] == "<p>hi</p>"
        assert out["body_text"] == ""

    def test_multipart_alternative(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("plain")}},
                {"mimeType": "text/html", "body": {"data": b64("<b>rich</b>")}},
            ],
        }
        out = extract_mime_body(payload)
        assert out["body_text"] == "plain"
        assert out["body_html"] == "<b>rich</b>"

    def test_attachment_stub(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("see file")}},
                {
                    "mimeType": "application/pdf",
                    "filename": "statement.pdf",
                    "body": {"attachmentId": "att-1", "size": 12345},
                },
            ],
        }
        out = extract_mime_body(payload)
        assert out["body_text"] == "see file"
        assert len(out["attachments"]) == 1
        att = out["attachments"][0]
        assert att == {
            "filename": "statement.pdf",
            "mime_type": "application/pdf",
            "attachment_id": "att-1",
            "size": 12345,
        }

    def test_text_part_with_filename_is_attachment_not_body(self):
        # A text/plain part that has a filename is an attachment, not the body.
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "notes.txt",
                    "body": {"attachmentId": "att-x", "size": 9},
                }
            ],
        }
        out = extract_mime_body(payload)
        assert out["body_text"] == ""
        assert len(out["attachments"]) == 1

    def test_empty_payload(self):
        assert extract_mime_body({}) == {
            "body_text": "",
            "body_html": "",
            "attachments": [],
        }
        assert extract_mime_body(None) == {
            "body_text": "",
            "body_html": "",
            "attachments": [],
        }

    def test_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": b64("deep")}},
                    ],
                }
            ],
        }
        out = extract_mime_body(payload)
        assert out["body_text"] == "deep"

    def test_bad_base64_is_swallowed(self):
        payload = {"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}}
        out = extract_mime_body(payload)
        # Lenient decode → never raises; either empty or replacement chars.
        assert isinstance(out["body_text"], str)


class TestParseMessage:
    def test_headers_lowercased_and_extracted(self):
        msg = simple_message(subject="Invoice", sender="bob@x.com")
        parsed = parse_message(msg)
        assert parsed["subject"] == "Invoice"
        assert parsed["from"] == "bob@x.com"
        assert parsed["to"] == "me@example.com"
        assert parsed["body_text"] == "Plain body"
        assert parsed["label_ids"] == ["UNREAD", "INBOX"]

    def test_missing_headers_default_blank(self):
        msg = {"id": "z", "threadId": "t", "payload": {}}
        parsed = parse_message(msg)
        assert parsed["subject"] == ""
        assert parsed["from"] == ""
        assert parsed["attachments"] == []

    def test_none_message(self):
        parsed = parse_message(None)
        assert parsed["id"] is None
        assert parsed["body_text"] == ""


class TestDecideKeep:
    def test_kept_thread(self):
        state = {
            "thread_id": "t1",
            "messages": [simple_message(subject="Q3 report")],
            "skip_label_id": None,
        }
        res = decide(state)
        assert res["output"]["keep"] is True
        assert res["output"]["reason"] == "kept"
        thread = res["output"]["thread"]
        assert thread["thread_id"] == "t1"
        assert thread["subject"] == "Q3 report"
        assert thread["from"] == "alice@example.com"
        assert len(thread["messages"]) == 1
        assert res["self_metric"]["message_count"] == 1
        assert res["self_metric"]["decision_path"] == "kept"
        assert res["self_metric"]["confidence"] == 1.0

    def test_summary_from_first_message(self):
        state = {
            "thread_id": "t9",
            "messages": [
                simple_message(msg_id="a", subject="First", sender="first@x.com"),
                simple_message(msg_id="b", subject="Second", sender="second@x.com"),
            ],
            "skip_label_id": None,
        }
        res = decide(state)
        thread = res["output"]["thread"]
        assert thread["subject"] == "First"
        assert thread["from"] == "first@x.com"
        assert len(thread["messages"]) == 2
        assert res["self_metric"]["message_count"] == 2

    def test_attachment_count_rolled_up(self):
        msg = simple_message()
        msg["payload"] = {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "with files"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("body")}},
                {
                    "mimeType": "application/pdf",
                    "filename": "a.pdf",
                    "body": {"attachmentId": "1", "size": 1},
                },
                {
                    "mimeType": "image/png",
                    "filename": "b.png",
                    "body": {"attachmentId": "2", "size": 2},
                },
            ],
        }
        state = {"thread_id": "t", "messages": [msg], "skip_label_id": None}
        res = decide(state)
        assert res["self_metric"]["attachment_count"] == 2
        assert len(res["output"]["thread"]["messages"][0]["attachments"]) == 2


class TestDecideEmpty:
    def test_empty_messages(self):
        state = {"thread_id": "t-empty", "messages": [], "skip_label_id": None}
        res = decide(state)
        assert res["output"]["keep"] is False
        assert res["output"]["reason"] == "empty_thread"
        assert res["output"]["thread"] is None
        assert res["self_metric"]["decision_path"] == "empty_thread"

    def test_missing_messages_key(self):
        state = {"thread_id": "t", "skip_label_id": None}
        res = decide(state)
        assert res["output"]["reason"] == "empty_thread"


class TestDecideSkipLabel:
    def test_skip_label_present_drops_thread(self):
        state = {
            "thread_id": "t-skip",
            "messages": [
                simple_message(label_ids=["UNREAD", "Label_99"]),
            ],
            "skip_label_id": "Label_99",
        }
        res = decide(state)
        assert res["output"]["keep"] is False
        assert res["output"]["reason"] == "skip_label"
        assert res["output"]["thread"] is None
        assert res["self_metric"]["decision_path"] == "skip_label"

    def test_skip_label_on_any_message_drops_thread(self):
        state = {
            "thread_id": "t-skip2",
            "messages": [
                simple_message(msg_id="a", label_ids=["UNREAD"]),
                simple_message(msg_id="b", label_ids=["UNREAD", "Label_99"]),
            ],
            "skip_label_id": "Label_99",
        }
        res = decide(state)
        assert res["output"]["keep"] is False
        assert res["output"]["reason"] == "skip_label"

    def test_skip_label_absent_keeps_thread(self):
        state = {
            "thread_id": "t-keep",
            "messages": [simple_message(label_ids=["UNREAD", "INBOX"])],
            "skip_label_id": "Label_99",
        }
        res = decide(state)
        assert res["output"]["keep"] is True

    def test_null_skip_label_disables_filter(self):
        # Even if a thread carried the skip label, a null skip_label_id means
        # the caller didn't resolve it — the organ must not invent the filter.
        state = {
            "thread_id": "t",
            "messages": [simple_message(label_ids=["UNREAD", "Label_99"])],
            "skip_label_id": None,
        }
        res = decide(state)
        assert res["output"]["keep"] is True
        assert res["output"]["reason"] == "kept"

    def test_empty_thread_beats_skip_label(self):
        # Empty guard runs first; an empty thread is empty regardless of label.
        state = {"thread_id": "t", "messages": [], "skip_label_id": "Label_99"}
        res = decide(state)
        assert res["output"]["reason"] == "empty_thread"


class TestFailOpen:
    def test_non_dict_state_fails_open(self):
        res = decide("not a dict")  # type: ignore[arg-type]
        assert res["output"]["keep"] is True
        assert res["self_metric"]["decision_path"] == "error_fallback"
        assert res["self_metric"]["confidence"] == 0.0

    def test_context_is_optional(self):
        state = {"thread_id": "t", "messages": [simple_message()], "skip_label_id": None}
        res = decide(state, None)
        assert res["output"]["keep"] is True


class TestEnvelopeShape:
    def test_output_envelope_keys(self):
        state = {"thread_id": "t", "messages": [simple_message()], "skip_label_id": None}
        res = decide(state)
        assert set(res.keys()) == {"output", "rationale", "self_metric"}
        assert set(res["output"].keys()) == {"keep", "reason", "thread"}
        assert "confidence" in res["self_metric"]
        assert "decision_path" in res["self_metric"]
        assert isinstance(res["rationale"], str)

    def test_json_serialisable(self):
        state = {"thread_id": "t", "messages": [simple_message()], "skip_label_id": None}
        res = decide(state)
        # Must round-trip through JSON unchanged (orchestrator transport).
        assert json.loads(json.dumps(res)) == res


class TestCli:
    def test_cli_runs_on_stdin(self):
        state = {
            "state": {
                "thread_id": "cli-t",
                "messages": [simple_message(subject="CLI")],
                "skip_label_id": None,
            }
        }
        proc = subprocess.run(
            [sys.executable, "organ.py"],
            input=json.dumps(state),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["output"]["keep"] is True
        assert out["output"]["thread"]["subject"] == "CLI"

    def test_cli_rejects_garbage(self):
        proc = subprocess.run(
            [sys.executable, "organ.py"],
            input="not json",
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
