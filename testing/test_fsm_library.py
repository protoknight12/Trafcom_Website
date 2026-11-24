"""
CypCut library technologies (.fsm, owner's request 2026-11-24): fscut.read_fsm() / write_fsm() and the dossier's
import (several .fsm at once) / export (one .fsm, all as a ZIP).

An .fsm is a one-member ZIP (material.lcm, a <Material> XML) encrypted with ZipCrypto; CypCut opens only an
encrypted one, so the writer must produce what the stdlib zipfile decrypts with the password and refuses without it.
The real password is not in the repository - the tests use their own.

Run: pytest testing/test_fsm_library.py -v
"""
import io
import zipfile

import pytest
from flask import g
from werkzeug.security import generate_password_hash

import fscut
import app as app_module
from app import (app as flask_app, db, User, Machine, MachineCutTechnology, HallMachine, MaterialGrade, limiter,
                 seed_material_types)

PW = 'test-only-password'
UNC = r'\\192.168.18.140\technology'
MATERIAL = ('<?xml version="1.0" encoding="utf-8"?>\r\n'
            '<Material ChannelPort="1" ParamFlag="768" OEM="1" OEMCode="42">'
            '<CamParams WorkSpeed="{speed}" PierceStepCount="1" LiftHeight="30">'
            '<Cut Focus="{focus}" GasPressure=".5" GasType="{gas}" Height=".6" PeakPower="6000"/>'
            '<Pierce1 Focus="-6" GasType="2" Height="12" TimeFollow="1000" TimeStay="150"/></CamParams>'
            '<MaterialName>4mm碳钢-双层1.2.fsm氧气.fsm</MaterialName>'
            '<MaterialParams MaterialID="0" MaterialName="硅钢" NozzleSpec="默认" Thickness="3"/><Note/></Material>')


def _fsm(speed=45, focus=11, gas=2, password=PW):
    return fscut._zipcrypto(fscut.FSM_MEMBER, MATERIAL.format(speed=speed, focus=focus, gas=gas).encode(), password)


def test_zipcrypto_is_what_zipfile_decrypts():
    data = _fsm()
    zf = zipfile.ZipFile(io.BytesIO(data))
    info = zf.getinfo('material.lcm')
    assert info.flag_bits & 1 and info.compress_type == zipfile.ZIP_DEFLATED
    assert zf.read('material.lcm', pwd=PW.encode()).startswith(b'<?xml')
    with pytest.raises(RuntimeError):
        zipfile.ZipFile(io.BytesIO(data)).read('material.lcm', pwd=b'wrong')


def test_read_fsm_takes_the_file_name():
    ch = fscut.read_fsm(_fsm(), PW, '8mmCS-D1.2-O2.fsm')
    # thickness / nozzle from the name, not the stale MaterialName / MaterialParams inside
    assert (ch['library_thickness'], ch['library_nozzle'], ch['gas']) == (8.0, '1.2d', 'oxygen')
    assert ch['speed_mm_min'] == 2700 and ch['focus_mm'] == 11 and ch['nozzle'] is None       # 默认 = no nozzle
    assert ch['raw'][0] == ['@', {'ChannelPort': '1', 'ParamFlag': '768', 'OEM': '1', 'OEMCode': '42'}, '']
    assert fscut.library_name_parts('10mm SS-N2 S4.fsm') == (10.0, '4s')
    assert fscut.library_name_parts('1mm CS S2.0.fsm') == (1.0, '2.0s')
    assert fscut.library_name_parts('8mm1.2d1.5d_D.fsm') == (8.0, '1.2d/1.5d')                # old style unchanged
    # no gas in the file (GasType 0): from the name; none there either -> None
    assert fscut.read_fsm(_fsm(gas=0), PW, '25mmCS-D1.6-O2.fsm')['gas'] == 'oxygen'
    assert fscut.read_fsm(_fsm(gas=0), PW, '10mmSS-S3.5.fsm')['gas'] is None
    assert fscut.read_fsm(_fsm(gas=0), PW, '1mm CS AIR S2.0.fsm')['gas'] == 'air'
    for bad in (_fsm(password='other'), b'not a zip', fscut._zipcrypto('other.xml', b'<a/>', PW),
                fscut._zipcrypto('material.lcm', b'<Other/>', PW)):
        with pytest.raises(ValueError):
            fscut.read_fsm(bad, PW, 'x.fsm')


