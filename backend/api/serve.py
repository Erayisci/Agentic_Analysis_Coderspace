"""Explicit website launcher using the web-tools CLI's saved configuration.

Run `python -m backend.api.serve` after building frontend/. Importing the API
or the extension elsewhere still does not load this configuration implicitly.
"""
import argparse
import os

from ..core.config import ROOT, _read_dotenv


def main():
    parser = argparse.ArgumentParser(description="Serve the website and API using saved web-tools settings.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        parser.error("Build the website first: cd frontend && npm ci && npm run build")

    from extensions.web_tools.manage import environment
    values = environment()
    key = values.get("KLOUDEKS_API_KEY") or _read_dotenv("KLOUDEKS_API_KEY") or values.get("WEB_KLOUDEKS_API_KEY")
    if key:
        values["KLOUDEKS_API_KEY"] = key
    os.environ.update(values)

    import uvicorn
    uvicorn.run("backend.api.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
