// Boxes of the detected objects over a camera's <img> (frame or MJPEG video): attachDetectOverlay(img, cameraId).
// Polls /admin/cameras/<id>/detections every 1.5 s; stops by itself when the image leaves the page.
window.attachDetectOverlay = function (img, camId) {
    const cv = document.createElement('canvas');
    cv.style.cssText = 'position:absolute;pointer-events:none';
    img.after(cv);
    if (getComputedStyle(img.parentNode).position === 'static') img.parentNode.style.position = 'relative';
    let timer = null;
    const draw = items => {
        const w = img.clientWidth, h = img.clientHeight;
        cv.style.left = img.offsetLeft + 'px'; cv.style.top = img.offsetTop + 'px'; cv.width = w; cv.height = h;
        const c = cv.getContext('2d'); c.clearRect(0, 0, w, h);
        c.lineWidth = 2; c.font = '12px sans-serif';
        items.forEach(it => {
            const [x1, y1, x2, y2] = it.box, x = x1 * w, y = y1 * h, bw = (x2 - x1) * w, bh = (y2 - y1) * h, t = it.label + ' ' + Math.round(it.score * 100) + '%';
            c.strokeStyle = '#ff3c3c'; c.strokeRect(x, y, bw, bh);
            c.fillStyle = 'rgba(0,0,0,0.65)'; c.fillRect(x, Math.max(0, y - 15), c.measureText(t).width + 6, 15);
            c.fillStyle = '#ffe600'; c.fillText(t, x + 3, Math.max(11, y - 4));
        });
    };
    const poll = async () => {
        if (!img.isConnected) { clearInterval(timer); cv.remove(); return; }
        if (document.hidden) return;
        const r = await fetch('/admin/cameras/' + camId + '/detections').catch(() => null), d = r && r.ok ? await r.json().catch(() => null) : null;
        draw(d ? d.items : []);
    };
    poll(); timer = setInterval(poll, 1500);
};
