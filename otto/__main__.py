"""Run Otto:  python -m otto  [--port 8100]"""

import argparse
import sys


def main():
    ap = argparse.ArgumentParser(prog="otto", description="Run the Otto API")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    from .config import missing_settings
    missing = missing_settings()
    if missing:
        print("Missing required settings: %s" % ", ".join(missing))
        print("Put them in a .env beside the package, or in the environment.")
        return 1

    import uvicorn
    print("Otto on http://%s:%d" % (args.host, args.port))
    print("  console   http://%s:%d/otto/test-ui" % (args.host, args.port))
    print("  health    http://%s:%d/health" % (args.host, args.port))
    uvicorn.run("otto.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
