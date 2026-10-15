# План: разделяне на `app.py` на модули

Статус: **само план** — нищо от описаното тук още не е направено. Числата са измерени върху `app.py` при
commit `0ff43c0` (24 928 реда; в работното копие в момента има staged промени, затова номерата на редовете
може да са изместени с ±15). При изпълнение търси границите по текста на банера (`# ----- ... -----`), не по
номер на ред.

## 0. Накратко

- Днес: един файл, 24 928 реда, 99 модела (`db.Model`), 444 view функции (446 `@app.route`), 3
  `after_request`, 3 `context_processor`, 5 фонови нишки, 17 Jinja globals + 1 филтър (`local`). Ядрото (DXF/цени, каталог,
  поръчки, склад, разписки, оферти, производство) е ~7 600 реда код + ~2 000 реда модели; останалото са
  други домейни.
- Предложение: пакет `trafcom/` с `extensions.py`, един `models.py` (всички модели), `core/helpers.py` и
  **10 домейн модула** (някои домейни са групирани, защото имат кръгови зависимости помежду си).
  Маршрутите остават `@app.route` върху **същия** `app` обект — без Blueprints, така че нито едно
  endpoint име не се променя. `app.py` става shim, който ре-експортира всичко, така че
  `from app import X` в тестове, 126 миграции и `wsgi.py` продължава да работи без промяна.
- Ред: подготовка → `extensions` → `models` → `core/helpers` → листови домейни (автомобили, генератор,
  ISO/качество, мрежа) → огъване → чат → енергетика → обект/хале → камери. Core маршрутите (Фаза C) — по
  желание, по-късно.
- Препоръка: **да, Фаза A + B, но само в „тих прозорец“ без отворени worktree клонове**; Фаза C и
  разделяне на моделите по домейни — не сега. Подробно в т. 8.

## 1. Какво има днес (измерено)

Секции на `app.py` и колко реда код (без моделите им) биха отишли във всеки модул:

| Секция (банер) | Редове в `app.py` | Размер | Отива в |
|---|---|---|---|
| imports, `app = Flask`, ProxyFix, logging, `APP CONFIGURATION`, `csrf`, `db`, `login_manager`, `limiter`, Babel, `anthropic_client`, папки | 1–273 | 273 | `extensions.py` |
| `МОДЕЛИ В БАЗАТА ДАННИ` (вкл. вложените ISO банери 3626–3890, мрежовите модели, `_RichTextSanitizer`, `load_user`) | 274–4122 | 3 849 | `models.py` |
| `DXF ГЕОМЕТРИЯ И ЦЕНИ` + достъп/роли + `_log_activity` + context processors + Jinja globals | 4123–5387 | 1 265 | `core/helpers.py` |
| публични страници (`/`, `/services`, robots, sitemap) | 5388–5522 | 135 | core (app.py) |
| Panel Generator | 5523–5779 | 257 | `generator.py` |
| `АБКАНТ / ОГЪВАНЕ` (+ 10 модела Bend*) | 5780–7532 | 1 753 | `press_brake.py` (+ модели) |
| `FLASHING` (7533–7844 и 9105–9177) | — | 385 | `flashing.py` |
| Хале 3D, досиета, сливане на машини | 7845–9104 | 1 260 | `facility.py` |
| сигурност, `EMAIL DELIVERY`, auth, акаунт, upload, `АДМИНИСТРАТОРСКИ МАРШРУТИ`, услуги, детайли, продукти, поръчки, производство, етикети | 9178–14283 | 5 106 | core (app.py) |
| `КОНТРОЛ НА КАЧЕСТВОТО` + всички `ISO 9001 - ...` маршрути | 14284–15779 | 1 496 | `quality.py` |
| `QUICK-CREATE API ENDPOINTS` + „ADD THESE THREE ROUTES“ | 15780–16361 | 582 | core (app.py) |
| `SHELLY`, `MQTT LIVE FEED`, `LIVE SNAPSHOT CACHE`, `POLLER HEALTH` (+ `/health`) | 16362–17681 | 1 320 | `energy.py` |
| `HELIOTHERM ТЕРМОПОМПА`, разходи, енергиен отчет, „Виж лог“ | 17682–18513 | 832 | `energy.py` |
| `/admin/power*` | 18514–18959 | 446 | `energy.py` |
| `СГРАДИ`, `ЕЛЕКТРИЧЕСКИ ТАБЛА`, `СХЕМА НА ЕЛ. ТАБЛО`, `БАТЕРИЙНО СТОПАНСТВО`, `ТЕМПЕРАТУРНИ СЕНЗОРИ` | 18960–20288 | 1 329 | `facility.py` |
| `КАМЕРИ`, `РАЗПОЗНАВАНЕ`, `КАЛИБРИРАНЕ`, `СОБСТВЕНИ ОБЕКТИ` | 20289–21160 | 872 | `cameras.py` |
| `КОНВЕКТОРИ` | 21161–21350 | 190 | `facility.py` |
| `АВТОМОБИЛНО СТОПАНСТВО` | 21351–21503 | 153 | `vehicles.py` |
| `MODBUS ЕЛЕКТРОМЕРИ` (Solis, DTSU666, батерийни стекове) | 21504–22439 | 936 | `energy.py` |
| `LAN / МРЕЖОВА ИНФРАСТРУКТУРА` | 22440–23419 | 980 | `network.py` |
| `ИНТЕРАКТИВНА КАРТА НА ФАБРИКАТА` (без слънцето) | 23420–23731 | 312 | `facility.py` |
| слънце (`SITE_LATITUDE`, `_sun_position`, `_sun_rise_set`) + `ПОКРИВ СЪС СОЛАРНИ ПАНЕЛИ` | 23732–23922 | 191 | `energy.py` |
| `ОФЕРТИ` | 23923–24356 | 434 | core (app.py) |
| `CHAT WIDGET` | 24357–24837 | 481 | `chat.py` |
| `CUSTOM ERROR PAGES` + `__main__` | 24838–24928 | 91 | остава в `app.py` |

