from enum import StrEnum

from src.app.version import __version__, release_notes
from src.common.config.config import settings
from src.common.utils import datetime_msk


class Tags(StrEnum):
    antikorr = "Антикоррупционная экспертиза НПА"


api_version = __version__
api_release_notes = release_notes
start_time = datetime_msk().strftime("%d %b %Y - %H:%M:%S MSK")

description_template = f"""
### Antikorr Expertise API
* Версия: **{api_version}**
* Запущен: **{start_time}**
* На порту: **{settings.get("PORT", settings["API_PORT"])!s}**
* Режим работы: **{settings["API_MODE"]}**
* Логирование: **{settings["LOG_DIR"]}**
* Последние изменения: {api_release_notes}
"""

description = (
    description_template.replace("\n+", "<br>\n✔️").replace("\n>", "<br>\n🐌").replace("\n-", "<br>\n📌").replace("(!)", "⚠️")
)


tags_metadata = [
    {
        "name": Tags.antikorr,
    },
]
