# Client Stock (материали на съхранение) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track materials and semi-finished parts that clients leave at Trafcom as the client's property: receive, consume (with remnant or scrap), return, print a protocol for each, see stock per owner.

**Architecture:** Ownership is one nullable FK (`owner_client_id`) on `MaterialPrice`, reusing the same convention `Detail`/`Product` already have. The three documents reuse the existing `DeliveryNote` (intake, now also from a client) and `ClientDeliveryNote` (outgoing, now with `kind` return/consume and optional remnant per line). Everything stays in `app.py` (single-file convention), movements stay on `_bump_stock()`.

**Tech Stack:** Flask + SQLAlchemy (`app.py`), Jinja2 templates, raw-SQL migration module, pytest (SQLite test DB via `testing/__init__.py`).

**Spec:** `docs/superpowers/specs/2026-11-03-client-stock-design.md`

## Global Constraints

- No new dependencies. No Alembic: one new `migration/migrate_add_client_stock.py` (`ADD COLUMN IF NOT EXISTS`, re-runnable, run as `python -m migration.migrate_add_client_stock`); index also mirrored in `migration/migrate_add_indexes.py`.
- Never edit an already-shipped `migrate_*.py` other than `migrate_add_indexes.py`.
- Stock moves only through `_bump_stock()`. Documents lock their row with `_lock_or_404()`; POST routes keep `@role_required('admin')` + `@retry_on_db_busy` (re-read everything from `request` on retry).
- A client-owned lot (`owner_client_id IS NOT NULL`, material or Detail) must never end a transaction below `-PRODUCTION_STOCK_TOLERANCE`; our own lots keep today's non-blocking behaviour.
- UI strings and flash messages in Bulgarian. Forms stay inside `<dialog>` (`form_dialog.js`), deletes use `data-confirm`, no DB text concatenated into `innerHTML`.
- Templates use `|local` for UTC timestamps. No `window.open()`.
- Do NOT commit. The owner reviews and commits after each task is verified (standing rule). Each task ends by running its tests, not `git commit`.
- Run tests one file at a time: `python -m pytest testing/test_client_stock.py -v`, plus the named existing files.

## Review Focus

1. Receipt line that picks one of OUR lots from the dropdown on a CLIENT receipt — must create a client lot with the same cutting speeds, never bump our lot. (Task 2 test `test_client_receipt_from_our_lot_pick`)
2. Remnant on a `consume` line for a lot whose type is linear (`price_unit == 'm'`) — quantity goes back into the same lot, no new row. (Task 3 test `test_consume_remnant_linear_same_lot`)
3. Deleting a client receipt after its material was consumed — refused, nothing changes, flash names the lot. (Task 3 test `test_delete_receipt_after_consume_refused`)
4. A non-staff upload (`/upload`) posting `material=<client lot key>` — flash refusal, no `DxfFile` row, no 500. (Task 4 test `test_upload_client_lot_refused`)
5. `format_material_option()` on a client lot shows the owner so a staff picker can tell lots apart; unchanged output for owner-less rows (existing `testing/test_material_option_format.py` must still pass). (Task 1 test `test_option_text_shows_owner`)

---

### Task 1: Model, migration, ownership-aware matching

**Model:** Sonnet. **Files:**
- Modify: `app.py` — `MaterialPrice` (~line 402), `DeliveryNote` (~1265), `ClientDeliveryNote`/`ClientDeliveryNoteItem` (~1317-1352), `format_material_option()` (~5345), `_find_or_create_delivery_target()` (~10751-10930)
- Create: `migration/migrate_add_client_stock.py`
- Modify: `migration/migrate_add_indexes.py` (append the new index)
- Test: `testing/test_client_stock.py` (create; pytest, copy the fixture shape of `testing/test_delivery_note_edit.py`: temp SQLite, `db.create_all()`, `seed_material_types()`, admin user `qa_admin`, login via `/login`, `WTF_CSRF_ENABLED=False`, `limiter.reset()`)