def test_write_fsm_round_trip():
    ch = fscut.read_fsm(_fsm(gas=0), PW, '25mmCS-D1.6-O2.fsm')
    ch['fields']['focus_mm'] = 17
    out = fscut.write_fsm(ch, PW, name='25mm1.6d_O2.fsm', thickness=25)
    back = fscut.read_fsm(out, PW, '25mm1.6d_O2.fsm')
    assert back['fields']['focus_mm'] == 17 and back['gas'] == 'oxygen' and back['speed_mm_min'] == 2700
    assert back['raw'][0][1]['OEMCode'] == '42'                                     # <Material> attributes kept
    xml = zipfile.ZipFile(io.BytesIO(out)).read('material.lcm', pwd=PW.encode()).decode()
    assert '<MaterialName>25mm1.6d_O2.fsm</MaterialName>' in xml and 'Thickness="25"' in xml
    assert xml.index('<CamParams') < xml.index('<MaterialName') < xml.index('<MaterialParams')
    assert '\r\n' in xml and 'GasType="2"' in xml
    # a technology that never came from an .fsm (no '@' row) still gets CypCut's <Material> attributes
    plain = fscut.write_fsm({'speed_mm_min': 1200, 'gas': 'nitrogen', 'fields': {'focus_mm': -3}}, PW)
    p = fscut.read_fsm(plain, PW, '3mm2.0s_N2.fsm')
    assert p['raw'][0][1]['OEMCode'] == fscut.FSM_ROOT_ATTRS['OEMCode'] and p['gas'] == 'nitrogen'
    assert p['library_thickness'] == 3 and p['library_nozzle'] == '2.0s'


def test_export_name_has_the_material_and_reads_back():
    """The exported name says the material (owner, 2026-11-24): "8mmCS-D1.2-O2.fsm", like the machine maker's."""
    from types import SimpleNamespace as NS
    from app import _fsm_name
    cases = [(NS(name='Черна стомана'), 8, '1.2d', 'oxygen', '8mmCS-D1.2-O2.fsm', '1.2d'),
             (NS(name='Неръждаема стомана'), 10, '4s', 'nitrogen', '10mmSS-S4-N2.fsm', '4s'),
             (NS(name='Алуминий'), 3, '1.2d/1.5d', 'air', '3mmAL-D1.2-D1.5-AIR.fsm', '1.2d/1.5d'),
             (NS(name='Поцинкована стомана'), 1.5, '1.4E', None, '1.5mmGI-E1.4.fsm', '1.4E'),
             (None, 8, None, 'oxygen', '8mm-O2.fsm', None)]
    for grade, t, nozzle, gas, name, back in cases:
        got = _fsm_name(NS(grade=grade, thickness_mm=t, nozzle=nozzle, gas=gas))
        assert got == name, got
        assert fscut.library_name_parts(got) == (t, back), got


@pytest.fixture
def s(monkeypatch):
    monkeypatch.setattr(app_module, 'FSCUT_FSM_PASSWORD', PW)
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    limiter.reset()
    with flask_app.app_context():
        db.create_all()
        seed_material_types()
        pw = generate_password_hash('irrelevant123')
        db.session.add_all([User(username='fsm_admin', password=pw, role='admin'),
                            User(username='fsm_worker', password=pw, role='worker')])
        db.session.commit()
        yield
        db.session.remove()
        db.drop_all()


def _login(username):
    g.pop('_login_user', None)
    c = flask_app.test_client()
    c.post('/login', data={'username': username, 'password': 'irrelevant123'})
    return c


