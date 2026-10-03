"""
One-off, safe to re-run: puts the specs found for the checked hall machines into the machine cards (ServiceMachineCard):
  * DURMA AD-R 40175 - merges the missing catalog data (Durmazlar "AD-R Series Technical Details": overall size, weight,
    columns, stroke, throat, table, speeds, oil tank) into its existing card, existing lines are kept untouched;
  * Polymeta-C LS3015 300 kg vacuum sheet loader - creates a card (page 'hall': shown only in the 3D hall editor/page, never
    on the public services/index pages) from the supplier brochure and links it to the hall machine that uses the
    'sheet_lift' model and has no card yet, renaming that machine;
  * Aore Laser PG3015 fibre laser with enclosure (hall machine no. 12) - card from the manufacturer's page and dealer listings;
  * BENZINGER TNI-B8 (hall machine no. 4) - missing electrical / spindle / Y-axis lines merged into its card;
  * hall machine no. 11 is named 'Компресор за сгъстен въздух EM15H' (no data found for that model yet);
  * LT1500-C-6 (SZGH-T1500-C-6) 6-axis robot manipulator (hall machine no. 17) - card from the manufacturer's page, model 'robot';
  * DURMA AD-ES 2040 electric (servo) press brake (hall machine no. 13) - card + overall size from the AD-ES catalog; the machine
    is set to the catalog size (centre kept) and the 'press' model.

    python -m migration.seed_hall_machine_specs
"""
from app import app, db, HallMachine, ServiceMachineCard, _card_dims

BENZINGER_ADD = [                                 # Carl Benzinger TNI-B8: dealer/catalog listings (no overall size or weight found)
    ('Налични диаметри на шпиндела', '32 - 42 мм'),
    ('Ход на оста Y', '70 мм (под револвера, за главния и насрещния шпиндел)'),
    ('Захранване', '3 фази, 400 V, 50 Hz'),
    ('Макс. обща мощност', '20 kW'),
    ('Макс. ток', '25 A'),
    ('Управляващо напрежение', '24 V DC'),
]
DURMA_ADD = [
    ('Габарити (Д x Ш x В)', '5250 x 1700 x 2850 мм'),
    ('Тегло', '≈ 12 780 кг'),
    ('Разстояние между колоните', '3600 мм'),
    ('Ход на буталото', '265 мм'),
    ('Дълбочина на гърлото', '450 мм'),
    ('Височина на масата', '900 мм'),
    ('Широчина на масата', '104 / 240 мм'),
    ('Бързо движение (Y)', '160 мм/с'),
    ('Работна скорост (Y)', '10 мм/с'),
    ('Скорост на връщане (Y)', '140 мм/с'),
    ('Точност на оста Y', '0,01 мм'),
    ('Маслен резервоар', '250 л'),
    ('Задна опора X (стандарт)', '650 мм'),
    ('Ход Z (странично движение на опората)', '2910 мм'),
    ('Ход R (ръчно)', '140 мм'),
]
LS3015 = {
    'title': 'LS3015 вакуумен подемник за листове (300 кг)',
    'series_label': 'ПОДЕМНИК ЗА ЛИСТОВ МАТЕРИАЛ',
    'description': 'Г-образен колонен вакуумен товарач на Полимета-С (polymeta.bg) за зареждане на метални листове върху работната маса на '
                   'лазерна машина. Листът се хваща с вакуумни вендузи; има предпазна система срещу внезапно спиране на захранването, а '
                   'навесната система разпределя равномерно теглото при придвижване на листа.',
    'specs_text': '\n'.join([
        'Товароносимост: 300 кг',
        'Макс. размер на листа: 3000 x 1500 мм',
        'Мин. размер на листа: 1250 x 1250 мм',
        'Дължина на рамата: 3600 мм',
        'Дебелина на листа: 1 - 8 мм',
        'Налягане на въздуха: 6 бара',
        'Тегло бруто: 435 кг',
        'Доставчик: Полимета-С (polymeta.bg)',
    ]),
}

