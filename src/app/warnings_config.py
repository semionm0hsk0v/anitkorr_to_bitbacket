from __future__ import annotations

import warnings


def configure_upstream_warnings() -> None:
    try:
        from langchain_core._api.deprecation import LangChainPendingDeprecationWarning  # noqa: PLC0415

        warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
    except Exception:
        pass

    warnings.filterwarnings(
        "ignore",
        message=r".*allowed_objects.*future version.*",
    )

    warnings.filterwarnings(
        "ignore",
        category=UserWarning,
        module="pydantic.main",
        message=r"(?s).*field_name='parsed'.*",
    )

    try:
        from urllib3.exceptions import InsecureRequestWarning  # noqa: PLC0415

        warnings.filterwarnings("ignore", category=InsecureRequestWarning)
    except Exception:
        pass


configure_upstream_warnings()
