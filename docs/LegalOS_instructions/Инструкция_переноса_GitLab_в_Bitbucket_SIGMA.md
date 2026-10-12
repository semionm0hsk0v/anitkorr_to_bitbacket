# Перенос сервиса LegalOS из GitLab в Bitbucket контура SIGMA

> **Важно:** навык «Судебный приказ» используется в этой инструкции только как пример переносимого репозитория. При переносе другого навыка сотрудник должен самостоятельно заменить `court_order`, `court-order`, `legalant-court-order`, номер LOS, названия веток, тегов и URL на значения своего проекта. Не копируйте значения из примера без адаптации под собственный навык.

## 1. Назначение инструкции

Инструкция описывает перенос Python-сервиса из GitLab GigaLegal во внутренний Bitbucket Сбербанка и дальнейшее обновление релизной ветки.

Основной маршрут:

1. Подготовить доступы и репозиторий.
2. Проверить рабочую директорию `$WORK` и настроить `uv`.
3. Подготовить закрытоконтурные файлы и тег в GitLab.
4. Клонировать GitLab-репозиторий по HTTPS.
5. Подключить пустой Bitbucket как `origin`, оставив GitLab как `upstream` только для чтения.
6. Создать ветку `release/LOS-<номер>-<год>`.
7. Запустить проверки и передать ветку в сборку SIGMA.
8. Последующие правки переносить из GitLab через cherry-pick.

В примерах используются:

- GitLab: `https://gitlab.gigalegal.space/l3m/backend/court_order.git`;
- Bitbucket: `ssh://git@stash.delta.sbrf.ru:7999/lex/legalant-court-order.git`;
- GitLab-ветка: `main`;
- GitLab-тег: `los-5-2026-v1`;
- Bitbucket-релиз: `release/LOS-5-2026`.

Для другого сервиса замените URL, имя сервиса, номер LOS, год и тег.

## 2. Предварительные условия

До начала работы должны быть получены:

- доступ к GitLab `https://gitlab.gigalegal.space/l3m/backend`;
- доступ read/write к проекту Bitbucket `https://stash.delta.sbrf.ru/projects/LEX`;
- пустой Bitbucket-репозиторий с именем, начинающимся с `legalant-`;
- согласование Team Lead и архитектора на перенос сервиса;
- доступ к SberOSC и личный токен SberOSC;
- доступ к контуру SIGMA.

Не добавляйте пароли, токены SberOSC и другие секреты в репозиторий, `pyproject.toml` или `uv.lock`.

## 3. Проверка `$WORK` и настройка `uv`

### 3.1. Сначала проверить `$WORK`

Перед установкой `uv` обязательно проверьте, существует ли выделенная рабочая директория `$WORK`:

```bash
whoami
printf 'HOME=<%s>\nWORK=<%s>\n' "$HOME" "$WORK"
ls -ld "$HOME"
ls -ld "$WORK"
```

Корректный результат:

- переменная `WORK` не пустая;
- `ls -ld "$WORK"` показывает существующий каталог;
- у текущего пользователя есть права записи в него.

Если `$WORK` не задан, каталога нет или запись запрещена, остановитесь и запросите установку/подключение рабочей папки через поддержку рабочего места. Не устанавливайте `uv` в случайную системную директорию.

### 3.2. Проверить, установлен ли `uv`

```bash
uv --version
```

Если команда выводит версию, повторная установка не требуется.

Если `uv` не установлен, подготовьте каталоги:

```bash
mkdir -p "$WORK/.uv" "$WORK/.python" "$WORK/.config/uv"
```

Установите `uv`:

```bash
python3 -m pip install uv \
  --break-system-packages \
  --target "$WORK/.uv"
```

Добавьте пользовательские настройки в `~/.bashrc` один раз:

```bash
echo 'export PATH="$WORK/.uv/bin:$PATH"' >> ~/.bashrc
echo 'export UV_PYTHON_INSTALL_DIR="$WORK/.python"' >> ~/.bashrc
source ~/.bashrc
```

Проверьте установку:

```bash
type -a uv
uv --version
```

Токен и зеркала SberOSC настройте по действующей корпоративной инструкции. Токен должен находиться в пользовательской переменной окружения `SBEROSC_TOKEN`, а не в файлах проекта:

```bash
export SBEROSC_TOKEN="<ЛИЧНЫЙ_ТОКЕН>"
```

Не публикуйте значение токена в логах, скриншотах и Git.

### 3.3. Если SberOS запрещает запуск инструментов из репозитория