AORE = {
    'title': 'Aore Laser PG3015',
    'series_label': 'ФАЙБЪР ЛАЗЕР СЪС ЗАТВОРЕНА КАБИНА',
    'description': 'Листов влакнест лазер с пълна защитна кабина (CE) и автоматична смяна на две палетни маси. Данни от aorelaser.com и обяви '
                   'на търговци (PG3015-T6, 3 kW) - габаритите, теглото и ходовете са по обява, не от официален каталог.',
    'specs_text': '\n'.join([
        'Работна площ: 3050 x 1530 мм',
        'Позиционна точност: ± 0,03 мм',
        'Повторяемост: ± 0,02 мм',
        'Макс. скорост (X/Y): до 120 м/мин',
        'Макс. ускорение: 1,5 G',
        'Мощност на лазера: 1,5 - 20 kW (според версията)',
        'Ход X / Y / Z: 2100 / 3000 / 315 мм',
        'Смяна на палети: 2 маси, до 15 сек, до 1000 кг на маса',
        'Габарити (Д x Ш x В): 8635 x 3920 x 2350 мм',
        'Тегло: ≈ 6400 кг',
        'Рязане при 3 kW: стомана 20 мм, неръждаема 10 мм, алуминий 10 мм, месинг 6 мм, мед 5 мм',
    ]),
}

ROBOT = {
    'title': 'LT1500-C-6 роботизиран манипулатор',
    'series_label': 'РОБОТИЗИРАН МАНИПУЛАТОР (6 ОСИ)',
    'description': 'Шестосен ставен робот LT1500-C-6 с AC серво задвижване за зареждане/разреждане на машини и манипулация. '
                   'Данни от ръководството на производителя (Robot Machinery, Betrun система за управление).',
    'specs_text': '\n'.join([
        'Товароносимост: 10 кг',
        'Максимален радиус на движение: 1500 мм',
        'Конструкция: ставен тип, 6 степени на свобода, AC серво задвижване',
        'Повторяемост: ± 0,05 мм',
        'Обхват J1 / J2 / J3: ±170° / +120° до -75° / +165° до -100°',
        'Обхват J4 / J5 / J6: ±185° / ±130° / ±360°',
        'Скорост J1 / J2 / J3: 150 / 150 / 225 °/с',
        'Скорост J4 / J5 / J6: 225 / 225 / 360 °/с',
        'Въртящ момент на китката J4 / J5 / J6: 16,6 / 16,6 / 9,4 N·m',
        'Момент на инерция на китката J4 / J5 / J6: 0,47 / 0,47 / 0,15 kg·m²',
        'Тегло на тялото: около 160 кг',
        'Монтаж: подов или окачен (8 винта M14 x 55)',
        'Основа: плоча 340 x 340 мм (отвори 8 x Ø16, разстояние 260 мм)',
        'Клас на защита: IP65',
        'Работна температура: 0 - 45 °C',
    ]),
}
ROBOT_SIZE = (0.4, 0.4, 1.8)                      # base x base x height used by the 3D model (arm ~ 0.87 x height of reach)

EM15H = {
    'title': 'Винтов компресор EM15H (15 kW, 400 л)',
    'series_label': 'КОМПРЕСОР ЗА СГЪСТЕН ВЪЗДУХ',
    'description': 'Винтов компресор със съосно задвижване, хладилна сушилня, филтри и въздушен резервоар 400 л. '
                   'Данни от Ръководство за работа и поддръжка EM11H / EM15H / EM22HV (Полимета-С).',
    'specs_text': '\n'.join([
        'Мощност на мотора: 15 kW, 3000 об/мин, честотно-променлив старт',
        'Макс. налягане: до 1,55 MPa (зададено 1,50 MPa)',
        'Макс. капацитет: 1,3 m³/мин',
        'Резервоар за въздух: 400 л',
        'Охлаждане: въздушно, съосно задвижване',
        'Ниво на шума: 62 ± 3 dB(A)',
        'Масло: специално за високо налягане, 10 л; съдържание в изходния въздух ≤ 3 ppm',
        'Захранване: 380 V, 50 Hz, 3 фази (ток 28 A, автоматичен прекъсвач 60 A)',
        'Вентилатор: 250 W, 1400 об/мин',
        'Температура на входа: ≤ 40 °C',
        'Температура на изхода на компресора: 90 ± 2 °C',
        'Налягане на празен ход / включване: 1,58 / 1,30 MPa',
        'Присъединяване: G3/4" (въздух и резервоар)',
        'Габарити (Д x Ш x В): 1780 x 790 x 1605 мм',
        'Нетно тегло: 550 кг',
    ]),
}