Hooks и глобални регистрации (важат за реда на импортиране):

- `@app.after_request`, в реда на регистрация: `_log_activity` (5226), `_hall_place_new_devices` (8214),
  `_security_headers` (9179). Flask ги изпълнява в **обратен** ред.
- `@app.context_processor`: `inject_locale` (192), `inject_current_year` (5258), `inject_vehicle_alerts`
  (5263 — ядрото чете модела `Vehicle` за банера в navbar-а).
- `@app.url_defaults _static_cache_bust` (181) — ползва `app.static_folder`.
- SQLAlchemy listeners: `_sync_name` върху `HallMachine.name`/`Machine.name` (2983–2995) и
  `_hall_register_device_hooks()` — извиква се на ниво модул (ред ~8225) и закача `after_insert`/`after_delete`
  върху 7 модела.
- Нишки: `start_shelly_history_poller`, `start_solis_history_poller`, `start_mqtt_listener`,
  `start_heatpump_poller`, `start_detection_worker` — пускат се от `wsgi.py` и от `__main__`, пазени от
  глобални флагове (`_shelly_poller_started`, `_solis_poller_started`, `_heatpump_started`, `_detect_started`).
- `env` парсване при import, което може да хвърли: `SECRET_KEY`/`DATABASE_URL` (RuntimeError),
  `APP_TZ = ZoneInfo(...)` (5318), `LIVE_CACHE_*` (17283–17292), `READINGS_KEEP_DAYS` (17495),
  `DETECT_*` (20405–20407), `MODBUS_LOCK_TIMEOUT` (21526).

Кой какво внася от `app`:

- тестове (`testing/`, 104 файла): `User` 82×, `app` 74×, `db` 71×, `limiter` 67×, `MaterialPrice` 58×,
  `Detail` 47×, плюс ~90 частни функции (`_bump_stock`, `_reset_token`, `_flashing_unfold`,
  `_program_tooling`, `_normalize_mac`, `inject_vehicle_alerts`, `HALL_SEED`, ...);
- миграции (126 от 130 файла): `app, db` + модели + `_hall_auto_place`, `_hall_sync_maps`,
  `_hall_place_room`, `_hall_fit_buildings`, `_remove_room`, `_card_dims`, `_next_erp_number`,
  `calculate_material_price`, `seed_*`, `HALL_*`, `_REGISTRATION_CLOSED_KEY`;
- `wsgi.py`: `app` + петте `start_*`.

**10 тестови файла подменят атрибути на модула `app`** (`monkeypatch.setattr(appmod, ...)` или
`appmod.X = ...`) — това е капанът №1 при местене, виж т. 4.

## 2. Целева структура

```
app.py                  shim: re-export + (до Фаза C) core маршрутите + error handlers + __main__
wsgi.py                 без промяна
trafcom/
  __init__.py           празен
  extensions.py   ~280  app, config, ProxyFix, logging, csrf, db, login_manager, limiter, babel,
                        get_locale, _static_cache_bust, localized, anthropic_client, папките за качване
  models.py      ~4100  всички 99 модела + свързаните с тях константи, seed данни, _next_*_number,
                        get_text, calculate_product_pricing, _lock_numbering, крипто helper-ите
  core/
    __init__.py
    helpers.py   ~1450  DXF геометрия и цени, role_required, app_access_required, client_allows,
                        catalog_item_allowed, log_action, describe_changes, _log_activity,
                        _security_headers, send_email, Jinja globals, CREATE_NEW_TARGETS/create_url,
                        _save_upload и другите разпилени helper-и (списък в стъпка A3)
    (Фаза C) public_auth.py ~900 · admin.py ~2400 · catalog.py ~2050 · quick_create.py ~580 · offers.py ~430
  vehicles.py     ~180  автомобили + inject_vehicle_alerts
  generator.py    ~260  Panel Generator
  quality.py     ~1500  контрол на качеството + ISO 9001
  network.py      ~980  LAN модул
  flashing.py     ~420  обкантващи профили (_fnum, _flashing_unfold, FLASHING_PROFILES, _attach_flashing_step)
  press_brake.py ~1650  абкант маршрутите (не е `bending/` — той остава Flask-free пакет)
  chat.py         ~480  AI чат
  energy.py      ~3650  Shelly, MQTT, кеш, poller-и, /health, Modbus/Solis/DTSU, термопомпа,
                        отчети, „Виж лог“, слънце, соларен покрив
  facility.py    ~3100  сгради, табла, батерии, сензори, конвектори, карта на фабриката, хале 3D, досиета
  cameras.py      ~870  камери, разпознаване, калибриране, собствени обекти
```

