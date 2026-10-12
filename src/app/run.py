import os
import sys
from pathlib import Path

import uvicorn

backend_root = Path(__file__).resolve().parent.parent.parent

if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

import src.app.warnings_config  # noqa: F401, E402
from src.common.config.config import settings  # noqa: E402


def _reload_enabled() -> bool:
    "Автоперезагрузка ВЫКЛЮЧЕНА по умолчанию."
    return os.environ.get("UVICORN_RELOAD", "0").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    reload = _reload_enabled()
    uvicorn.run(
        "src.app.api.api:app",
        host=settings["API_HOST"],
        port=int(settings.get("PORT", settings["API_PORT"])),
        reload=reload,
        reload_dirs=[str(backend_root / "src")] if reload else None,
        proxy_headers=True,
        server_header=True,
        # Экспертиза длится минуты; сохраняем соединение между сообщениями MCP.
        timeout_keep_alive=120,
    )
