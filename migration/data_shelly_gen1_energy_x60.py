"""
One-time: Gen1 Shelly `total` was divided by 60000 (Watt-minutes) instead of 1000 (Wh), so ShellyReadingLog.total_energy
of every Gen1 meter is 60x too small. Multiplies those rows by 60 so they join the correctly converted new rows.

A host is Gen1 when its logged energy counter grew ~1/60 of what its logged power integrates to; Gen2 and Modbus hosts
(ratio ~1) are left alone. Registered in migration/run_once.py. Decisions are printed.
"""
from sqlalchemy import text

from app import app, db, ShellyReadingLog

with app.app_context():
    hosts = [h for (h,) in db.session.query(ShellyReadingLog.host).distinct()]
    for host in hosts:
        rows = (ShellyReadingLog.query.filter(ShellyReadingLog.host == host, ShellyReadingLog.total_energy.isnot(None),
                                              ShellyReadingLog.total_power.isnot(None)).order_by(ShellyReadingLog.ts).all())
        kwh = sum((a.total_power + b.total_power) / 2 * (b.ts - a.ts) / 3.6e6 for a, b in zip(rows, rows[1:]) if 0 < b.ts - a.ts <= 300)
        delta = sum(max(b.total_energy - a.total_energy, 0) for a, b in zip(rows, rows[1:]))
        if kwh < 1:
            print(f'{host}: too little data ({kwh:.2f} kWh integrated), left alone')
        elif 0.5 / 60 < delta / kwh < 2 / 60:
            n = db.session.execute(text('UPDATE shelly_reading_log SET total_energy = total_energy * 60 WHERE host = :h'), {'h': host}).rowcount
            print(f'{host}: counter {delta:.2f} vs integrated {kwh:.2f} kWh -> Gen1, {n} rows x60')
        else:
            print(f'{host}: counter {delta:.2f} vs integrated {kwh:.2f} kWh -> left alone')
    db.session.commit()