Защо такива групи (по реалните зависимости, извлечени с AST анализ на `app.py`):

- **Моделите — в един файл.** Те са най-преплетени: `Machine.room` → `Room`, `Machine.panel` →
  `ElectricalPanel` (1760–1761), а backref-ите `Machine.shelly_devices`, `Machine.modbus_devices`,
  `Machine.hall_record`, `Machine.services`, `Room.machines`, `Room.convectors` се създават от моделите на
  *други* домейни. Ако моделите се разделят, атрибутът `machine.shelly_devices` съществува само ако
  модулът на Shelly е импортиран. В един файл този проблем изчезва, а таблиците и имената на класовете
  не се променят → никаква миграция. (Отделно: в корена има директория `models/` с `yolox_s.onnx`, затова
  файлът е `trafcom/models.py`, не `models.py` в корена.)
- **`energy.py` е един модул**, защото Shelly ↔ Modbus са кръгови: `_shelly_history_poll_tick` и
  `admin_power_data` викат `_dtsu666_snapshot`/`_solis_snapshot`, а Modbus ползва `DeviceBusy`,
  `_cached_modbus_snapshot` от кеша; термопомпата ползва `_shelly_key`, `_heatpump_cost_cfg` от
  отчетите, а отчетите — модела на термопомпата; соларният покрив ползва слънцето от картата на фабриката.
  Вътре в един модул тези кръгове не пречат.
- **`facility.py` е един модул**, защото табла, батерии, сензори и конвектори викат `_hall_follow_room` /
  `_hall_pull`, а халето вика `_collect_power_aggregates` и `_hall_room_rect`/`_hall_shapes` с картата на
  фабриката.
- **Посоката е еднопосочна**: `extensions → models → core.helpers → {vehicles, generator, quality, network,
  flashing → press_brake → chat, energy → facility → cameras}`. Нито един домейн не ползва функция от
  core *маршрутите* — само от helper-ите (`role_required` 19 модула, `log_action` 16, `limiter` 13,
  `app_access_required`, `allowed_only`, `_save_upload`, `_lock_numbering`, `to_local`, ...).
- Обратни връзки core → домейн (само 4, всички извиквания по време на изпълнение):
  `list_machines` → `_machines_overview_rows`, `rename_machine` → `_hall_follow_room` (facility),
  `api_quick_create_detail` → `_attach_flashing_step` (flashing), `_selected_machines` →
  `_parse_machine_ids` (преместваме го в helpers). Докато core маршрутите са в `app.py`, те намират
  имената през ре-експорта; във Фаза C стават локален import във функцията.

## 3. Регистриране на маршрутите без смяна на endpoint имената

**Решение: обикновени модули, които правят `from trafcom.extensions import app, db, limiter` и ползват
`@app.route` точно както днес. Без Blueprints, без app factory.**

Endpoint по подразбиране = името на функцията, независимо от модула. Следователно `url_for('admin_power')`
в шаблоните (723 извиквания в 117 шаблона, 69 само в `partials/navbar.html`) и 630 `url_for(` в самия
`app.py` продължават да работят без нито една промяна. Имената на функциите вече са уникални (единственото
дублирано име е `my_orders` — маршрутът на ред 13759 и AI инструментът на ред 24570; маршрутът е
регистриран с първата функция, разделянето дори премахва засенчването). Ако по грешка две функции с еднакво
име се регистрират като маршрути, Flask хвърля `AssertionError: View function mapping is overwriting...`
още при import — грешката е шумна.

Защо не Blueprints:

- Blueprint винаги слага префикс: `Blueprint('power', ...)` → endpoint `power.admin_power`. Празно име
  на blueprint не е позволено във Flask 3.1 (инсталирана е 3.1.3). Значи или ~1 350 редакции на
  `url_for`, или хак (`app.url_build_error_handlers`, собствен `url_for`) — и двете чупливи.
- Endpoint имената се ползват и като данни: `_log_activity` записва `request.endpoint` в `ActivityLog`,
  когато маршрутът не е извикал `log_action()` — след Blueprint старите и новите редове в лога ще са с
  различни имена; `CREATE_NEW_TARGETS` (5341) пази endpoint низове; `login_manager.login_view = 'login'`;
  smoke тестът има `EXCLUDED = {'camstream_dev_proxy', 'admin_camera_video', ...}` по endpoint име.