Если `make check` завершается ошибкой вида `Failed to spawn: ruff` / `Operation not permitted`, разместите виртуальное окружение и кеш в `$WORK`:

```bash
mkdir -p "$WORK/.venvs/<service-name>" "$WORK/.cache/uv"

export UV_PROJECT_ENVIRONMENT="$WORK/.venvs/<service-name>"
export UV_CACHE_DIR="$WORK/.cache/uv"
export UV_LINK_MODE=copy

uv sync --frozen
uv run ruff --version
```

Для судебного приказа вместо `<service-name>` используется `court-order`.

Эти переменные действуют в текущей терминальной сессии. Не задавайте одно общее `UV_PROJECT_ENVIRONMENT` глобально для разных проектов.

## 4. Подготовка GitLab-репозитория

### 4.1. Проверить закрытоконтурные файлы

В репозитории должны быть:

```text
sberbank/
├── Dockerfile
├── run_scripts/
│   ├── full_start.sh
│   ├── start.sh
│   └── waitingsecrets.sh
└── settings/
    ├── pip/
    │   └── pip.conf
    └── uv/
        └── uv.toml

scripts/
├── init-repo-in-bb.sh
├── create-release.sh
├── cherry-pick-upstream.sh
├── lib-project.sh
└── README.md
```

Если в новом навыке каталогов `sberbank/` и `scripts/` ещё нет, их можно целиком взять за основу из другого навыка, который уже успешно перенесён и развёрнут в контуре SIGMA. Это рекомендуемый способ получить актуальную структуру и штатные скрипты синхронизации.

После копирования обязательно адаптируйте файлы под собственный навык:

- замените имя сервиса и репозитория;
- проверьте точку входа приложения и порт;
- проверьте пути к исходному коду, `pyproject.toml` и `uv.lock`;
- замените специфичные для исходного навыка настройки и переменные окружения;
- не переносите чужие секреты, токены и локальные значения конфигурации.

Проверьте в `sberbank/Dockerfile` корректное имя сервиса, а в `sberbank/run_scripts/start.sh` — актуальную точку входа приложения.

Скриптам нужны права на выполнение:

```bash
chmod +x scripts/*.sh sberbank/run_scripts/*.sh
```

Если политика SberOS запрещает прямой запуск, используйте `bash ./scripts/<script>.sh`.

### 4.2. Проверить проект перед тегированием

```bash
git switch main
git pull --ff-only origin main
git status
make check
```

`make check` должен завершиться успешно, а рабочая директория должна быть чистой.

### 4.3. Создать согласованный GitLab-тег

Формат тега:

```text
los-<номер LOS>-<год>-v<номер сборки>
```

Пример:

```bash
git tag -a los-5-2026-v1 -m "LOS-5-2026 v1"
git push origin los-5-2026-v1
```

Проверка:

```bash
git tag --list los-5-2026-v1
```

## 5. Создание Bitbucket-репозитория

Запросите создание пустого репозитория в проекте `LEX`. Имя должно начинаться с `legalant-`.

Для судебного приказа:

```text
legalant-court-order
```

Получите SSH URL с правами read/write:

```text
ssh://git@stash.delta.sbrf.ru:7999/lex/legalant-court-order.git
```

На этом этапе Bitbucket может быть полностью пустым: без `master` и других веток.

## 6. Клонирование репозитория из GitLab

Создайте отдельную рабочую директорию для переносимого навыка и перейдите в неё. Название и расположение этой директории можно выбрать самостоятельно:

```bash
mkdir -p "<РАБОЧАЯ_ДИРЕКТОРИЯ>"
cd "<РАБОЧАЯ_ДИРЕКТОРИЯ>"
```

Клонируйте репозиторий обычной командой `git clone`:

```bash
git clone https://gitlab.gigalegal.space/l3m/backend/court_order.git
cd court_order
```

Git самостоятельно создаст внутри выбранной директории папку `court_order` с репозиторием. Для другого навыка используйте его GitLab URL и перейдите в созданную для него папку.

Во время HTTPS-клонирования Git может запросить данные корпоративной учётной записи GitLab. Это штатное поведение: введите свой логин GitLab, а затем свой пароль или персональный токен — в зависимости от настроенного в GitLab способа авторизации. При вводе пароля или токена терминал обычно не показывает символы и не двигает курсор; это не ошибка, после ввода нужно нажать Enter.

Используйте только собственную учётную запись. Не вставляйте логин, пароль или токен непосредственно в URL и не сохраняйте их в файлах репозитория.

