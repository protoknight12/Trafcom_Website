"""
One-time data step (owner, 2026-11-24), every laser technology (MachineCutTechnology):

1. Every technology carries all the data of a file: a technology without a parameter tree (a proposal, a hand-made
   one) gets the tree of its machine's nearest real technology (_tech_template_raw: same Вид + gas, nearest
   thickness), and every tree gets the technology's own fields written into it (fscut.raw_with_fields) - so the
   tree, the columns and the exported file agree.
2. Nothing Chinese is kept: every text / attribute of the tree through fscut.raw_no_chinese(), and the note.
3. The note's first line says where the technology comes from: [Оригинална] <file>, [Оригинална, коригирана от нас],
   [Генерирана от нас] <how>, [Въведена ръчно] (_mark_tech_origin).

Safe to run again (the same result).

    python -m migration.data_tech_full_files_and_origin
"""
import json

from app import (app, db, MachineCutTechnology, fscut, _tech_full_raw, _tech_columns_dict, _mark_tech_origin)


def apply():
    """Returns (trees filled from a template, technologies done). Flushed, not committed."""
    filled = done = 0
    techs = MachineCutTechnology.query.order_by(MachineCutTechnology.id).all()
    for t in techs:                      # real trees first, so a template is never a tree made a moment ago
        t._had_tree = bool(t.params_json)
    for t in sorted(techs, key=lambda x: not x._had_tree):
        raw = _tech_full_raw(t)
        if not raw:
            continue
        filled += not t._had_tree
        t.params_json = json.dumps(fscut.raw_no_chinese(fscut.raw_with_fields(
            raw, _tech_columns_dict(t), t.cutting_speed_mm_per_min, t.gas)), ensure_ascii=False)
        if t.note:
            t.note = fscut._no_chinese(t.note) or None
        _mark_tech_origin(t)
        done += 1
        db.session.flush()
    return filled, done


if __name__ == '__main__':
    with app.app_context():
        n_filled, n_done = apply()
        db.session.commit()
        print(f'Технологии: {n_done} обновени, {n_filled} получиха пълно дърво от най-близката истинска.')
