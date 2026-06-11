#!/usr/bin/env python3
"""
Gmail Inbox Poller Organ — extracted decision logic from discovery-engine.

A pure decider for the Gmail unread-thread ingestion pipeline. Given a single
Gmail thread that the CALLER has already fetched from the Gmail API (raw message
resources) plus a pre-resolved skip-label id, the organ:

  1. parses each raw message into a flat dict (pure MIME walk — text/plain,
     text/html, attachment stubs),
  2. decides whether the thread should be KEPT or SKIPPED (the ``dfa:skip``
     privacy control + empty-thread guard),
  3. emits the normalised thread dict the platform consumes.

Provenance (discovery-engine ``lib/dataflow_core/gmail_inbox/poller.py``):
  - The I/O — listing unread threads, fetching each thread, resolving a label
    *name* to its label *id* — stays in the CALLER. Those are the only steps
    that touch ``service`` (the Gmail API client). They are NOT part of this
    organ, which is pure by construction.
  - What IS extracted is every decision the original made AFTER the bytes were
    on hand: the MIME-body walk (``extract_mime_body`` / ``parse_message``),
    the belt-and-braces skip-label drop (poller.py lines 178-181), the
    empty-thread drop (line 172), and the first-message summary projection
    (lines 183-192).

Contract:
  INPUT state: {
    "thread_id": str | null,            # Gmail thread id (echoed through)
    "messages": [ <raw gmail message resource>, ... ],
                                        # each: {id, threadId, labelIds, snippet,
                                        #        internalDate, payload: {headers, ...}}
    "skip_label_id": str | null         # pre-resolved id of the skip label.
                                        # null disables the skip filter.
  }

  OUTPUT: {
    "output": {
      "keep": bool,
      "reason": str,   # "kept" | "skip_label" | "empty_thread"
      "thread": {      # null unless keep is True
        "thread_id": str | null,
        "messages": [ <parsed message dict>, ... ],
        "subject": str,
        "from": str,
        "snippet": str
      } | null
    },
    "rationale": "...",
    "self_metric": {
      "confidence": float,        # 1.0 when inputs well-formed, < 1.0 on error
      "decision_path": str,       # which branch decided
      "message_count": int,       # parsed messages in the thread
      "attachment_count": int     # attachment stubs across the thread
    }
  }

The organ is pure:
  - Takes all inputs via JSON (caller pre-fetches everything from Gmail).
  - Makes no DB / network / Google-client calls.
  - Never raises on bad input (fail-open → keep, mirroring the original
    pipeline which kept anything it managed to fetch).
"""

from __future__ import annotations

import base64
import json
import os
import sys


def _decode_part(data: str) -> str:
    """Decode a base64url Gmail body part to text (lossy on bad bytes)."""
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        # A malformed base64 part must not sink the whole thread.
        return ""


def extract_mime_body(payload: dict) -> dict:
    """Recursively pull text/plain, text/html and attachment stubs from a
    Gmail message payload.

    Returns ``{body_text, body_html, attachments}`` where each attachment is
    ``{filename, mime_type, attachment_id, size}``.
    """
    result = {"body_text": "", "body_html": "", "attachments": []}

    def _walk(part):
        mime = part.get("mimeType", "")
        filename = part.get("filename", "")

        if mime.startswith("multipart/"):
            for sub in part.get("parts", []) or []:
                _walk(sub)
        elif mime == "text/plain" and not filename:
            result["body_text"] += _decode_part(part.get("body", {}).get("data", ""))
        elif mime == "text/html" and not filename:
            result["body_html"] += _decode_part(part.get("body", {}).get("data", ""))
        elif filename:
            body = part.get("body", {})
            result["attachments"].append(
                {
                    "filename": filename,
                    "mime_type": mime,
                    "attachment_id": body.get("attachmentId"),
                    "size": body.get("size", 0),
                }
            )

    _walk(payload or {})
    return result