URL вставляется в терминал как обычный адрес — без Markdown-скобок `[ ]( )`. Иначе Git может вывести `protocol 'https' is not supported`.

Проверьте клон:

```bash
git status
git branch --show-current
git tag --list los-5-2026-v1
git config core.fileMode false
```

Ожидается:

- ветка `main`;
- чистая рабочая директория;
- тег найден;
- `core.fileMode=false`, чтобы различия файловых прав SberOS не создавали лишние изменения.

Если GitLab недоступен по SSH с ошибкой `Connection reset`, используйте согласованный HTTPS URL. Если HTTPS-клон также не проходит, остановитесь и проверьте сетевой доступ и права к GitLab; альтернативный перенос содержимого этой инструкцией не предусмотрен.

## 7. Подключение Bitbucket и защита GitLab от случайного push

В корне клонированного репозитория выполните:

```bash
chmod +x scripts/*.sh
bash ./scripts/init-repo-in-bb.sh
```

На запрос скрипта введите Bitbucket URL:

```text
ssh://git@stash.delta.sbrf.ru:7999/lex/legalant-court-order.git
```

Скрипт:

- переименует GitLab remote `origin` в `upstream`;
- добавит Bitbucket как новый `origin`;
- запретит push в GitLab через `upstream`;
- подключит `master`, если ветка уже существует в Bitbucket.

Проверьте результат:

```bash
git remote -v
git branch --show-current
```

Ожидаемая схема:

```text
origin    ssh://git@stash.delta.sbrf.ru:7999/lex/legalant-court-order.git (fetch)
origin    ssh://git@stash.delta.sbrf.ru:7999/lex/legalant-court-order.git (push)
upstream  https://gitlab.gigalegal.space/l3m/backend/court_order.git (fetch)
upstream  no_push (push)
```

Правило:

- `git fetch upstream` получает изменения из GitLab;
- `git push origin ...` отправляет изменения в Bitbucket;
- `git push upstream` должен быть заблокирован.

## 8. Создание релизной ветки в Bitbucket

Перед созданием ветки:

```bash
git status
git fetch upstream --tags --prune
```

Рабочая директория должна быть чистой.

Создайте релиз из согласованного тега:

```bash
bash ./scripts/create-release.sh 5 refs/tags/los-5-2026-v1
```

Скрипт создаст и отправит в Bitbucket ветку:

```text
release/LOS-5-2026
```

Проверьте:

```bash
git branch --show-current
git status
git log --oneline -5
git remote -v
```

Если скрипт запускался с `PUSH=0`, отправьте ветку вручную:

```bash
git push -u origin release/LOS-5-2026
```

## 9. Проверка релизной ветки

Выполните проверки проекта:

```bash
uv sync --frozen
make check
git status
```

Проверки должны завершиться успешно, а `git status` не должен показывать незакоммиченных изменений.

Если `uv.lock` изменился локально, сначала посмотрите только состав изменений:

```bash
git diff -- uv.lock
```

Не коммитьте `uv.lock`, если изменение возникло только из-за локальной настройки SberOSC или содержит токен. Временно сохраните изменение:

```bash
git stash push -m "local uv.lock before GitLab sync" -- uv.lock
```

После успешных локальных проверок переходите к заполнению РД и передаче релизной ветки на сборку согласно разделу 11.

## 10. Перенос последующих исправлений из GitLab

Этот раздел используется, если ветка `release/LOS-5-2026` уже существует, а в GitLab `main` появились новые согласованные коммиты.

Переключитесь на релизную ветку и убедитесь, что она чистая:

```bash
git switch release/LOS-5-2026
git status
git pull --ff-only origin release/LOS-5-2026
```

Создайте локальную страховочную ветку:

```bash
git branch backup/LOS-5-2026-before-update
```

Получите свежую историю GitLab:

```bash
git fetch upstream main
git log --oneline HEAD..upstream/main
```

Для одного или нескольких конкретных коммитов используйте штатный скрипт:

```bash
bash ./scripts/cherry-pick-upstream.sh <SHA_1> <SHA_2>
```

SHA передаются от самого раннего коммита к самому позднему.

Если возник конфликт:

```bash
git status
```

После ручного исправления конфликтов:

```bash
git add <ИСПРАВЛЕННЫЕ_ФАЙЛЫ>
git cherry-pick --continue
```

Для полного отказа от незавершённого cherry-pick:

```bash
git cherry-pick --abort
```

После переноса:

```bash
uv sync --frozen
make check
git status
git log --oneline -10
git push origin release/LOS-5-2026
```

