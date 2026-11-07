# Integration verification

Use isolated rooms and accounts. Do not send test traffic to a production community without its administrators' approval. Record bridge versions, configuration, account type and permissions with each result; a passing result applies only to that setup.

## Directed pairs

For each enabled source and destination, send a unique text message from a person on the source network. Check that it appears once at the destination with understandable attribution. For Matrix, use a normal user account rather than a bridge account. Record failures and unsupported directions explicitly.

| Source | Matrix | Discord | Telegram | IRC | QQ |
|---|---|---|---|---|---|
| Matrix | — | ☐ | ☐ | ☐ | ☐ |
| Discord | ☐ | — | ☐ | ☐ | ☐ |
| Telegram | ☐ | ☐ | — | ☐ | ☐ |
| IRC | ☐ | ☐ | ☐ | — | ☐ |
| QQ | ☐ | ☐ | ☐ | ☐ | — |

Wait for delayed echoes before marking a case complete. Test two or more bridges in the same room; a working Matrix-to-platform connection alone does not establish cross-platform delivery. Repeat with multiple communities to check that messages reach only their assigned endpoints.

## Event and failure cases

For each supported direction, exercise Unicode text, long messages, links, mentions, replies, edits, deletion and attachments. Record the visible result on every destination, including notices for unsupported operations. Check that mentions cannot unexpectedly alert a broad group.

Restart each bridge and the homeserver with messages in flight. Exercise duplicate event delivery, bridge disconnection, expired credentials, rate limits and recovery from a restored backup. A platform send may succeed even if its response is lost; record such outcomes as unknown until reconciled. Confirm a failed or disconnected bridge cannot produce an uncontrolled retry storm or prevent other communities from operating.

Test encrypted rooms separately, with explicit approval and the actual bridge versions. Confirm what each bridge can decrypt and forward. Do not infer encrypted-room behavior from plaintext tests.
