"""Self-check for the LAN network module's pure helpers (_normalize_mac(),
_parse_dhcp_lease_terse(), _encrypt_secret()/_decrypt_secret()) - see app.py's
"LAN / МРЕЖОВА ИНФРАСТРУКТУРА" section.

    python -m testing.test_network_module
"""
import os

from cryptography.fernet import Fernet

from app import _normalize_mac, _parse_dhcp_lease_terse, _encrypt_secret, _decrypt_secret

assert _normalize_mac('aa:bb:cc:dd:ee:ff') == 'AA:BB:CC:DD:EE:FF'
assert _normalize_mac('AA-BB-CC-DD-EE-FF') == 'AA:BB:CC:DD:EE:FF'
assert _normalize_mac('aabb.ccdd.eeff') == 'AA:BB:CC:DD:EE:FF', "Cisco dotted format must normalize too"
assert _normalize_mac('AABBCCDDEEFF') == 'AA:BB:CC:DD:EE:FF'
assert _normalize_mac('') is None
assert _normalize_mac(None) is None
assert _normalize_mac('aa:bb:cc:dd:ee') is None, "11 hex digits is not a valid MAC"
assert _normalize_mac('not a mac') is None

terse_line = '0   address=192.168.18.50 mac-address=AA:BB:CC:DD:EE:FF host-name="PC1" server=dhcp1 status=bound'
leases = _parse_dhcp_lease_terse(terse_line)
assert leases == [{'mac': 'AA:BB:CC:DD:EE:FF', 'ip': '192.168.18.50', 'hostname': 'PC1'}]

no_hostname = 'address=192.168.18.51 mac-address=11:22:33:44:55:66 server=dhcp1 status=bound'
assert _parse_dhcp_lease_terse(no_hostname) == [{'mac': '11:22:33:44:55:66', 'ip': '192.168.18.51', 'hostname': None}]

multi = terse_line + '\n' + no_hostname + '\n\n# a comment line\nnot a lease at all'
assert len(_parse_dhcp_lease_terse(multi)) == 2, "blank/comment/malformed lines must be skipped, not crash"

assert _parse_dhcp_lease_terse('') == []
assert _parse_dhcp_lease_terse('address=192.168.18.1 no-mac-here') == [], "a line missing mac-address must be skipped"

_old_key = os.environ.pop('NETWORK_API_ENCRYPTION_KEY', None)
try:
    assert _encrypt_secret('hunter2') is None, "no key configured -> live management stays disabled, not a crash"
    assert _decrypt_secret('anything') is None

    os.environ['NETWORK_API_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
    token = _encrypt_secret('hunter2')
    assert token is not None and token != 'hunter2', "must not store the plaintext password"
    assert _decrypt_secret(token) == 'hunter2'

    os.environ['NETWORK_API_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
    assert _decrypt_secret(token) is None, "a token from a rotated/different key must not silently decrypt"
finally:
    if _old_key is None:
        os.environ.pop('NETWORK_API_ENCRYPTION_KEY', None)
    else:
        os.environ['NETWORK_API_ENCRYPTION_KEY'] = _old_key

print("ok")