def test_dossier_import_and_export(s, monkeypatch):
    laser = Machine(name='PG3015 FSM', is_laser=True)
    db.session.add(laser)
    db.session.flush()
    hall = HallMachine(name=laser.name, machine_id=laser.id)
    grade = MaterialGrade(name='Черна стомана FSM', density_kg_dm3=7.85)
    db.session.add_all([hall, grade])
    db.session.commit()
    c = _login('fsm_admin')
    url = f'/admin/hall/{hall.id}/cut-tech/import'
    files = [(io.BytesIO(_fsm(speed=45, focus=11)), '8mmCS-D1.2-O2.fsm'),
             (io.BytesIO(_fsm(speed=40, focus=12)), '10mmCS-D1.2-O2.fsm'),
             (io.BytesIO(_fsm(password='other')), '12mmCS-D1.2-O2.fsm'),          # unreadable: listed, skipped
             (io.BytesIO(_fsm(gas=0)), '5mmSS-S3.5.fsm')]                          # no gas anywhere: skipped
    r = c.post(url, data={'grade_id': grade.id, 'file': files}, content_type='multipart/form-data',
               follow_redirects=True)
    page = r.get_data(as_text=True)
    assert 'Внесени технологии: 2.' in page and '12mmCS-D1.2-O2.fsm' in page and '5mmSS-S3.5.fsm' in page
    rows = sorted((t.thickness_mm, t.gas, t.nozzle, t.cutting_speed_mm_per_min, t.focus_mm, t.source)
                  for t in MachineCutTechnology.query.filter_by(machine_id=laser.id))
    assert rows == [(8, 'oxygen', '1.2d', 2700, 11, '8mmCS-D1.2-O2.fsm'),
                    (10, 'oxygen', '1.2d', 2400, 12, '10mmCS-D1.2-O2.fsm')]
    assert 'Изтегли всички като .fsm' in page

    t = MachineCutTechnology.query.filter_by(machine_id=laser.id, thickness_mm=8).one()
    r = c.get(f'/admin/hall/{hall.id}/cut-tech/{t.id}/fsm')
    assert r.status_code == 200 and '8mmCS-D1.2-O2.fsm' in r.headers['Content-Disposition']
    back = fscut.read_fsm(r.data, PW, '8mmCS-D1.2-O2.fsm')
    assert back['speed_mm_min'] == 2700 and back['focus_mm'] == 11 and back['raw'][0][1]['OEMCode'] == '42'
    assert (back['library_thickness'], back['library_nozzle'], back['gas']) == (8, '1.2d', 'oxygen')

    z = zipfile.ZipFile(io.BytesIO(c.get(f'/admin/hall/{hall.id}/cut-tech/fsm.zip').data))
    assert sorted(z.namelist()) == ['Черна стомана FSM Кислород/10mmCS-D1.2-O2.fsm',
                                    'Черна стомана FSM Кислород/8mmCS-D1.2-O2.fsm']
    assert fscut.read_fsm(z.read('Черна стомана FSM Кислород/10mmCS-D1.2-O2.fsm'), PW, 'x.fsm')['focus_mm'] == 12

    # admin only; without a password the feature is off
    w = _login('fsm_worker')
    assert w.get(f'/admin/hall/{hall.id}/cut-tech/{t.id}/fsm').status_code == 302
    monkeypatch.setattr(app_module, 'FSCUT_FSM_PASSWORD', None)
    c = _login('fsm_admin')
    assert 'Изтегли всички като .fsm' not in c.get(f'/admin/hall/{hall.id}/dossier').get_data(as_text=True)
    r = c.post(url, data={'grade_id': grade.id, 'file': [(io.BytesIO(_fsm()), '6mmCS-D1.2-O2.fsm')]},
               content_type='multipart/form-data', follow_redirects=True)
    assert 'FSCUT_FSM_PASSWORD' in r.get_data(as_text=True)
    assert c.get(f'/admin/hall/{hall.id}/cut-tech/{t.id}/fsm').status_code == 302
    assert MachineCutTechnology.query.filter_by(machine_id=laser.id).count() == 2


def test_folder_grade_guess():
    from types import SimpleNamespace as NS
    from app import _folder_grade_id
    grades = [NS(id=1, name='Стомана'), NS(id=2, name='Неръждаема стомана'), NS(id=3, name='Алуминий'),
              NS(id=4, name='Месинг')]
    for folder, gid in (('steel', 1), ('ss', 2), ('al', 3), ('mesing', 4), ('CS O2 氧气碳钢', 1),
                        ('SS N2 不锈钢氮气', 2), ('6000W切割参数（板机）\\al', 3), ('class', None), ('', None)):
        assert _folder_grade_id(folder, grades) == gid, folder


