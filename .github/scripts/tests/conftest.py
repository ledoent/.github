"""Make the parent scripts/ importable as flat modules.

Each script under .github/scripts/ is run via `python3 .github/scripts/foo.py`
which puts that directory on sys.path. Mirror that here so tests can
`import fork_sync_digest` etc.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Token isn't needed for the parser/rendering tests — but require_token()
# runs at module import (`HEADERS = make_headers(require_token(), …)`),
# so stub it so the script-level imports don't exit(2).
os.environ.setdefault("GH_TOKEN", "test-token-not-real")