ES = {
    'title': 'DURMA AD-ES 2040',
    'series_label': 'ЕЛЕКТРИЧЕСКА АБКАНТ ПРЕСА (СЕРВО)',
    'description': 'Абкант преса със серво задвижване (без хидравлика) на Durma, серия AD-ES. Данни от каталога Durma AD-ES.',
    'specs_text': '\n'.join([
        'Усилие на сгъване: 40 тона',
        'Дължина на сгъване: 2050 мм',
        'Разстояние между колоните: 2050 мм',
        'Ход на буталото: 200 мм',
        'Светъл отвор: 440 мм',
        'Работна височина: 1000 мм',
        'Скорост на приближаване: 120 - 180 мм/с',
        'Скорост на сгъване: 20 - 40 мм/с (по CE макс. 10 мм/с, освен при роботизирана работа)',
        'Задна опора: X 650 мм, R 250 мм',
        'Мощност на двигателя: 7,2 kW',
        'Габарити (Д x Ш x В): 2870 x 1625 x 2800 мм',
        'Тегло: 5600 кг',
    ]),
}
ES_SIZE = (2.87, 1.625, 2.8)                      # catalog L x W x H, m

# placeholder pictures taken from the manufacturers' brochures/site (replace with real photos of the machines later)
IMAGES = {'LS3015 вакуумен подемник за листове (300 кг)': 'polymeta-ls3015.jpg', 'Aore Laser PG3015': 'aore-pg3015.jpg',
          'DURMA AD-ES 2040': 'durma-ad-es-2040.jpg', 'LT1500-C-6 роботизиран манипулатор': 'robot-lt1500-c-6.jpg',
          'Винтов компресор EM15H (15 kW, 400 л)': 'compressor-em15h.jpg'}

