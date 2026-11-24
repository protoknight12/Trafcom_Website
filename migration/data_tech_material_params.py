"""
One-time data step (owner, 2026-11-25): every stored technology tree's MaterialParams = the technology's own Вид name,
nozzle and thickness (_tech_tree) - trees copied from another file (a template, a copy, an import whose CypCut
document kept the source's values) carried the source's NozzleSpec / Thickness. Safe to run again.

    python -m migration.data_tech_material_params
"""
import json

from app import app, db, MachineCutTechnology, _tech_raw, _tech_tree


def apply():
    """Returns how many trees changed. Flushed, not committed."""
    n = 0
    for t in MachineCutTechnology.query.filter(MachineCutTechnology.params_json.isnot(None)):
        raw = _tech_raw(t)
        if not raw:
            continue
        new = json.dumps(_tech_tree(t, raw), ensure_ascii=False)
        if new != t.params_json:
            t.params_json = new
            n += 1
    db.session.flush()
    return n


if __name__ == '__main__':
    with app.app_context():
        changed = apply()
        db.session.commit()
        print(f'Технологии с поправени MaterialParams (вид, дюза, дебелина): {changed}.')
