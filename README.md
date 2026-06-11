# Gmail Inbox Poller Organ

A **pure decider** for the Gmail unread-thread ingestion pipeline, extracted
from discovery-engine
[`lib/dataflow_core/gmail_inbox/poller.py`](https://github.com/Data-Flow-Advisory/discovery-engine).

## What it does

The original poller (Stream 13 Phase A) did three I/O things and three pure
decisions, interleaved:

| Step | Kind | Lives where now |
|------|------|-----------------|
| list unread threads (`threads().list`) | I/O | **caller** |
| fetch each thread (`threads().get`) | I/O | **caller** |
| resolve skip-label *name* → *id* (`labels().list`) | I/O | **caller** |
| parse MIME → `{body_text, body_html, attachments}` | pure | **this organ** |
| drop empty threads | pure | **this organ** |
| belt-and-braces skip-label drop | pure | **this organ** |
| project first-message summary | pure | **this organ** |

The organ takes a **single thread the caller has already fetched** plus the
**pre-resolved skip-label id**, and decides whether to keep it — returning the
normalised thread the platform consumes. It imports no Google client library
and makes no network or DB calls.

## Contract

`decide(state, context) -> {output, rationale, self_metric}`

### Input `state`

```json
{
  "thread_id": "thread-kept-001",
  "messages": [
    {
      "id": "msg-1",
      "threadId": "thread-kept-001",
      "labelIds": ["UNREAD", "INBOX"],
      "snippet": "Hi, here is the Q3 statement...",
      "internalDate": "1718000000000",
      "payload": {
        "mimeType": "multipart/mixed",
        "headers": [
          {"name": "Subject", "value": "Q3 statement attached"},
          {"name": "From", "value": "Ruth <ruth@example.com>"}
        ],
        "parts": [
          {"mimeType": "text/plain", "body": {"data": "<base64url>"}},
          {"mimeType": "application/pdf", "filename": "Q3.pdf",
           "body": {"attachmentId": "att-1", "size": 84213}}
        ]
      }
    }
  ],
  "skip_label_id": "Label_8841"
}
```

| Field | Meaning |
|-------|---------|
| `thread_id` | Gmail thread id, echoed onto the output thread. |
| `messages` | List of **raw** Gmail message resources (the caller's `threads().get(format="full")` output). |
| `skip_label_id` | The **resolved id** of the skip label (`dfa:skip`). `null` disables the filter — the organ never invents it. |

### Output

```json
{
  "output": {
    "keep": true,
    "reason": "kept",
    "thread": {
      "thread_id": "thread-kept-001",
      "messages": [ { "id": "msg-1", "subject": "...", "body_text": "...", "attachments": [...] } ],
      "subject": "Q3 statement attached",
      "from": "Ruth <ruth@example.com>",
      "snippet": "Hi, here is the Q3 statement..."
    }
  },
  "rationale": "Thread 'thread-kept-001' kept: 1 message(s), 1 attachment(s), no skip label.",
  "self_metric": {
    "confidence": 1.0,
    "decision_path": "kept",
    "message_count": 1,
    "attachment_count": 1
  }
}
```

`reason` ∈ `"kept" | "skip_label" | "empty_thread"`. When `keep` is `false`,
`thread` is `null`.

### Decision order

1. **empty_thread** — no parseable messages → drop (runs first; an empty thread
   is empty regardless of labels).
2. **skip_label** — `skip_label_id` set **and** any message carries it → drop
   (the `dfa:skip` privacy control).
3. **kept** — otherwise keep, with the first message's subject/from/snippet
   projected onto the thread.

### Fail-open

Any internal error returns `keep: true` with `confidence: 0.0` and
`decision_path: "error_fallback"`. The original pipeline kept whatever it
managed to fetch; dropping real mail on a parse hiccup is the worse failure.

## Run it

```bash
# stdin
echo '{"state": {"thread_id":"t","messages":[],"skip_label_id":null}}' | python3 organ.py

# file
ORGAN_INPUT=samples/kept_thread.json python3 organ.py
```

## Test

```bash
python -m pytest -v
```

## Samples

- `samples/kept_thread.json` — multipart thread with a PDF attachment, kept.
- `samples/skip_label_filtered.json` — thread carrying the skip label, dropped.
- `samples/empty_thread.json` — no messages, dropped.

CI (`.github/workflows/conformance.yml`) shadow-runs the organ on every sample
and prints each verdict + `self_metric` to the job summary, then runs the test
suite.