def test_machine_tech_folder(s, monkeypatch):
    """The laser's technology folder (SMB, faked here): saved in "Система" with an encrypted password that is never
    shown; the folder page lists sub-folders with a guessed Вид; import of the checked ones; export into Trafcom\."""
    from cryptography.fernet import Fernet
    fernet = Fernet(Fernet.generate_key())
    monkeypatch.setattr(app_module, '_network_api_fernet', lambda: fernet)
    disk = {'steel': {'8mm1.2d1.5d.fsm': _fsm(speed=45, focus=13), '10mm1.2d1.5d.fsm': _fsm(speed=33, focus=11)},
            'ss': {'2mm1.5s2.0s.fsm': _fsm(speed=500, focus=-1, gas=3)}}
    written = {}
    monkeypatch.setattr(app_module, '_tech_folder_files', lambda mc: {k: sorted(v) for k, v in disk.items()})
    monkeypatch.setattr(app_module, '_tech_folder_read', lambda mc, rel, name: disk[rel][name])
    monkeypatch.setattr(app_module, '_tech_folder_write', lambda mc, rel, name, data: written.update({f'{rel}\\{name}': data}))
    laser = Machine(name='PG3015 DIR', is_laser=True)
    db.session.add(laser)
    db.session.flush()
    hall = HallMachine(name=laser.name, machine_id=laser.id)
    db.session.add_all([hall, MaterialGrade(name='Стомана'), MaterialGrade(name='Неръждаема стомана')])
    db.session.commit()
    c = _login('fsm_admin')
    sysurl = f'/admin/hall/{hall.id}/dossier/system'
    base = {'status': 'idle', 'is_laser': '1'}
    r = c.post(sysurl, data=dict(base, tech_folder='not a path'), follow_redirects=True)
    assert 'мрежов път' in r.get_data(as_text=True)
    c.post(sysurl, data=dict(base, tech_folder=UNC + '\\', tech_folder_user='cnc', tech_folder_password='s3cret-pass'))
    mc = db.session.get(Machine, laser.id)
    assert mc.tech_folder == UNC and mc.tech_folder_user == 'cnc'
    assert mc.tech_folder_password_encrypted and 's3cret' not in mc.tech_folder_password_encrypted
    assert fernet.decrypt(mc.tech_folder_password_encrypted.encode()).decode() == 's3cret-pass'
    page = c.get(f'/admin/hall/{hall.id}/dossier').get_data(as_text=True)
    assert 's3cret' not in page and 'записана ✓' in page and 'Папка с технологии на машината' not in page   # no techs yet
    c.post(sysurl, data=dict(base, tech_folder=UNC, tech_folder_user='cnc'))
    db.session.expire_all()
    assert db.session.get(Machine, laser.id).tech_folder_password_encrypted                 # blank = unchanged

    page = c.get(f'/admin/hall/{hall.id}/cut-tech/folder').get_data(as_text=True)
    steel_id = MaterialGrade.query.filter_by(name='Стомана').one().id
    assert 'steel' in page and f'<option value="{steel_id}" selected' in page
    # import only the steel folder (index 2 = 'steel' in sorted order: ss, steel)
    c.post(f'/admin/hall/{hall.id}/cut-tech/folder/import', data={'use_2': '1', 'rel_2': 'steel', 'grade_2': steel_id})
    rows = sorted((t.thickness_mm, t.focus_mm, t.grade_id, t.source)
                  for t in MachineCutTechnology.query.filter_by(machine_id=laser.id))
    assert rows == [(8, 13, steel_id, UNC + '\\steel\\8mm1.2d1.5d.fsm'),
                    (10, 11, steel_id, UNC + '\\steel\\10mm1.2d1.5d.fsm')]
    page = c.get(f'/admin/hall/{hall.id}/dossier').get_data(as_text=True)
    assert 'Папка с технологии на машината' in page and 'id="ctTree"' in page and 'data-grade="Стомана"' in page

    # both came from the folder (linked to their files): "Запиши в папката" leaves them to "Обнови файла"
    r = c.post(f'/admin/hall/{hall.id}/cut-tech/folder/export', follow_redirects=True)
    assert written == {} and '2 вече имат файл' in r.get_data(as_text=True)
    MachineCutTechnology.query.update({'folder_file': None})
    db.session.commit()
    c.post(f'/admin/hall/{hall.id}/cut-tech/folder/export')
    assert sorted(written) == ['Trafcom\\Стомана Кислород\\10mmCS-D1.2-D1.5-O2.fsm',
                               'Trafcom\\Стомана Кислород\\8mmCS-D1.2-D1.5-O2.fsm']
    assert fscut.read_fsm(written['Trafcom\\Стомана Кислород\\8mmCS-D1.2-D1.5-O2.fsm'], PW, 'x.fsm')['focus_mm'] == 13

    # an SMB error is a message, not a 500
    def boom(mc):
        raise OSError('host unreachable')
    monkeypatch.setattr(app_module, '_tech_folder_files', boom)
    r = c.get(f'/admin/hall/{hall.id}/cut-tech/folder', follow_redirects=True)
    assert r.status_code == 200 and 'не е достъпна' in r.get_data(as_text=True)
    assert _login('fsm_worker').get(f'/admin/hall/{hall.id}/cut-tech/folder').status_code == 302