**Interfaces — Produces:**
- `MaterialPrice.owner_client_id: Integer FK client.id nullable index=True`, `MaterialPrice.owner_client` relationship (`foreign_keys=[owner_client_id]`).
- `DeliveryNote.client_id: Integer FK client.id nullable`, `DeliveryNote.client` relationship.
- `ClientDeliveryNote.kind: String(10) NOT NULL default 'return'`; constant `CLIENT_NOTE_KINDS = {'return': 'Връщане / издаване на клиент', 'consume': 'Изписване за производство'}` next to `REQUEST_STATUS_LABELS`.
- `ClientDeliveryNoteItem.remnant_qty: Float nullable`, `.remnant_width: Float nullable`, `.remnant_height: Float nullable`, `.remnant_material_id: Integer FK material_price.id nullable`.
- `own_materials_query()` → `MaterialPrice.query.filter(MaterialPrice.owner_client_id.is_(None))` (defined right after `format_material_option()`).
- `_find_or_create_delivery_target(..., notes=None, owner_client_id=None)` — new keyword, last.
- `format_material_option(m)` appends `f' [клиент: {m.owner_client.name}]'` when `m.owner_client_id` is set.

- [ ] **Step 1: Write failing tests** in `testing/test_client_stock.py`

```python
def test_matching_separates_owner(admin_client):
    with flask_app.app_context():
        c = Client(name='Клиент А'); db.session.add(c); db.session.commit()
        ours = MaterialPrice.query.filter_by(key='steel').one()
        lot = _find_or_create_delivery_target('material', 'Стомана 2мм', None, 1000, 2000, 2.0, None, 'steel',
                                              material_type='sheets', owner_client_id=c.id)
        assert lot.id != ours.id and lot.owner_client_id == c.id and lot.cost_per_m2 == 0
        assert lot.cutting_speed_mm_per_min == ours.cutting_speed_mm_per_min   # copied from the picked row
        again = _find_or_create_delivery_target('material', 'Стомана 2мм', None, 1000, 2000, 2.0, None, 'steel',
                                                material_type='sheets', owner_client_id=c.id)
        assert again.id == lot.id                                              # second receipt reuses the client lot
        ours_again = _find_or_create_delivery_target('material', 'Стомана 2мм', None, 1000, 2000, 2.0, None, 'steel',
                                                     material_type='sheets')
        assert ours_again.id == ours.id

def test_option_text_shows_owner(admin_client):
    with flask_app.app_context():
        c = Client(name='Клиент А'); db.session.add(c); db.session.flush()
        m = MaterialPrice(key='x', display_name='Мед', cost_per_m2=0, type='sheets', owner_client_id=c.id)
        db.session.add(m); db.session.flush()
        assert format_material_option(m).endswith(' [клиент: Клиент А]')
        assert own_materials_query().filter_by(key='x').first() is None
```

- [ ] **Step 2: Run** `python -m pytest testing/test_client_stock.py -v` — expect FAIL (unexpected keyword `owner_client_id`, no `own_materials_query`).
- [ ] **Step 3: Add the columns/relationships/constant** listed in Interfaces; `_find_or_create_delivery_target`: for `material` include `owner_client_id` in the `picked` tuple comparison and in the `filter_by`, run the fields lookup whenever `existing is None` (drop the `and not material_key` guard), pass `owner_client_id` into `MaterialPrice(...)`; when `owner_client_id` is set ignore `unit_price` (cost 0, `price_per_unit=None`). For `detail`: `filter_by(..., owner_client_id=owner_client_id)`, new `Detail(..., owner_client_id=owner_client_id)`, `calculated_price=unit_price or 0.0` and do not return `None` on missing price when `owner_client_id` is set. For `product`: same owner on lookup and create.
- [ ] **Step 4: Write `migration/migrate_add_client_stock.py`** (docstring in the style of `migrate_add_own_material.py`): `material_price.owner_client_id INTEGER REFERENCES client(id)`, `CREATE INDEX IF NOT EXISTS ix_material_price_owner_client_id ON material_price(owner_client_id)`, `delivery_note.client_id INTEGER REFERENCES client(id)`, `client_delivery_note.kind VARCHAR(10) NOT NULL DEFAULT 'return'`, `client_delivery_note_item.remnant_qty FLOAT`, `.remnant_width FLOAT`, `.remnant_height FLOAT`, `.remnant_material_id INTEGER REFERENCES material_price(id)`. Append the same index line to `migration/migrate_add_indexes.py`.
- [ ] **Step 5: Run** `python -m pytest testing/test_client_stock.py -v` → PASS; then `python -m testing.test_material_option_format`, `python -m testing.test_delivery_note_matching`, `python -m testing.test_delivery_note_price_split`, `python -m pytest testing/test_delivery_note_edit.py -v` → all PASS.

