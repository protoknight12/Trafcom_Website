"""Self-check for _group_service_cards_by_section() - guards the bug where a
new machine added to an existing section (e.g. "ФРЕЗОВИ ЦЕНТРОВЕ") spawned a
second, same-titled section instead of joining the existing one, because
cards were only merged into the *immediately preceding* section instead of
being keyed by title. See app.py's services()/CLAUDE.md task 4.

    python test_service_sections_grouping.py
"""
from types import SimpleNamespace

from app import _group_service_cards_by_section

card = lambda id_, section_title: SimpleNamespace(id=id_, section_title=section_title)

# Interleaved insertion order: A, A, B, then a *new* A card added later
# (highest id) - the old adjacent-only grouping would spawn a second "A"
# section here instead of appending to the first.
cards = [card(1, 'A'), card(2, 'A'), card(3, 'B'), card(4, 'A')]
sections = _group_service_cards_by_section(cards)

assert [s['title'] for s in sections] == ['A', 'B'], "must not spawn a duplicate section for A"
assert [c.id for c in sections[0]['cards']] == [1, 2, 4], "new A card must join the existing A section"
assert [c.id for c in sections[1]['cards']] == [3]

# Cards with no section_title fall into one trailing bucket.
cards_with_blank = cards + [card(5, None), card(6, None)]
sections2 = _group_service_cards_by_section(cards_with_blank)
assert sections2[-1]['title'] == 'ДОПЪЛНИТЕЛНИ МАШИНИ'
assert [c.id for c in sections2[-1]['cards']] == [5, 6]

print("ok")
