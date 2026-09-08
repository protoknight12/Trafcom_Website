"""
One-off diagnostic: reads the ENTIRE 34000-34999 Modbus input-register range
(function 0x04) from both Solis S6 inverters, in safe 100-register chunks
(a single connection per device, tolerating individual chunk failures),
and dumps address/raw-value pairs plus quick derived views (/10, /100,
u32-with-next-register) for anything non-zero, to a text file for analysis.

Not part of the running app - scratch diagnostic only, per explicit ask
("напраеи тест и пробвай всички регистри от 34xxx"). See
SOLIS_INVERTER_MODBUS.md section 4 for how this was used and what it found.
Safe to re-run any time (read-only), ideally while the two BMS ports are
known to be at genuinely different SOC levels (e.g. mid-discharge) so a
fresh solis_34xxx_probe_result.txt can be cross-checked against SolisCloud
again for the still-unconfirmed 34275-34290 area.

    python probe_solis_34xxx.py
"""
import json

from app import app, ModbusDevice
from pymodbus.client import ModbusTcpClient

CHUNK = 100
START = 34000
END = 35000  # exclusive

out_lines = []
json_dump = {}  # {device_id: {addr(str): value}} - for analyze_solis_34xxx_repeats.py


def log(line=''):
    print(line)
    out_lines.append(line)


with app.app_context():
    devices = ModbusDevice.query.filter_by(device_type='solis_s6').order_by(ModbusDevice.id).all()
    for device in devices:
        log(f'=== Device {device.id} {device.name} ({device.host}:{device.port} unit={device.unit_id}) ===')
        client = ModbusTcpClient(device.host, port=device.port, timeout=6)
        if not client.connect():
            log('  CONNECTION FAILED')
            continue
        all_regs = {}
        addr = START
        while addr < END:
            count = min(CHUNK, END - addr)
            try:
                result = client.read_input_registers(addr, count=count, device_id=device.unit_id)
                if result.isError():
                    log(f'  {addr}-{addr+count-1}: ERROR {result}')
                else:
                    for i, v in enumerate(result.registers):
                        all_regs[addr + i] = v
            except Exception as e:
                log(f'  {addr}-{addr+count-1}: EXCEPTION {e}')
            addr += count
        client.close()

        nonzero = {a: v for a, v in all_regs.items() if v != 0}
        json_dump[device.id] = {str(a): v for a, v in nonzero.items()}
        log(f'  Read {len(all_regs)} registers total, {len(nonzero)} non-zero.')
        log('  --- non-zero registers (addr: raw | /10 | /100) ---')
        for a in sorted(nonzero):
            v = nonzero[a]
            log(f'  {a}: {v} | /10={v/10.0:.2f} | /100={v/100.0:.3f}')
        log('  --- as u32 (addr,addr+1 both non-zero or forming a plausible pair) ---')
        for a in sorted(nonzero):
            if a + 1 in all_regs:
                hi, lo = all_regs[a], all_regs[a + 1]
                u32 = (hi << 16) | lo
                if u32 != nonzero.get(a, 0):  # only show if actually different/interesting
                    log(f'  {a}-{a+1} as u32: {u32}')
        log('')

with open(r'C:\Projects\Trafcom_Website\solis_34xxx_probe_result.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out_lines))
with open(r'C:\Projects\Trafcom_Website\solis_34xxx_probe_data.json', 'w', encoding='utf-8') as f:
    json.dump(json_dump, f, indent=1)

print('Done - written to solis_34xxx_probe_result.txt and solis_34xxx_probe_data.json')