---

### Task 2: Receipt from a client (DeliveryNote with `client_id`)

**Model:** Sonnet. **Files:**
- Modify: `app.py` — `admin_delivery_notes()` (~10934), `create_delivery_note()`/`edit_delivery_note()` (~11019-11108), `_set_delivery_note_header()` (~11147), `_apply_delivery_note_items()` (~11172), `admin_delivery_note_print()` (~11273)
- Modify: `templates/admin_delivery_notes.html` (header form ~lines 62-90, list rows ~370-400, JS `addDeliveryRow`/`dialog-open`), `templates/admin_delivery_note_print.html`
- Test: `testing/test_client_stock.py`

**Interfaces — Consumes:** Task 1. **Produces:**
- Form field `dn_party` (`supplier` | `client`) + selects `supplier_id`, `client_id`; server: `_set_delivery_note_header(note, party_field, lock_party=False)` — with `lock_party=True` only `note_number`/`note_date` change.
- `create_delivery_note()`: both ids posted → flash `'Разписката е или от доставчик, или от клиент - не и двете.'` and redirect; a line that `_find_or_create_delivery_target` rejects → flash `f'Редът "{name}" не беше записан (липсват данни за нов материал).'` (append to a list, flash once, still save the rest).
- `_apply_delivery_note_items(note, items, kept=None)` passes `owner_client_id=note.client_id` and, when `note.client_id`, `unit_price=None` into the matcher and stores `unit_price=None` on the item.
- `admin_delivery_notes()` passes `clients` (ordered by name) to the template; `materials_data` keeps every lot (labels via `format_material_option`).

- [ ] **Step 1: Write failing tests**