def test_note_round_trip(s):
    """A technology's note = the .fsm <Note>: read on import, edited in the dossier, written on export; a file
    without a note keeps the one written in the app."""
    with_note = fscut._zipcrypto(fscut.FSM_MEMBER, MATERIAL.format(speed=45, focus=11, gas=2).replace(
        '<Note/>', '<Note>бавно в ъглите</Note>').encode(), PW)
    assert fscut.read_fsm(with_note, PW, '8mmCS-D1.2-O2.fsm')['note'] == 'бавно в ъглите'
    assert fscut.read_fsm(_fsm(), PW, '8mmCS-D1.2-O2.fsm')['note'] is None
    laser = Machine(name='PG3015 NOTE', is_laser=True)
    db.session.add(laser)
    db.session.flush()
    hall = HallMachine(name=laser.name, machine_id=laser.id)
    db.session.add_all([hall, MaterialGrade(name='Стомана')])
    db.session.commit()
    c = _login('fsm_admin')
    url = f'/admin/hall/{hall.id}/cut-tech/import'
    c.post(url, data={'file': [(io.BytesIO(with_note), '8mmCS-D1.2-O2.fsm')]}, content_type='multipart/form-data')
    t = MachineCutTechnology.query.filter_by(machine_id=laser.id).one()
    assert t.note == 'бавно в ъглите'
    c.post(url, data={'file': [(io.BytesIO(_fsm()), '8mmCS-D1.2-O2.fsm')]}, content_type='multipart/form-data')
    db.session.expire_all()
    assert db.session.get(MachineCutTechnology, t.id).note == 'бавно в ъглите'           # empty <Note> keeps ours
    c.post(f'/admin/hall/{hall.id}/cut-tech', data={'tech_id': t.id, 'thickness_mm': 8, 'gas': 'oxygen',
                                                   'nozzle': '1.2d', 'cutting_speed_mm_per_min': 2700,
                                                   'note': 'фокус +13 работи'})
    db.session.expire_all()
    t = db.session.get(MachineCutTechnology, t.id)
    assert t.note == 'фокус +13 работи'
    assert 'фокус +13 работи' in c.get(f'/admin/hall/{hall.id}/dossier').get_data(as_text=True)
    out = c.get(f'/admin/hall/{hall.id}/cut-tech/{t.id}/fsm').data
    xml = zipfile.ZipFile(io.BytesIO(out)).read('material.lcm', pwd=PW.encode()).decode()
    assert '<Note>фокус +13 работи</Note>' in xml


