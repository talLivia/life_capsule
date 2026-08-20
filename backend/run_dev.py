"""Dev launcher — the ONLY safe way to run the backend on Windows.

`python -m uvicorn main:app` creates the event loop BEFORE lazily importing
main.py, so main's WindowsSelectorEventLoopPolicy pin (required by the
psycopg/LangGraph checkpointer — see main.py's header comment) comes too
late: the server runs on Proactor and every segment analysis crashes with
"Psycopg cannot use the 'ProactorEventLoop'" (found live 2026-08-20, after
two recording attempts failed). Importing main FIRST, then calling
uvicorn.run, applies the policy before any loop exists.

    python run_dev.py          # no --reload on purpose: on this machine the
                               # reloader wedges and can co-bind :8000
"""

import main  # noqa: F401  — pins the event-loop policy at import time
import uvicorn

if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=8000)