def parse_message(msg: dict) -> dict:
    """Parse a full Gmail message resource into a flat dict."""
    msg = msg or {}
    payload = msg.get("payload", {}) or {}
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers", []) or []
    }
    parsed = extract_mime_body(payload)
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "date": headers.get("date", ""),
        "internal_date": msg.get("internalDate"),
        "label_ids": msg.get("labelIds", []) or [],
        "snippet": msg.get("snippet", ""),
        "body_text": parsed["body_text"],
        "body_html": parsed["body_html"],
        "attachments": parsed["attachments"],
    }


def decide(state: dict, context: dict | None = None) -> dict:
    """Decide whether a fetched Gmail thread is kept, and parse it.

    Args:
        state: {"thread_id": ..., "messages": [...raw...], "skip_label_id": ...}
        context: unused, present for orchestrator compatibility.

    Returns:
        {"output": {keep, reason, thread}, "rationale": "...", "self_metric": {...}}
    """
    context = context or {}

    try:
        thread_id = state.get("thread_id")
        raw_messages = state.get("messages") or []
        skip_label_id = state.get("skip_label_id")

        messages = [parse_message(m) for m in raw_messages]
        message_count = len(messages)
        attachment_count = sum(len(m.get("attachments", [])) for m in messages)

        base_metric = {
            "confidence": 1.0,
            "message_count": message_count,
            "attachment_count": attachment_count,
        }

        # 1. Empty-thread guard (poller.py line 172): a thread with no messages
        #    carries nothing to classify — drop it.
        if not messages:
            return {
                "output": {
                    "keep": False,
                    "reason": "empty_thread",
                    "thread": None,
                },
                "rationale": (
                    f"Thread {thread_id!r} has no parseable messages; dropped."
                ),
                "self_metric": {**base_metric, "decision_path": "empty_thread"},
            }

        # 2. Belt-and-braces skip-label drop (poller.py lines 178-181): when the
        #    caller resolved the skip label, drop the thread if ANY message still
        #    carries it. This covers the case where the query-level exclusion
        #    could not be applied (label unresolved at query time).
        if skip_label_id and any(
            skip_label_id in m.get("label_ids", []) for m in messages
        ):
            return {
                "output": {
                    "keep": False,
                    "reason": "skip_label",
                    "thread": None,
                },
                "rationale": (
                    f"Thread {thread_id!r} carries skip label id "
                    f"{skip_label_id!r}; excluded from the platform."
                ),
                "self_metric": {**base_metric, "decision_path": "skip_label"},
            }

        # 3. Keep — project the first-message summary (poller.py lines 183-192).
        first = messages[0]
        thread = {
            "thread_id": thread_id,
            "messages": messages,
            "subject": first.get("subject", ""),
            "from": first.get("from", ""),
            "snippet": first.get("snippet", ""),
        }
        return {
            "output": {
                "keep": True,
                "reason": "kept",
                "thread": thread,
            },
            "rationale": (
                f"Thread {thread_id!r} kept: {message_count} message(s), "
                f"{attachment_count} attachment(s), no skip label."
            ),
            "self_metric": {**base_metric, "decision_path": "kept"},
        }

    except Exception as e:
        # Fail-open → keep. The original pipeline kept whatever it managed to
        # fetch; blocking on a parse hiccup would silently drop real mail.
        return {
            "output": {
                "keep": True,
                "reason": "kept",
                "thread": {
                    "thread_id": state.get("thread_id") if isinstance(state, dict) else None,
                    "messages": [],
                    "subject": "",
                    "from": "",
                    "snippet": "",
                },
            },
            "rationale": f"Decision logic error (fail-open → keep): {e}",
            "self_metric": {
                "confidence": 0.0,
                "decision_path": "error_fallback",
                "message_count": 0,
                "attachment_count": 0,
            },
        }


def main() -> int:
    path = os.environ.get("ORGAN_INPUT")
    raw = open(path).read() if path else sys.stdin.read()
    try:
        payload = json.loads(raw)
        state = payload["state"]
    except Exception as e:
        print(json.dumps({"error": f"invalid input: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps(decide(state, payload.get("context")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