- Ползите на Blueprint (url_prefix, отделни `before_request`, повторна употреба) тук не са нужни: има един
  `app`, един процес.

Защо не app factory (`create_app()`): 446 декоратора и всички модулни `app.config[...]`/`os.makedirs`
разчитат на глобален `app`; тестовете ползват `from app import app`. Factory няма какво да спечели при
един екземпляр.

**Капан при `extensions.py`:** `Flask(__name__)` в `trafcom/extensions.py` ще търси `templates/` и
`static/` в `trafcom/` → всяка страница е `TemplateNotFound`. Пиши:

```python
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask('app', root_path=ROOT)   # import name 'app' -> app.name и app.logger остават 'app'
```

Flask-Limiter ключува `@limiter.exempt`/`@limiter.limit` по `__module__ + __name__` на функцията — и
декораторът, и проверката виждат един и същ обект, така че местенето е безопасно; броячите са в паметта и
и без това се нулират при рестарт.

## 4. Обратна съвместимост на `from app import X`

`app.py` става shim. `import *` не върши работа (пропуска имената с `_`, а тестовете внасят
`_bump_stock`, `_reset_token`, ...), затова — малка функция:

```python
# app.py
import trafcom.extensions, trafcom.models, trafcom.core.helpers


def _reexport(*modules):
    """`from app import X` keeps working for tests, migrations and wsgi.py. Never overwrites a name
    app.py already has, so app.py's own code never gets a global swapped under it."""
    g = globals()
    for m in modules:
        for k, v in vars(m).items():
            if not k.startswith('__'):
                g.setdefault(k, v)


_reexport(trafcom.extensions, trafcom.models, trafcom.core.helpers)

# ... core маршрутите остават тук до Фаза C ...

from trafcom import vehicles, generator, quality, network, flashing, press_brake, chat, energy, facility, cameras  # noqa: E402 - registers routes
_reexport(vehicles, generator, quality, network, flashing, press_brake, chat, energy, facility, cameras)

if __name__ == '__main__':
    ...  # без промяна
```

Правила:

1. **Нито един файл в `trafcom/` не внася от `app`.** При `python app.py` модулът се казва `__main__`;
   `from app import db` вътре в `trafcom/` би заредил `app.py` втори път → втори набор `@app.route` върху
   същия `app` (AssertionError) или, по-лошо, двойни SQLAlchemy listeners и двойни нишки. Пази го тест
   (стъпка A0).
2. **`monkeypatch` трябва да сочи модула, чийто код *вика* името**, не shim-а. `setattr(appmod,
   'shelly_history', boom)` подменя само копието в `app`; маршрутът в `trafcom.energy` ще извика
   истинската функция. Най-евтиният ремонт е един ред на тестов файл: `import app as appmod` →
   `from trafcom import energy as appmod` (когато всички `appmod.*` в този файл са от един модул).
3. **Глобални променливи, които се пре-присвояват с `global`**, се копират към `app` само веднъж:
   `_mqtt_client_instance`, `_readings_cleanup_last`, `_shelly_poller_started`, `_solis_poller_started`,
   `_heatpump_started`, `_detect_lock_conn`, `_detect_started`. Само тестове ги четат през `app` — те
   трябва да се пренасочат по правило 2. Речниците (`_heatpump_live`, `_mqtt_state`, `_live_cache`,
   `_poller_seen`, `_shelly_gen_cache`) са един и същ обект и работят.
4. `wsgi.py` и 126-те миграции не се пипат. Миграциите не бива да се редактират изобщо
   (`migration/run_once.py` ги пуска веднъж; правилото от CLAUDE.md), затова **shim-ът остава завинаги** —
   струва ~15 реда. Може да се махне само ако някой ден се реши, че старите миграции вече няма да се
   пускат върху стара база; тогава заедно с ъпдейт на всички тестове към `trafcom.*`.

## 5. Последователност от стъпки

Всяка стъпка = **един commit, само преместване** (cut/paste + import редове + ре-експорт + пренасочени
тестове). Никакво преименуване и „почистване“ в същия commit. Всяка стъпка е code-only →
`deploy/update_quick.sh` (няма колони, таблици, миграции, зависимости за production).

### Стандартна проверка (след всяка стъпка)

1. `python -c "import testing, app"` — import минава върху SQLite (`testing` насочва `DATABASE_URL`).
2. `pyflakes trafcom app.py | grep "undefined name"` — празно. Това е **единствената проверка, която
   хваща липсващ import в рядко ползван POST маршрут** (smoke тестът прави само GET). `pyflakes` е само за
   разработка — добави го в `requirements-dev.txt` в стъпка A0.