```python
def _receipt(client, c_id, lines, note_id=None):
    url = f'/admin/delivery-notes/{note_id}/edit' if note_id else '/admin/delivery-notes/create'
    return client.post(url, data={'client_id': c_id, 'items_json': json.dumps(lines)}, follow_redirects=True)

def test_client_receipt_creates_owner_lot(admin_client):
    # line picked as 'steel' (our lot) with a unit price -> client lot, price ignored, our lot untouched
    ... r = _receipt(admin_client, c.id, [_line(3, 55.0)])
    assert r.status_code == 200
    lot = MaterialPrice.query.filter_by(owner_client_id=c.id).one()
    assert lot.stock_quantity == 3 and lot.cost_per_m2 == 0 and lot.price_per_unit is None
    assert MaterialPrice.query.filter_by(key='steel').one().stock_quantity in (None, 0)
    item = DeliveryNoteItem.query.one(); assert item.unit_price is None and item.target_id == lot.id

def test_client_receipt_from_our_lot_pick(admin_client):   # Review Focus 1
    ... lot = ...; assert lot.cutting_speed_mm_per_min == 2000 and lot.pierce_rate_per_min == 30

def test_client_receipt_both_parties_refused(admin_client):
    r = admin_client.post('/admin/delivery-notes/create', data={'client_id': c.id, 'supplier_id': s.id,
                          'items_json': json.dumps([_line(1, None)])}, follow_redirects=True)
    assert 'не и двете' in r.get_data(as_text=True) and DeliveryNote.query.count() == 0

def test_client_receipt_detail_line(admin_client):
    line = {'type': 'detail', 'name': 'Фланец', 'material_key': 'steel', 'qty': 10, 'unit_price': None}
    ...; d = Detail.query.filter_by(name='Фланец').one()
    assert d.owner_client_id == c.id and d.stock_quantity == 10 and d.calculated_price == 0

def test_client_receipt_edit_keeps_party(admin_client):
    # edit posting supplier_id instead of client_id leaves note.client_id unchanged, supplier stays None
    ...

def test_client_receipt_print(admin_client):
    r = admin_client.get(f'/admin/delivery-notes/{note.id}/print')
    assert r.status_code == 200 and 'ПРИЕМО-ПРЕДАВАТЕЛЕН ПРОТОКОЛ' in r.get_data(as_text=True)
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement the routes** per Interfaces. In `edit_delivery_note()` call `_set_delivery_note_header(note, 'client_id' if note.client_id else 'supplier_id', lock_party=True)`. Log/flash header text: `'Приемане на съхранение №{id} (клиент: …)'` when `note.client_id`, else today's text.
- [ ] **Step 4: Template `admin_delivery_notes.html`**: radio `dn_party` (Доставчик / Клиент (на съхранение)) above the party selects; show one select, clear the other's value on switch and on submit; `client_id` select gets `data-create="{{ create_url('client') }}"` if `'client'` is in `CREATE_NEW_TARGETS`, else plain. List row: party column shows `note.client.name + ' (съхранение)'` when `note.client`. Edit button `data-values` carries `client_id` too; `dialog-open` handler sets the radio from which id is present and disables both party controls when `data-note-id` is set (locked on edit).
- [ ] **Step 5: Print template**: when `note.client`: heading `ПРИЕМО-ПРЕДАВАТЕЛЕН ПРОТОКОЛ № {{ note.id }}`, subheading `приемане на съхранение`, two party blocks (`Предал: {{ note.client.name }}` + `legal_lines(note.client)` / `Приел: ТРАФКОМ ООД`), hide the two price columns and the total row, sign-off with two signature lines `Предал: ………… / Приел: …………`. Supplier branch unchanged.
- [ ] **Step 6: Run** `python -m pytest testing/test_client_stock.py -v` and `python -m pytest testing/test_delivery_note_edit.py -v` → PASS.

---

### Task 3: Consume / return with remnant, reversal, negative-stock guard

**Model:** Opus. **Files:**
- Modify: `app.py` — `_reverse_note_items()` (~11156), `_delete_note()` (~11128), `admin_client_delivery_notes()` (~11286), `create_client_delivery_note()`/`edit_client_delivery_note()` (~11319-11379), `_apply_client_delivery_note_items()` (~11381), `admin_client_delivery_note_print()` (~11428), `edit_delivery_note()`/`delete_delivery_note()` (guard call)
- Modify: `templates/admin_client_delivery_notes.html`, `templates/admin_client_delivery_note_print.html`
- Test: `testing/test_client_stock.py`

**Interfaces — Consumes:** Task 1 columns, `CLIENT_NOTE_KINDS`. **Produces:**
- `_negative_client_lots() -> list[str]`: names (`format_material_option(m)` / `d.name`) of `MaterialPrice`/`Detail` rows with `owner_client_id IS NOT NULL` and `stock_quantity < -PRODUCTION_STOCK_TOLERANCE`. Called after the lines are applied/reversed and before `commit()` in `create/edit/delete_client_delivery_note`, `edit_delivery_note`, `delete_delivery_note` (and `_delete_note`); non-empty → `db.session.rollback()`, flash `f'Отказано: наличността на клиентска партида би станала отрицателна ({", ".join(names)}).'`, redirect.
- items_json line shape for client notes: `{type, target_id, qty, unit_price, notes, remnant_qty, remnant_width, remnant_height}` (remnant keys optional, only honoured when `note.kind == 'consume'` and `type == 'material'`).
- `_apply_client_delivery_note_items(note, items)` returns `(added_any, log_parts, log_detail_lines, errors)` where `errors` is a list of Bulgarian strings; any error → caller rolls back, flashes them, saves nothing. Error cases: consume without `client_id` → `'Изписването изисква клиент.'`; consume line on a row whose `owner_client_id != note.client_id` → `f'"{name}" не е партида на клиента.'`; return line with `owner_client_id` not in `(None, note.client_id)` → `f'"{name}" принадлежи на друг клиент.'`; remnant on a sheet lot (`material_price_unit(src.type) == 'm2'`) without both `remnant_width` and `remnant_height` → `f'Остатъкът от "{name}" трябва да има ширина и дължина.'`.
- Remnant: `m2` lot → `_find_or_create_delivery_target('material', f'{src.display_name} - остатък', src.brand, remnant_width, remnant_height, src.thickness_mm, None, src.key, material_type=src.type, notes=None, owner_client_id=note.client_id)`, then set `lot.height_mm = src.height_mm` if the new row has none, `_bump_stock(lot, remnant_qty)`, `item.remnant_material_id = lot.id`. Linear/pcs lot → `_bump_stock(src, remnant_qty)`, `item.remnant_material_id = src.id`.
- `_reverse_note_items(items, sign)`: additionally, for an item with `remnant_material_id` and `remnant_qty`, `_bump_stock(MaterialPrice.query.get(remnant_material_id), sign * remnant_qty)` (sign is -1 for client items, so the remnant is taken back out).
- `kind` and `client_id` are set on create only (`note.kind = request.form.get('kind') if in CLIENT_NOTE_KINDS else 'return'`); edit ignores both.

- [ ] **Step 1: Write failing tests** (fixture: client A with a 1000×2000×2 lot `stock_quantity=5`, client B with its own lot, our `steel` lot `stock_quantity=4`)

```python
def _out(client, c_id, kind, lines, note_id=None): ...  # POST /admin/client-delivery-notes/create or /<id>/edit

