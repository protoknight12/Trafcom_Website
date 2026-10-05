"""Camera: snapshot URL / go2rtc source per connection type (nvr / onvif / rtsp)."""
from app import Camera, CameraNvr

n = CameraNvr(name='n', host='10.0.0.5', port=8080)
nvr = Camera(name='a', nvr=n, channel=3)                       # nvr is the default connection
assert nvr.snapshot_target()[0] == 'http://10.0.0.5:8080/ISAPI/Streaming/channels/301/picture'
assert nvr.rtsp_url().endswith('@10.0.0.5:554/Streaming/Channels/302')
assert Camera(name='c', conn_type='nvr').snapshot_target() is None
onvif = Camera(name='o', conn_type='onvif', host='1.2.3.4', username='u', port=8899)
assert onvif.rtsp_url() == 'onvif://u:@1.2.3.4:8899' and onvif.snapshot_target() is None
rtsp = Camera(name='r', conn_type='rtsp', host='1.2.3.4', username='u', rtsp_path='/stream1', snapshot_path='/snap.jpg')
assert rtsp.rtsp_url() == 'rtsp://u:@1.2.3.4:554/stream1' and rtsp.snapshot_target()[0] == 'http://1.2.3.4/snap.jpg'
assert Camera(name='x', conn_type='rtsp').rtsp_url() is None
print('ok')