3. Снимка на инварианти преди/след и `diff`:

   ```bash
   python - > "$TMP/inv_after.txt" <<'EOF'
   import types, testing, app
   a = app.app
   for r in sorted(a.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
       print('RULE', r.rule, r.endpoint, sorted(r.methods))
   print('AFTER', [f.__name__ for f in a.after_request_funcs[None]])
   print('CTX', [f.__name__ for f in a.template_context_processors[None]])
   print('JINJA', sorted(a.jinja_env.globals), sorted(a.jinja_env.filters))
   print('TABLES', sorted(app.db.metadata.tables))
   for n in sorted(vars(app)):
       if not n.startswith('__') and not isinstance(getattr(app, n), types.ModuleType):
           print('NAME', n)
   print('PATHS', a.template_folder, a.static_folder, a.root_path)
   EOF
   diff "$TMP/inv_before.txt" "$TMP/inv_after.txt"
   ```

   Очаквано: никаква разлика в `RULE`, `TABLES`, `JINJA`, `PATHS`; `NAME` може само да расте;
   `AFTER` — само документираната промяна в A3.
4. `python -m testing.run_all` — зелено (включително `test_smoke_all_pages.py`).
5. Ръчно: `FLASK_DEBUG=1 python app.py`, отвори 2–3 страници от преместения домейн, направи **един POST**
   (запис/изтриване) и провери в `/admin/log`, че записът е със същия текст/endpoint като преди.
6. След deploy: `curl /health` → 200, `journalctl -u trafcom` 10 минути без Traceback.

Rollback: `git revert <sha>` + `deploy/update_quick.sh`. Базата не се пипа в нито една стъпка, затова
revert винаги е безопасен. Направи го веднага — колкото повече commit-и стъпят отгоре, толкова повече
конфликти.

### Фаза A — основа (без нея нищо не може да излезе от `app.py`)

**A0. Подготовка** (~1 ч)
- Слей или затвори всички worktree клонове (в момента 5: `agent-a41478be…`, `agent-a74e5f0c…`,
  `agent-a835012b…`, `agent-a933dbe1…`, `agent-afcbb38d…`) и не пускай паралелни агенти върху `app.py`
  по време на стъпка. Git не следи код, преместен между файлове: клон, който е редактирал преместена
  функция, ще трябва да се пренесе на ръка (`git diff main...клон -- app.py` и ръчно в новия файл).
- Запиши `inv_before.txt` (горния скрипт) и `pyflakes app.py` като базова линия.
- Добави `pyflakes` в `requirements-dev.txt`.
- Нов малък тест `testing/test_package_layout.py`: (1) никой файл под `trafcom/` не съдържа
  `import app`/`from app import`; (2) имената в `app.after_request_funcs[None]` са в очаквания ред.
- `babel.cfg`: добави `[python: trafcom/**.py]` — иначе следващият `pybabel extract` ще изпусне всички
  `gettext(...)` от преместения код и преводите в `translations/*/messages.po` ще станат obsolete.
  Провери: брой `msgid` в `messages.pot` преди/след extract е същият.

**A1. `trafcom/extensions.py`** (~1 ч) — редове 1–273.
- Ре-експорт: всичко (`app`, `db`, `csrf`, `limiter`, `login_manager`, `babel`, `get_locale`,
  `localized`, `anthropic_client`, `STATUS_LABELS`, `BASE_SETUP_FEE`, `SUPPORTED_LANGUAGES`,
  `MACHINE_FILE_MAX_BYTES`).
- Капани: `root_path` (т. 3); `ProxyFix` да остане веднага след `app = Flask(...)`; `logging.basicConfig`
  да остане при създаването на `app`; `load_dotenv()` преди четенето на `SECRET_KEY`. Import блокът
  (редове 1–73) остава и в `app.py` — останалият код го ползва; не разчитай на ре-експорта за `os`,
  `request`, `jsonify`.
- Проверка: стандартната; специално `PATHS` в инвариантите.

**A2. `trafcom/models.py`** (~3 ч) — редове 274–4122 плюс моделите, пръснати другаде:
`PressBrakeConfig`…`BendDeduction` (5797–5907), `BendProgram`…`BendSpringbackCorrection` (7467–7532),
`FlashingPreset` (7533), `HeatPumpReading` (17684).
- Методите на моделите викат 6 имена, дефинирани по-надолу в `app.py` — те трябва да дойдат с моделите:
  `_apply_adjustment` (4724; от `calculate_product_pricing`), `_material_available_qty` (4959; от
  `ProductionOrder`), `APP_TZ`/`to_local` (5318–5329; от `CapaRecord`, `MeasuringInstrument`,
  `TrainingRecord`, `vehicle_deadline_status`), `_lock_numbering` + `NUMBER_LOCK_KEYS` (10685–10700;
  от `_next_offer_number`, `_next_capa_number`, `_next_audit_number`, `_next_hall_no`),
  `_network_api_fernet`/`_encrypt_secret`/`_decrypt_secret` (22480–~22520; от `Camera.stream_url`).
  Иначе: `NameError` по време на заявка, не при import.