with app.app_context():
    card = ServiceMachineCard.query.filter(ServiceMachineCard.title.ilike('%AD-R 40175%')).first()
    if card:
        have = {l.partition(':')[0].strip() for l in (card.specs_text or '').splitlines() if ':' in l}
        add = [f'{k}: {v}' for k, v in DURMA_ADD if k not in have]
        card.specs_text = '\n'.join(([card.specs_text.rstrip()] if card.specs_text else []) + add)
        print(f'DURMA AD-R 40175: {len(add)} lines added to card {card.id}')
    else:
        print('DURMA AD-R 40175 card not found - skipped')

    loader = ServiceMachineCard.query.filter_by(title=LS3015['title']).first()
    if not loader:
        loader = ServiceMachineCard(page='hall', kind='machine', **LS3015)
        db.session.add(loader)
        db.session.flush()
        print('LS3015 card created')
    hm = HallMachine.query.filter_by(model='sheet_lift', card_id=None).order_by(HallMachine.no).first()
    if hm:
        hm.card_id, hm.name = loader.id, 'Вакуумен подемник LS3015 (300 кг)'
        print(f'hall machine #{hm.no} linked to the LS3015 card')
    aore = ServiceMachineCard.query.filter_by(title=AORE['title']).first()
    if not aore:
        aore = ServiceMachineCard(page='hall', kind='machine', **AORE)
        db.session.add(aore)
        db.session.flush()
        print('Aore PG3015 card created')
    laser = HallMachine.query.filter_by(no=12, model='laser_cabin').first()
    if laser and laser.card_id != aore.id:                       # first run only - later edits are kept
        laser.card_id, laser.name = aore.id, 'Aore Laser PG3015'
        print('hall machine #12 renamed and linked to the Aore card')
    bz = ServiceMachineCard.query.filter(ServiceMachineCard.title.ilike('%TNI-B8%')).first()
    if bz:
        have = {l.partition(':')[0].strip() for l in (bz.specs_text or '').splitlines() if ':' in l}
        add = [f'{k}: {v}' for k, v in BENZINGER_ADD if k not in have]
        if add:
            bz.specs_text = (bz.specs_text.rstrip() + chr(10) if bz.specs_text else '') + chr(10).join(add)
        print(f'BENZINGER TNI-B8: {len(add)} lines added to card {bz.id}')
    robot = ServiceMachineCard.query.filter_by(title=ROBOT['title']).first()
    if not robot:
        robot = ServiceMachineCard(page='hall', kind='machine', **ROBOT)
        db.session.add(robot)
        db.session.flush()
        print('LT1500-C-6 card created')
    robot.specs_text, robot.description, robot.series_label = ROBOT['specs_text'], ROBOT['description'], ROBOT['series_label']
    arm = HallMachine.query.filter_by(no=17).first()
    if arm and arm.card_id != robot.id:                           # first run only - later edits are kept
        cx, cz = arm.x + arm.width / 2, arm.z + arm.depth / 2
        arm.width, arm.depth, arm.height = ROBOT_SIZE
        arm.x, arm.z = round(cx - arm.width / 2, 3), round(cz - arm.depth / 2, 3)
        arm.model, arm.category, arm.card_id, arm.name = 'robot', 'util', robot.id, 'Робот манипулатор LT1500-C-6'
        print('hall machine #17 set to the LT1500-C-6 robot (size, model, card)')
    comp = HallMachine.query.filter_by(no=11, model='compressor').first()
    if comp and comp.name == 'Нова машина':                       # name only - no specs found for the EM15H yet
        comp.name = 'Компресор за сгъстен въздух EM15H'
        print('hall machine #11 renamed (EM15H compressor)')
    if arm and arm.model == 'robot':                              # the manual shows a green robot
        arm.model = 'robot_green'
    em = ServiceMachineCard.query.filter_by(title=EM15H['title']).first()
    if not em:
        em = ServiceMachineCard(page='hall', kind='machine', **EM15H)
        db.session.add(em)
        db.session.flush()
        print('EM15H card created')
    if comp and comp.card_id != em.id:                            # first run only - later edits are kept
        cx, cz = comp.x + comp.width / 2, comp.z + comp.depth / 2
        length, width, height = _card_dims(em)
        comp.width, comp.depth = (width, length) if comp.rotation % 180 else (length, width)
        comp.height = height
        comp.x, comp.z = round(cx - comp.width / 2, 3), round(cz - comp.depth / 2, 3)
        comp.card_id, comp.model = em.id, 'compressor'
        print('hall machine #11 linked to the EM15H card (size from the card)')
    es = ServiceMachineCard.query.filter_by(title=ES['title']).first()
    if not es:
        es = ServiceMachineCard(page='hall', kind='machine', **ES)
        db.session.add(es)
        db.session.flush()
        print('AD-ES 2040 card created')
    brake = HallMachine.query.filter_by(no=13).first()
    if brake and brake.card_id != es.id:                          # first run only - later edits are kept
        cx, cz = brake.x + brake.width / 2, brake.z + brake.depth / 2
        brake.width, brake.depth, brake.height = ES_SIZE
        brake.x, brake.z = round(cx - brake.width / 2, 3), round(cz - brake.depth / 2, 3)
        brake.model, brake.category, brake.card_id, brake.name = 'press', 'press', es.id, 'Абкант DURMA AD-ES 2040'
        print('hall machine #13 set to the AD-ES 2040 (size, model, card)')
    for title, filename in IMAGES.items():
        c = ServiceMachineCard.query.filter_by(title=title).first()
        if c and not c.image_filename:
            c.image_filename = filename
            print(f'{title}: picture {filename}')
    db.session.commit()
