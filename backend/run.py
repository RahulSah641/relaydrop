"""Start RelayDrop:  python run.py   (API + UI on one port; press F5 in VS Code)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relaydrop import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    print(f"\n  RelayDrop is running → http://{host}:{port}\n  Operator inbox      → http://{host}:{port}/admin\n")
    app.run(host=host, port=port, debug=debug, threaded=True)