- `load_user` (`@login_manager.user_loader`) и `_sync_name` listener-ите идват с моделите.
- Тестове: `test_document_locks_and_numbers.py` подменя `_lock_numbering` в `app` — пренасочи към
  `trafcom.models` (и към `trafcom.core.helpers` след A3, защото `_next_erp_number`,
  `_erp_number_conflict`, `generate_order_number` са там).
- Проверка: `TABLES` в инвариантите е същото множество (имената на таблиците идват от имената на
  класовете, които не се менят); `db.create_all()` в тест минава.

**A3. `trafcom/core/helpers.py`** (~3 ч) — редове 4123–5387 + разпилените helper-и, които домейните
ползват: `_UNSAFE_FILENAME_CHARS`/`sanitize_display_filename` (~9654), `_security_headers` +
`EMAIL DELIVERY`/`send_email` (9179–9227), `_bump_stock` (10647), `IMAGE_EXTENSIONS`,
`_UPLOADED_IMAGE_PREFIX_RE`, `_save_upload`, `_remove_files` (11414–11450), `_parse_optional_float`
(11807), `_next_erp_number`/`_erp_number_conflict` (11831–~11900), `_known_machine_types` (12137),
`_add_cutting_operation` (12876), `_parse_machine_ids`/`_resolve_machines_or_none` (18569–18595, от
секцията на /admin/power — ползват ги core, Shelly и Modbus).
- Проверено с AST: след това преместване helpers няма препратки напред към кода, който остава в
  `app.py` (единствените са моделът `BendToolSegment` — вече в `models.py` — и двете константи по-горе).
- Ред на `after_request`: беше `[_log_activity, _hall_place_new_devices, _security_headers]`; след A3 —
  `[_log_activity, _security_headers, _hall_place_new_devices]` (халето още е в `app.py` и се регистрира
  след helpers). Изпълнение: hall → headers → log. Инвариантът, който пазим: `_log_activity` е първи в
  списъка = изпълнява се последен, т.е. след commit-а на автоматичното поставяне в халето. Headers не
  зависят от базата, разместването им е безвредно. Запиши новия ред в `test_package_layout.py`.
- Тестове: `test_dxf_curves_and_pierces.py` подменя `DXF_MAX_SEGMENTS` → `trafcom.core.helpers`;
  `test_document_locks_and_numbers.py` → и `trafcom.core.helpers`. `send_email` (подменян в
  `test_auth_hardening.py`, `test_email_verification.py`) се вика от auth маршрутите, които остават в
  `app.py` → подмяната в `app` продължава да работи до Фаза C.
- Ръчно: качи DXF в `/upload` (цена), създай детайл, отвори `/admin/log`.

### Фаза B — домейните, от листата навътре

Модул може да излезе едва когато всичко, което ползва, вече е извън `app.py`. При слепване на
непоследователни блокове в един модул пази оригиналния им относителен ред — подразбиращи се аргументи и
декоратори се изчисляват при import (`start_shelly_history_poller(interval=SHELLY_POLLER_INTERVAL)`,
`_shelly_get_status(host, timeout=SHELLY_TIMEOUT)`, `_sun_position(..., lat_deg=SITE_LATITUDE)`).

