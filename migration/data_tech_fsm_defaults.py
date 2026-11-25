"""
One-time data step (owner, 2026-11-25): every stored technology tree - and the machines' Defilm / Recool - made full
like a CypCut .fsm (_tech_tree -> fscut.raw_fsm_defaults): trees imported from an .lxds lack every attribute equal to
CypCut's default (PierceFlags, Enable, TimeStay ...), so the ticks of the parameters page were unknown and our .fsm
files were sparse. Meaning unchanged - CypCut reads a missing attribute as exactly that default. The technologies'
files in the folder then show as changed here ("Обнови всички файлове" rewrites them). Safe to run again.

    python -m migration.data_tech_fsm_defaults
"""
import json

import fscut
from app import app, db, Machine, MachineCutTechnology, _tech_raw, _tech_tree


def apply():
    """-> (technologies changed, Defilm / Recool trees changed). Flushed, not committed."""
    n = 0
    for t in MachineCutTechnology.query.filter(MachineCutTechnology.params_json.isnot(None)):
        raw = _tech_raw(t)
        if not raw:
            continue
        new = json.dumps(_tech_tree(t, raw), ensure_ascii=False)
        if new != t.params_json:
            t.params_json = new
            n += 1
    s = 0
    for m in Machine.query.filter((Machine.defilm_json.isnot(None)) | (Machine.recool_json.isnot(None))):
        for col in ('defilm_json', 'recool_json'):
            raw = json.loads(getattr(m, col) or 'null')
            if raw:
                new = json.dumps(fscut.raw_fsm_defaults(raw), ensure_ascii=False)
                if new != getattr(m, col):
                    setattr(m, col, new)
                    s += 1
    db.session.flush()
    return n, s


if __name__ == '__main__':
    with app.app_context():
        changed, service = apply()
        db.session.commit()
        print(f'Технологии, допълнени с подразбиращите се стойности на CypCut: {changed}; Defilm / Recool: {service}.')
