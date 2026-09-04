Implemented C1 and C2 test-first, including platform backends, capability-gated synchronization, chunking, relay, settings UI, persistence, and safety tests.

Files changed (before → after lines):

- `mouseshare/clipboard.py`: 0 → 406
- `mouseshare/app.py`: 970 → 1050
- `mouseshare/config.py`: 123 → 126
- `mouseshare/protocol.py`: 221 → 221
- `mouseshare/ui/web/index.html`: 143 → 148
- `mouseshare/ui/web/app.js`: 353 → 356
- `tests/test_clipboard.py`: 0 → 280
- `tests/test_app.py`: 909 → 921
- `tests/test_multi.py`: 233 → 262
- `tests/test_config.py`: 98 → 104
- `docs/plans/2026-09-03-capability-expansion.md`: 329 → 331

No touched file had a net line-count reduction. Existing capability assertions were expanded to include `clipboard`; their original negotiation meaning remains intact.

Loop prevention:

1. After applying remote text, record its SHA-256 and the backend sequence after writing.
2. Ignore a watched change matching that recorded sequence.
3. Also ignore content matching the last applied or sent SHA-256.

macOS threading: Apple documents pasteboard behavior but does not explicitly guarantee these calls are worker-thread safe, so all AppKit clipboard operations are synchronously marshalled to the main thread through PyObjC’s safe `NSObject` selector helper. Sources: [PyObjC threading helpers](https://pyobjc.readthedocs.io/en/latest/api/threading-helpers.html), [Apple `changeCount`](https://developer.apple.com/documentation/appkit/nspasteboard/changecount).

Limits: inline payload ≤ 32 KiB UTF-8; larger payloads use 32 KiB chunks; assembled cap is 1 MiB. Above 1 MiB is refused with a once-only notice.

Required check tail:

```text
..............................................                           [100%]
262 passed in 18.41s
```

Native Windows Python validation also passed: `68 passed` for clipboard/config/App/multi tests, followed by `13 passed` for the final clipboard-only run.

Left out: live cross-device macOS/Windows/X11 interoperability testing, because those external machines/displays were not available in this workspace. Fake platform-module coverage and native Windows Python tests passed. No commit was created.