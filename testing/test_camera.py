"""Camera.snapshot_target(): own host wins, else NVR channel URL, else None."""
from app import Camera, CameraNvr

n = CameraNvr(name='n', host='10.0.0.5', port=8080)
assert Camera(name='a', nvr=n, channel=3).snapshot_target()[0] == 'http://10.0.0.5:8080/ISAPI/Streaming/channels/301/picture'
assert Camera(name='b', host='10.0.0.9', nvr=n, channel=3).snapshot_target()[0] == 'http://10.0.0.9/ISAPI/Streaming/channels/101/picture'
assert Camera(name='c').snapshot_target() is None
print('ok')
assert Camera(name='d', host='1.2.3.4', snapshot_path='/cgi-bin/snapshot.cgi').snapshot_target()[0] == 'http://1.2.3.4/cgi-bin/snapshot.cgi'