def test_folder_sync_both_ways(s, monkeypatch):
    """A technology linked to its file in the machine's folder: changed file -> "file" (Внеси от файла), changed
    here -> "app" (Обнови файла), both -> "both", gone -> "missing"; pull / push bring them in sync again."""
    from cryptography.fernet import Fernet
    fernet = Fernet(Fernet.generate_key())
    monkeypatch.setattr(app_module, '_network_api_fernet', lambda: fernet)
    disk = {'steel': {'8mm1.2d1.5d.fsm': _fsm(speed=45, focus=13)}}
    monkeypatch.setattr(app_module, '_tech_folder_files', lambda mc: {k: sorted(v) for k, v in disk.items()})

    def read(mc, rel, name):
        if name not in disk.get(rel, {}):
            raise FileNotFoundError(name)
        return disk[rel][name]
    monkeypatch.setattr(app_module, '_tech_folder_read', read)
    monkeypatch.setattr(app_module, '_tech_folder_write', lambda mc, rel, name, data: disk.setdefault(rel, {}).update({name: data}))
    laser = Machine(name='PG3015 SYNC', is_laser=True, tech_folder=UNC)
    db.session.add(laser)
    db.session.flush()
    hall = HallMachine(name=laser.name, machine_id=laser.id)
    steel = MaterialGrade(name='Стомана')
    db.session.add_all([hall, steel])
    db.session.commit()
    c = _login('fsm_admin')
    c.post(f'/admin/hall/{hall.id}/cut-tech/folder/import', data={'use_1': '1', 'rel_1': 'steel', 'grade_1': steel.id})
    t = MachineCutTechnology.query.filter_by(machine_id=laser.id).one()
    assert t.folder_file == 'steel\\8mm1.2d1.5d.fsm' and t.folder_file_hash and t.folder_app_hash
    status = f'/admin/hall/{hall.id}/cut-tech/folder/status'
    assert c.get(status).get_json() == {str(t.id): 'same'}

    disk['steel']['8mm1.2d1.5d.fsm'] = _fsm(speed=45, focus=12)                         # changed on the machine
    assert c.get(status).get_json() == {str(t.id): 'file'}
    assert 'data-sync-tech="%d"' % t.id in c.get(f'/admin/hall/{hall.id}/dossier').get_data(as_text=True)
    c.post(f'/admin/hall/{hall.id}/cut-tech/{t.id}/folder/pull')
    db.session.expire_all()
    t = db.session.get(MachineCutTechnology, t.id)
    assert t.focus_mm == 12 and c.get(status).get_json() == {str(t.id): 'same'}

    t.focus_mm = 13.5                                                                    # changed here
    db.session.commit()
    assert c.get(status).get_json() == {str(t.id): 'app'}
    c.post(f'/admin/hall/{hall.id}/cut-tech/{t.id}/folder/push')
    assert fscut.read_fsm(disk['steel']['8mm1.2d1.5d.fsm'], PW, 'x.fsm')['focus_mm'] == 13.5
    assert c.get(status).get_json() == {str(t.id): 'same'}

    t = db.session.get(MachineCutTechnology, t.id)
    t.focus_mm = 14
    db.session.commit()
    disk['steel']['8mm1.2d1.5d.fsm'] = _fsm(speed=40, focus=11)
    assert c.get(status).get_json() == {str(t.id): 'both'}
    del disk['steel']['8mm1.2d1.5d.fsm']
    assert c.get(status).get_json() == {str(t.id): 'missing'}
    assert c.get(status + f'?tech={t.id + 99}').get_json() == {}

    # "Запиши в папката" leaves a linked technology to its own file; an unlinked one goes to Trafcom\ and is linked
    other = MachineCutTechnology(machine_id=laser.id, grade_id=steel.id, thickness_mm=10, gas='oxygen',
                                 nozzle='1.2d', cutting_speed_mm_per_min=2000, focus_mm=11)
    db.session.add(other)
    db.session.commit()
    c.post(f'/admin/hall/{hall.id}/cut-tech/folder/export')
    db.session.expire_all()
    assert 'Trafcom\\Стомана Кислород' in disk and '8mm1.2d1.5d.fsm' not in disk['steel']
    assert db.session.get(MachineCutTechnology, other.id).folder_file == 'Trafcom\\Стомана Кислород\\10mmCS-D1.2-O2.fsm'
    assert c.get(status).get_json()[str(other.id)] == 'same'
