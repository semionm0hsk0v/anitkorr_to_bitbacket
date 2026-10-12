import os
from pathlib import Path

from dotenv import load_dotenv
from dynaconf import Dynaconf

_repo_root = Path(__file__).resolve().parents[3]
load_dotenv(_repo_root / ".env")

# Локально секреты из .env; кластерный Vault (502 без VPN) не нужен.
if os.environ.get("API_MODE", "DEV").upper() == "DEV":
    os.environ["VAULT_ENABLED_FOR_DYNACONF"] = "false"

settings = Dynaconf(
    environment=True,
    load_dotenv=True,
    envvar_prefix=False,
    settings_file=["antikorr.toml", "antikorr_llms.toml"],
    root_path=__file__.rsplit("/", 1)[0],
)