| # | Модул | Какво (редове) | Капани | Тестове за пренасочване | Ръчна проверка | Усилие |
|---|---|---|---|---|---|---|
| B1 | `vehicles.py` | 21351–21503 + `inject_vehicle_alerts` (5263–5290) | context processor-ът се регистрира чак при import на модула — без него navbar-ът няма `vehicle_alerts`; `GUARANTEE_FUND_CHECK_URL` е в `models.py` | няма (`inject_vehicle_alerts` се внася през shim-а) | банерът за изтичащи срокове в navbar-а; `/admin/vehicles` | 0,5 ч |
| B2 | `generator.py` | 5523–5779 | `generator_price_on`, `allowed_only`, `app_access_required` идват от helpers; core ползва само модела `GeneratorPreset` (`delete_account`, `admin_delete_user`) | няма | `/generator`, запис на preset, DXF export | 0,5 ч |
| B3 | `quality.py` | 14284–15779 | `api_quick_create_instrument` и `admin_dashboard` остават в core (ползват само модели); файловите папки са в `app.config`, не в `__file__` | няма | `/admin/quality`, качване и сваляне на документ в `/admin/documents`, нов CAPA номер | 1 ч |
| B4 | `network.py` | 22440–23419 | `librouteros`, `netmiko`, `RouterosKey` идват с модула; крипто функциите вече са в `models.py` (камерите ги ползват също) | няма | `/admin/network`, картата, `/admin/network/audit` | 1 ч |
| B5 | `flashing.py` + `press_brake.py` | flashing: 7533–7844, 9105–9177, `FLASHING_PROFILES` (5789), `_attach_flashing_step` (6007); press_brake: останалото от 5794–7532 | press_brake → flashing (`_fnum`, `_flashing_unfold`), обратното — само модели; импортите от `bending.*` (редове 44–49) отиват в press_brake; core `api_quick_create_detail` вика `_attach_flashing_step` през ре-експорта | няма подмени; `test_bending_*` внасят `_program_tooling`, `_plan_program`, ... през shim-а | `/flashing` калкулация + DXF, програма в `/admin/bending`, симулация, quick-create детайл със стъпка огъване | 2 ч |
| B6 | `chat.py` | 24357–24837 | след B5 (ползва `_program_tooling`, `_program_time`); AI инструментът `my_orders` вече не засенчва маршрута; `anthropic_client` е в extensions | няма | чат widget-а: въпрос за поръчка и за програма за огъване | 1 ч |
| B7 | `energy.py` | 16362–18959, 21504–22439, 23732–23922 | най-много подмени в тестове; 4 `start_*` + `seed_shelly_devices` се ре-експортират за `wsgi.py`/`__main__`; `ModbusTcpClient` се импортира в модула (тестът го подменя); `_heatpump_live` се чете от халето (речник — ок) | `test_device_reliability`, `test_admin_power_history_gen1`, `test_shelly_reading_log`, `test_shelly_status` (`shelly_rpc`), `test_readings_retention`, `test_device_log` → `trafcom.energy` (`test_heatpump` само чете функции и речници — работи и през shim-а); в `test_device_reliability` `_detection_tick`/`_detection_cleanup` остават към `app` до B9 | `/admin/power` (живи данни + история Gen1/Gen2), `/admin/modbus-devices`, `/admin/heatpump` + разходи, `/admin/energy-report`, `/admin/solar-roof`; на сървъра `/health` показва всички poller-и със скорошен успех, в journald всеки poller стартира веднъж | 4 ч |
| B8 | `facility.py` | 7845–9104, 18960–20288, 21161–21350, 23420–23731 | `_hall_register_device_hooks()` и `_hall_place_new_devices` идват тук (listener-ите трябва да се закачат точно веднъж); `HALL_*` seed данните са в `models.py`; миграциите внасят `_hall_auto_place`, `_hall_sync_maps`, `_hall_place_room`, `_hall_fit_buildings`, `_remove_room`, `_card_dims` през shim-а | `test_worker_restrictions` подменя `_shelly_convector_status/_set` — викащият е `admin_convector_toggle`, вече тук → `trafcom.facility` | `/factory3d` (3D, План, Покрив), местене на машина, нов конвектор → появява се в халето, „Синхронизирай картите“, досие + качване на файл | 3 ч |
| B9 | `cameras.py` | 20289–21160 | **`DETECTION_FOLDER` и `TRAIN_FOLDER` се строят от `__file__`** (20408, 20714) — в `trafcom/` ще сочат празна папка; смени на `app.root_path`. `camstream_dev_proxy` се регистрира само при `CAMSTREAM_PROXY=1`; `start_detection_worker` се ре-експортира | `test_device_reliability` (`_detection_tick`, `_detection_cleanup`) → `trafcom.cameras`; `test_camera`, `test_custom_objects`, `test_detection` | `/admin/cameras`, история на детекциите (стари записи се виждат), обучителни снимки в `/admin/objects` | 1,5 ч |

### Фаза C — core маршрутите (по желание)

След B9 в `app.py` остават ~6 400 реда core маршрути. Ако и тях искаш вън:
`core/public_auth.py` (5388–5522, 9228–9845, error handlers), `core/admin.py` (9846–12231),
`core/catalog.py` (12232–14283), `core/quick_create.py` (15780–16361), `core/offers.py` (23923–24356);
`app.py` остава ~60 реда (импорти, ре-експорт, `__main__`). По-естественото място на `/machines`
маршрутите (12117–12230, `delete_machine` ~16280) е `facility.py` — те показват досиетата от халето.
Капани: обратните връзки от т. 2 стават локален import във функцията; `ADMIN_LOG_PAGE_LIMIT`
(`test_activity_log.py`) → `trafcom.core.admin`; `send_email` (два теста) → модула на auth маршрутите;
error handlers се регистрират при import на модула си. Усилие ~5–6 ч.

## 6. Какво да НЕ се мести (или не сега)

- **Моделите по домейни.** Backref-ите и релациите през домейните (т. 2) правят това рисково, а ползата
  е малка — `models.py` от ~4 100 реда се чете добре по класове. Да се преразгледа само ако конфликтите в
  `models.py` станат реален проблем.
- **`if __name__ == '__main__'` блокът** — остава в `app.py` (`python app.py` е начинът за локална
  разработка и за seed-а; `migration.change_admin_password` и т.н. не зависят от него).
- **`bending/`, `detection.py`, `camera_pose.py`** — вече са Flask-free, не се пипат.
- **`migration/`** — нито един файл (виж т. 4).
- **Шаблоните, endpoint имената, имената на функциите и таблиците** — нищо не се преименува.
- **Разделяне на `energy.py`/`facility.py` на по-малки модули** — възможно по слоевете, които AST
  анализът показа (`live/кеш < modbus < shelly+poller-и < термопомпа < отчети`; `синхронизация с халето <
  страниците на устройствата < страницата на халето`), но изисква пренареждане на код, не само
  преместване. Не в първия рунд.