def test_consume_foreign_lot_refused(admin_client): ...   # client A consuming B's lot: 'принадлежи'/'не е партида' in page, stocks unchanged, ClientDeliveryNote.query.count()==0
def test_consume_decrements_and_sheet_remnant_creates_lot(admin_client):
    # qty 2, remnant_qty 1, 600x900 -> A lot 3, new lot 'Стомана 2мм - остатък' owner A stock 1, width 600 height 900 thickness 2, remnant_material_id set
    # second consume with the same remnant dims -> same remnant lot, stock 2
def test_consume_remnant_linear_same_lot(admin_client): ...   # Review Focus 2: rods lot stock 10 (m), consume 3, remnant 1.5 -> 8.5, remnant_material_id == src.id, MaterialPrice count unchanged
def test_consume_sheet_remnant_without_dims_refused(admin_client): ...
def test_consume_over_stock_refused(admin_client): ...        # qty 6 on stock 5 -> 'отрицателна' in page, stock still 5
def test_edit_and_delete_consume_reverse_remnant(admin_client):
    # edit the note to qty 1 no remnant -> A lot 4, remnant lot 0; delete -> A lot 5, remnant lot 0
def test_delete_receipt_after_consume_refused(admin_client): ... # Review Focus 3: receipt 3 -> consume 2 -> DELETE receipt -> refused, lot still 1
def test_return_other_clients_lot_refused_our_stock_ok(admin_client): ...
def test_edit_ignores_kind_and_client(admin_client): ...
def test_client_note_print_titles(admin_client):
    # consume -> 'ПРОТОКОЛ ЗА ИЗПИСВАНЕ'; return -> 'ПРИЕМО-ПРЕДАВАТЕЛЕН ПРОТОКОЛ'
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** per Interfaces. `admin_client_delivery_notes()`: pass `kinds=CLIENT_NOTE_KINDS`, add `owner` (client id or `None`) and `unit` (`material_price_unit(m.type)`) to `materials_data`, `owner` to `details_data`; `note_rows` include the remnant fields. Log texts: consume → `'Изписване за производство №{id} (клиент: …)'`, lines `… -{qty}, остатък {remnant_qty} ({w}x{h})'`.
- [ ] **Step 4: Template `admin_client_delivery_notes.html`**: `kind` select (label `Вид`, options from `kinds`) in the header row; when `consume`: pickers show only options whose `data-owner` equals the selected `client_id` (hide others; re-run on client change and clear `cdnItems` when the client changes with a confirm), a remnant fieldset for material rows (`cdn_remnant_qty`, `cdn_remnant_width`, `cdn_remnant_height`; width/height shown only when the picked option's `data-unit === 'm2'`), table gets an `Остатък` column (`'1 бр. 600×900'` or `'-'`); when `return` behave as today. On edit (`data-note-id`): `kind` and `client_id` disabled. Submit maps the extra keys into items_json.
- [ ] **Step 5: Print template**: title by `note.kind` — `consume`: `ПРОТОКОЛ ЗА ИЗПИСВАНЕ № {{ note.id }}` with columns Наименование / Изразходвано / Остатък (размери, кол.) / Отпадък (`'да'` when no remnant) / Бележка; `return`: `ПРИЕМО-ПРЕДАВАТЕЛЕН ПРОТОКОЛ № {{ note.id }}` + `връщане`, parties `Предал: ТРАФКОМ ООД` / `Приел: {{ note.client.name }}`. Price columns and total only when `note.items|selectattr('unit_price')|list`. Two signature lines in both.
- [ ] **Step 6: Run** `python -m pytest testing/test_client_stock.py -v`, `python -m pytest testing/test_delivery_note_edit.py -v`, `python -m pytest testing/test_offer_item_to_product.py -v` → PASS.

---

### Task 4: Client lots are never "our material"

**Model:** Sonnet. **Files:**
- Modify: `app.py` at the `MaterialPrice.query` sites inside: `generator()` 5573, `admin_bending()` 5970, `_step_material_choices()` 6001, `admin_bending_step_create()` 6079, `flashing()` 7881, `process_dxf_upload()` 9746, `dashboard()` 9789, `upload()` 9886, `admin_details()` 10177, `storage_requests()` 10284, `create_material_request()` 10312, `admin_add_detail()` 12503, `detail_dxf_dashboard()` 12641, `admin_update_detail_material()` 12669, `admin_product_edit()` 13203, `create_order()` 13717, `admin_production_orders()` 13969, `create_production_order()` 14026, `api_quick_create_detail()` 15867, chatbot tools 24598/24632/24652/24675, `admin_delete_client()` ~10455. (Line numbers approximate after Tasks 1-3; grep `MaterialPrice.query` and confirm each.)
- Test: `testing/test_client_stock.py`

**Interfaces — Consumes:** `own_materials_query()` (Task 1). **Produces:** nothing new.

- [ ] **Step 1: Write failing tests**

```python
def test_client_lot_hidden_from_pickers(admin_client):   # as a regular_user and as admin: /dashboard, /order/create, /generator, /flashing pages do not contain the client lot's option text; /storage/materials (admin) does
def test_upload_client_lot_refused(admin_client):        # Review Focus 4: regular user POST /upload with material=<client lot key> + a tiny valid DXF -> 'Невалиден избор на материал' flashed, DxfFile.query.count()==0
def test_chatbot_tools_hide_client_lots(admin_client):   # list_materials(), get_material_details(name), search_catalog(name) as regular user and as admin never mention the client lot
def test_production_orders_skip_client_lots(admin_client):  # GET /admin/production-orders page JSON lacks the client lot id; POST create with material_id=client lot -> flash 'клиентска партида', no ProductionOrder
def test_delete_client_with_lot_refused(admin_client):   # POST /admin/clients/<id>/delete -> 'партиди' in flash, client still exists
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Replace** `MaterialPrice.query` with `own_materials_query()` at every listed site (keep `.order_by`/`.filter_by` chains). Key lookups (`filter_by(key=...)`) in `process_dxf_upload`, `admin_add_detail`, `api_quick_create_detail`, `admin_update_detail_material`, `admin_bending_step_create` go through `own_materials_query().filter_by(key=...)` so a client lot resolves to `None` and hits the existing "invalid material" branch. `create_production_order()`: `material = own_materials_query().filter_by(id=…).first()`; `None` → flash `'Клиентска партида не може да се ползва за производствена задача.'`. `admin_delete_client()`: add `(MaterialPrice, MaterialPrice.owner_client_id, 'партиди материал на съхранение'), (DeliveryNote, DeliveryNote.client_id, 'приемания на съхранение')` to `used_in`. Leave `storage_materials`, `admin_materials`, `admin_material_history`, both note pages, `erp_lookup`, `print_label`, `admin_update_material`, `admin_delete_material`, `seed_material_prices`, pricing functions (`calculate_*`, key lookups at 5025/5051/5070 — they must still price a client lot's geometry for the receipt's detail line) untouched.
- [ ] **Step 4: Run** `python -m pytest testing/test_client_stock.py -v`, `python -m pytest testing/test_access_hardening.py -v`, `python -m pytest testing/test_production_order_clamp.py -v`, `python -m pytest testing/test_smoke_all_pages.py -v` → PASS.

---

### Task 5: Owner in stock lists and lot history

**Model:** Sonnet. **Files:**
- Modify: `app.py` — `storage_materials()` (~10237), `admin_materials()` (~10006), `admin_material_history()` (~10102)
- Modify: `templates/storage_materials.html`, `templates/admin_materials.html`, `templates/admin_material_history.html`
- Test: `testing/test_client_stock.py`

**Interfaces — Consumes:** Task 1 columns, Task 3 `remnant_material_id`. **Produces:**
- `storage_materials()` reads `request.args.get('owner', 'all')`: `'own'` → `owner_client_id IS NULL`, `'client:<id>'` → that client, else all; passes `clients`, `owner_filter`; grouping key tuple gains `material.owner_client_id`; `group['total_value']` and the page's value sum exclude client lots.
- `admin_materials()` passes `clients`; each `<tr>` gets `data-owner="{{ material.owner_client_id or 'own' }}"`; new `filterOwner` select wired into the existing JS filter chain; the "Стойност на склад (€)" summary skips rows with `data-owner != 'own'`.
- `admin_material_history()` movement `kind` values: `delivery` (supplier), `client_receipt` (DeliveryNote with `client_id`, party = client name), `return` / `consume` (from `ClientDeliveryNote.kind`), `remnant` (direction `in`, `ClientDeliveryNoteItem.remnant_material_id == material_id`, quantity `remnant_qty`, party = client, notes `'остатък от "{description_snapshot}"'`), `production_take`/`production_return` unchanged. Template labels: Доставка / Приемане на съхранение / Връщане на клиент / Изписано за производство / Остатък / Производство.

- [ ] **Step 1: Write failing tests**

```python
def test_storage_owner_filter(admin_client):   # ?owner=own hides the client lot, ?owner=client:<id> hides ours, default shows both with 'Собственик' column text
def test_history_kinds(admin_client):          # after receipt + consume with remnant: source lot history has 'Приемане на съхранение' and 'Изписано за производство'; remnant lot history has 'Остатък'
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** per Interfaces (`storage_materials.html`: a `<select>` next to the existing search, `onchange` navigates to `?owner=…`; a `Собственик` column after `Материал`).
- [ ] **Step 4: Run** `python -m pytest testing/test_client_stock.py -v`, `python -m testing.test_storage_materials_thresholds`, `python -m pytest testing/test_query_counts.py -v` → PASS.

---

### Task 6: Documentation

**Model:** Haiku. **Files:** `CLAUDE.md`

- [ ] **Step 1:** In "Stock tracking" add one paragraph: ownership (`MaterialPrice.owner_client_id`, same as Detail/Product), the three documents (`DeliveryNote.client_id` receipt, `ClientDeliveryNote.kind` return/consume, remnant fields and `remnant_material_id`), `own_materials_query()` for every "our material" picker, `_negative_client_lots()` guard, protocols printed from the existing print views. Add `migration/migrate_add_client_stock.py` to the migration list and `testing/test_client_stock.py` to the tests list (one line each, same style as neighbours).
- [ ] **Step 2:** Run `python -m testing.run_all -j 4` and report failures (do not fix unrelated ones; list them).
