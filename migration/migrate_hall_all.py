"""
Everything an existing database needs for the hall map, in the right order, in one command. Every step is safe to re-run.

    python -m migration.migrate_hall_all

1. migrate_add_hall_elevation   - elevation / floors columns
2. migrate_add_hall_model       - 3D model, rotation, accessory columns
3. migrate_add_machine_dossier  - dossier columns + hall_machine_file table
4. migrate_unify_machines       - hall_machine.machine_id / on_plan + machine_connection table
5. seed_hall_machine_specs      - machine cards (page='hall') and their link to the hall machines

New tables (hall_equipment kinds convector/sensor/network need no schema change) come from db.create_all() inside the steps above.
Afterwards, in the app: "Създай липсващите машини" on /machines, then "Постави всички устройства" and "Синхронизирай картите" on the hall plan tab.
"""
import runpy

STEPS = ('migrate_add_hall_elevation', 'migrate_add_hall_model', 'migrate_add_machine_dossier', 'migrate_unify_machines', 'seed_hall_machine_specs')

for name in STEPS:
    print(f'== {name}')
    runpy.run_module(f'migration.{name}', run_name='__main__')
print('Hall migrations done.')
