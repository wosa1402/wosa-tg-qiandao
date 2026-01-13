from __future__ import annotations

import os


def main() -> None:
    try:
        import uvicorn
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "缺少 Web 依赖，请先安装: pip install -U \"tg-signer[web]\""
        ) from e

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("tg_signer.webapp.app:app", host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
