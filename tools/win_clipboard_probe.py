# ruff: noqa: E701, E702, E741  -- throwaway diagnostic script, kept as-is
import sys

sys.path.insert(0, ".")
from mouseshare import clipboard as c

names = [n for n in dir(c) if not n.startswith("_")]
print("RESULT exports:", ", ".join(names)[:300])
create = getattr(c, "create", None) or getattr(getattr(c, "Clipboard", None), "create", None)
backend = create() if create else None
print("RESULT backend:", type(backend).__name__, "available:", getattr(backend, "available", None))
if backend is None:
    sys.exit(0)
seq0 = backend.sequence(); orig = backend.read_text()
print("RESULT sequence:", seq0, "read_text type:", type(orig).__name__, "len:", None if orig is None else len(orig))
if isinstance(orig, str):
    probe = "MouseShare clip probe ✓ 日本語\nline2"
    backend.write_text(probe)
    seq1 = backend.sequence(); back = backend.read_text()
    print("RESULT write roundtrip exact:", back == probe, "sequence advanced:", seq1 > seq0)
    backend.write_text(orig)
    print("RESULT restored:", backend.read_text() == orig, "final sequence:", backend.sequence())
else:
    print("RESULT skipped write (clipboard not text)")
