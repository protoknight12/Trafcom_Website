"""Self-check for the 'web_designer' role (see User.can_edit_content in app.py).
Guards the permission split: web designers and admins may redact info pages
(machine names, detail names, product name/description); other roles may not,
and web designers specifically must not be able to touch pricing fields via
admin_product_update() (they share that endpoint with admins - see the
current_user.is_admin branch inside it).

    python -m testing.test_web_designer_role
"""
from app import User

roles_that_can_edit_content = {'admin', 'web_designer'}
roles_that_cannot = {'regular_user', 'worker'}

for role in roles_that_can_edit_content:
    u = User(role=role)
    assert u.can_edit_content, f"{role} should be able to edit content"

for role in roles_that_cannot:
    u = User(role=role)
    assert not u.can_edit_content, f"{role} should NOT be able to edit content"

# Only a real admin gets the pricing/ERP branch in admin_product_update()
admin = User(role='admin')
designer = User(role='web_designer')
assert admin.is_admin and not designer.is_admin, \
    "web_designer must stay outside the is_admin branch that edits markup/ERP number"

print("ok")
