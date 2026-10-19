import argparse

import uvicorn

from .config import load_settings


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser(prog="prompt-tester")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true", help="開發模式：程式碼變更時自動重啟")
    args = parser.parse_args()

    uvicorn.run(
        "prompt_tester.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