- **Рефакторинг „докато сме вътре“** — отделни commit-и след разделянето, никога в commit за преместване.

## 7. Рискове и как да ги хванеш бързо

| Риск | Симптом | Как се хваща веднага |
|---|---|---|
| Кръгов import / `from app import` в `trafcom/` | `ImportError: partially initialized module`, или при `python app.py` — `AssertionError: View function mapping is overwriting...` | `test_package_layout.py`; `python -c "import testing, app"`; локален старт с `python app.py` |
| Липсващ import в преместен код | `NameError` само когато се изпълни конкретният (често POST) маршрут | `pyflakes ... \| grep "undefined name"` като задължителна стъпка |
| Метод на модел вика функция, останала в `app.py` | `NameError` при заявка | списъкът от 6 имена в A2; pyflakes върху `models.py` |
| Липсва backref/релация | `InvalidRequestError: ... failed to locate a name` при първата заявка; `AttributeError: 'Machine' has no attribute 'shelly_devices'` | не може да се случи, докато всички модели са в един файл и всичко се зарежда през `app` |
| Сменен ред на `after_request` | лог записът преди commit-а на халето; липсващи headers | ред `AFTER` в инвариантите + `test_package_layout.py` |
| Нишка пусната два пъти / listener закачен два пъти | двойни редове в `ShellyReadingLog`; двойно изтриване на `HallEquipment` | правило 1 от т. 4; `/health`; в journald всеки poller стартира веднъж; редове на минута в `ShellyReadingLog` = брой устройства |
| Сменено/липсващо endpoint име или забравен модул в импортите на `app.py` | `BuildError` → 500 на страница с линк; 404 за целия домейн (smoke тестът не го вижда — обхожда само регистрираните правила!) | `RULE` редовете в инвариантите |
| Шаблони/static не се намират | всяка страница 500 `TemplateNotFound`, CSS без `?v=` | `PATHS` в инвариантите; smoke тестът пада веднага |
| Данни, вързани за `__file__` | празен списък с детекции/обучителни снимки | B9 капан; ръчната проверка на камерите |
| `monkeypatch` удря shim-а | тестът пада, или smoke тестът хваща опит за мрежа | `run_all`; правило 2 от т. 4 |
| Изгубени преводи | английски/немски текстове се връщат на български след следващия `pybabel update` | `babel.cfg` в A0; брой `msgid` преди/след |
| Конфликти с паралелни клонове | огромни конфликти в `app.py` | A0: стъпка само без отворени клонове |

Rollback: всяка стъпка е един commit без промени по схемата → `git revert <sha>` и
`deploy/update_quick.sh`. Ако проблем се види на сървъра след няколко стъпки, revert-вай в обратен ред.

## 8. Усилие и препоръка

| Стъпка | Усилие (с AI асистент, без времето на `run_all`) |
|---|---|
| A0 подготовка | 1 ч |
| A1 extensions | 1 ч |
| A2 models | 3 ч |
| A3 core/helpers | 3 ч |
| B1–B4 листови домейни | 3 ч общо |
| B5–B6 огъване + чат | 3 ч |
| B7 energy | 4 ч |
| B8 facility | 3 ч |
| B9 cameras | 1,5 ч |
| **Фаза A + B** | **~22 ч ≈ 3 работни дни**, + 1 ден наблюдение след B7 и B9 (нишките) |
| Фаза C (по желание) | 5–6 ч |

Аргументи „за“ сега:
- Файлът е растял ~6× за 12 седмици; при това темпо до лятото ще е 50k+ реда.
- Начинът на работа е с няколко паралелни агента (5 worktree-а в момента) — всички пишат в един файл и
  се сблъскват. С модули агент по камерите и агент по огъването пипат различни файлове.
- AI асистентът не може да прочете 25k реда наведнъж; модул от 1–4k реда може — по-малко токени и
  по-малко пропуснати зависимости.

Аргументи „против“:
- Нула функционална полза; тестовете са зелени и всичко работи.
- Докато трае, всеки отворен клон получава тежки конфликти.
- Рискът е реален само на две места: нишките (B7, B9) и подмените в тестовете — и двете се хващат от
  проверките по-горе.

**Препоръка:** направи Фаза A и B, но не „между другото“ — в един тих прозорец от ~3 дни, без паралелни
агенти върху `app.py`, по една стъпка на commit и deploy, с инвариантите и `pyflakes` като задължителна
врата. Фаза C и разделянето на моделите — отложи, докато core не стане източник на конфликти. Ако такъв
прозорец няма скоро, по-добре е да не се започва изобщо, отколкото да остане наполовина: половин
преместен домейн е по-лош от един голям файл.

След Фаза B: обнови раздел Architecture в `CLAUDE.md` (сега описва съзнателно еднофайловата конвенция) и
препратките в `docs/SHELLY_API.md` („search for SHELLY ENERGY MONITORING in app.py“) към новите модули.