Cherry-pick создаёт новые SHA в Bitbucket. Отличие SHA от GitLab является нормальным: содержимое коммитов сохраняется, но они создаются заново поверх релизной ветки.

## 11. Заполнение РД и передача навыка на сборку ИФТ

После первоначального переноса репозитория и добавления последних согласованных исправлений из GitLab через cherry-pick, если такие исправления были, самостоятельно завершать релиз или сливать релизную ветку в `master` не нужно.

### 11.1. Заполнить релизную документацию

Откройте [РД LegalOS](https://confluence.delta.sbrf.ru/display/LEGALBPM/LEGALOS) и заполните данные нового сервиса по образцу уже внесённых навыков.

В РД найдите актуальную для релиза версию оркестратора. Для релиза 5 соответствующее поле называется:

```text
legalant-native-orchestrator
```

В конфигурации этого сервиса добавьте MCP URL своего навыка по аналогии с уже существующими переменными. Не копируйте название судебного приказа для другого навыка: замените имя переменной и внутреннее имя Kubernetes-сервиса на значения собственного проекта.

Для навыка «Судебный приказ» в релизе 5 была добавлена строка:

```yaml
ORCHESTRATOR__MCP__COURT_ORDER__URL: http://legalant-court-order-svc:8080/mcp/
```

Перед сохранением РД проверьте:

- имя переменной навыка после `ORCHESTRATOR__MCP__`;
- внутреннее имя сервиса вида `legalant-<service-name>-svc`;
- порт приложения;
- наличие завершающего пути `/mcp/`.

### 11.2. Передать релизную ветку на сборку

Напишите в чат `legalOS_dev`:

1. Укажите название переносимого навыка.
2. Пришлите ссылку на Bitbucket-репозиторий.
3. Укажите подготовленную ветку `release/LOS-<номер>-<год>`.
4. Отметьте Тимофея Глазкова.
5. Попросите собрать навык для ИФТ.

Пример сообщения:

```text
Коллеги, подготовлен навык «Судебный приказ» для релиза LOS-5-2026.
Репозиторий: <ссылка на Bitbucket>
Ветка: release/LOS-5-2026
Тимофей Глазков, просьба собрать навык для ИФТ.
```

Для другого навыка обязательно замените название, ссылку на репозиторий и релизную ветку.

## 12. Краткая диагностика известных ошибок

### `protocol 'https' is not supported`

Проверьте, что в `git clone` вставлен обычный URL без Markdown-разметки:

```text
https://gitlab.gigalegal.space/l3m/backend/court_order.git
```

### `Connection reset ... port 22`

SSH-доступ к GitLab недоступен с рабочего места или для учётной записи. Используйте согласованный HTTPS URL и проверьте права к репозиторию.

### `Operation not permitted` при запуске `.sh`

Запускайте читаемый скрипт через Bash:

```bash
bash ./scripts/init-repo-in-bb.sh
```

### `Failed to spawn: ruff` / `Operation not permitted`

Вынесите проектное окружение в разрешённую директорию `$WORK`, как описано в разделе 3.3.

### `_sber_deferred_vars: команда не найдена`

Это ошибка пользовательской конфигурации shell, а не Git-операции. Если основная команда завершилась успешно, перенос можно продолжать; саму ошибочную строку в `~/.bashrc` следует позднее исправить отдельно.

## 13. Финальный чек-лист

- [ ] Получены согласования Team Lead и архитектора.
- [ ] Создан Bitbucket-репозиторий `legalant-...` с правами read/write.
- [ ] `$WORK` существует и доступен для записи.
- [ ] `uv --version` выполняется успешно.
- [ ] В GitLab есть актуальные `sberbank/` и `scripts/`.
- [ ] `make check` проходит на GitLab-версии.
- [ ] Создан и опубликован согласованный тег GitLab.
- [ ] Репозиторий клонирован из GitLab по HTTPS.
- [ ] `origin` указывает на Bitbucket, `upstream` — на GitLab только для чтения.
- [ ] Создана ветка `release/LOS-<номер>-<год>`.
- [ ] `uv sync --frozen` и `make check` завершились успешно.
- [ ] Релизная ветка отправлена в Bitbucket.
- [ ] В РД LegalOS заполнены данные навыка.
- [ ] В актуальный `legalant-native-orchestrator` добавлен MCP URL навыка.
- [ ] В `legalOS_dev` отправлены репозиторий и релизная ветка.
- [ ] Тимофей Глазков отмечен с просьбой собрать навык для ИФТ.
