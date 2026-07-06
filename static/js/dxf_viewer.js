/**
 * 2D DXF Viewer Modal
 * Fetches stored shape geometry for a given uploaded file and draws it on
 * a canvas. DXF coordinates are Y-up; canvas coordinates are Y-down, so the
 * Y-axis is flipped when drawing.
 */

let dxfViewerOverlay, dxfViewerCanvas, dxfViewerStatus, dxfViewerTitle;

document.addEventListener('DOMContentLoaded', function () {
    dxfViewerOverlay = document.getElementById('viewerOverlay');
    dxfViewerCanvas = document.getElementById('viewerCanvas');
    dxfViewerStatus = document.getElementById('viewerStatus');
    dxfViewerTitle = document.getElementById('viewerTitle');

    // Close modal on background click or Escape key
    dxfViewerOverlay.addEventListener('click', function (e) {
        if (e.target === dxfViewerOverlay) {
            closeDxfViewer();
        }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && dxfViewerOverlay.classList.contains('active')) {
            closeDxfViewer();
        }
    });
});

function openDxfViewer(fileId, filename) {
    dxfViewerTitle.textContent = filename;
    dxfViewerStatus.style.display = 'flex';
    dxfViewerStatus.textContent = 'Зареждане...';
    dxfViewerOverlay.classList.add('active');

    fetch(`/geometry/${fileId}`)
        .then(function (response) {
            if (!response.ok) {
                throw new Error('Грешка при зареждане на геометрията.');
            }
            return response.json();
        })
        .then(function (data) {
            if (!data.shapes || data.shapes.length === 0) {
                dxfViewerStatus.textContent = 'Няма налична геометрия за визуализация.';
                dxfViewerStatus.style.display = 'flex';
                return;
            }
            dxfViewerStatus.style.display = 'none';
            drawShapes(data.shapes);
        })
        .catch(function (err) {
            dxfViewerStatus.textContent = err.message || 'Грешка при зареждане.';
            dxfViewerStatus.style.display = 'flex';
        });
}

function closeDxfViewer() {
    dxfViewerOverlay.classList.remove('active');
}

function drawShapes(shapes) {
    const canvas = dxfViewerCanvas;
    const body = canvas.parentElement;

    // Match canvas pixel size to its rendered CSS size for crisp lines
    const dpr = window.devicePixelRatio || 1;
    const cssWidth = body.clientWidth;
    const cssHeight = body.clientHeight;
    canvas.width = cssWidth * dpr;
    canvas.height = cssHeight * dpr;
    canvas.style.width = cssWidth + 'px';
    canvas.style.height = cssHeight + 'px';

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    // 1. Compute bounding box across all shapes (in DXF drawing units)
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;

    function expand(x, y) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
    }

    shapes.forEach(function (shape) {
        if (shape.type === 'line') {
            expand(shape.x1, shape.y1);
            expand(shape.x2, shape.y2);
        } else if (shape.type === 'circle') {
            expand(shape.cx - shape.r, shape.cy - shape.r);
            expand(shape.cx + shape.r, shape.cy + shape.r);
        } else if (shape.type === 'arc') {
            expand(shape.cx - shape.r, shape.cy - shape.r);
            expand(shape.cx + shape.r, shape.cy + shape.r);
        } else if (shape.type === 'polyline') {
            shape.points.forEach(function (pt) {
                expand(pt[0], pt[1]);
            });
        }
    });

    if (!isFinite(minX) || !isFinite(minY) || !isFinite(maxX) || !isFinite(maxY)) {
        return;
    }

    const drawingWidth = Math.max(maxX - minX, 0.0001);
    const drawingHeight = Math.max(maxY - minY, 0.0001);

    // 2. Compute scale to fit drawing within canvas, with padding.
    // Extra room is reserved on the left and bottom for dimension lines/labels.
    const padding = 30;
    const dimGutter = 40; // space reserved for dimension lines + text
    const availableWidth = cssWidth - padding * 2 - dimGutter;
    const availableHeight = cssHeight - padding * 2 - dimGutter;
    const scale = Math.min(availableWidth / drawingWidth, availableHeight / drawingHeight);

    // 3. Transform a DXF point (Y-up) into a canvas pixel coordinate (Y-down),
    // centered within the available drawing area (shifted right/up to leave
    // room for the dimension gutter on the left and bottom).
    const offsetX = padding + dimGutter + (availableWidth - drawingWidth * scale) / 2;
    const offsetY = padding + (availableHeight - drawingHeight * scale) / 2;

    function toCanvas(x, y) {
        return [
            offsetX + (x - minX) * scale,
            cssHeight - (offsetY + (y - minY) * scale)
        ];
    }

    // 4. Draw each shape
    ctx.strokeStyle = '#00e0a4';
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';

    shapes.forEach(function (shape) {
        ctx.beginPath();

        if (shape.type === 'line') {
            const [x1, y1] = toCanvas(shape.x1, shape.y1);
            const [x2, y2] = toCanvas(shape.x2, shape.y2);
            ctx.moveTo(x1, y1);
            ctx.lineTo(x2, y2);

        } else if (shape.type === 'circle') {
            const [cx, cy] = toCanvas(shape.cx, shape.cy);
            ctx.arc(cx, cy, shape.r * scale, 0, Math.PI * 2);

        } else if (shape.type === 'arc') {
            const [cx, cy] = toCanvas(shape.cx, shape.cy);
            // Canvas angles increase clockwise (Y-down), DXF angles increase
            // counter-clockwise (Y-up), so we negate both angles and swap
            // start/end to preserve the correct arc direction and sweep.
            const startRad = -shape.start_angle * Math.PI / 180;
            const endRad = -shape.end_angle * Math.PI / 180;
            ctx.arc(cx, cy, shape.r * scale, endRad, startRad);

        } else if (shape.type === 'polyline') {
            shape.points.forEach(function (pt, idx) {
                const [x, y] = toCanvas(pt[0], pt[1]);
                if (idx === 0) {
                    ctx.moveTo(x, y);
                } else {
                    ctx.lineTo(x, y);
                }
            });
            if (shape.closed && shape.points.length > 0) {
                const [x0, y0] = toCanvas(shape.points[0][0], shape.points[0][1]);
                ctx.lineTo(x0, y0);
            }
        }

        ctx.stroke();
    });

    // 5. Draw overall width/height dimension lines (like a technical drawing)
    drawDimensions(ctx, minX, minY, maxX, maxY, toCanvas, drawingWidth, drawingHeight);
}

function drawDimensions(ctx, minX, minY, maxX, maxY, toCanvas, drawingWidth, drawingHeight) {
    const dimColor = '#ffb703';
    const extLineColor = 'rgba(255, 183, 3, 0.4)';
    const extPastLine = 8;   // how far extension lines poke past the dimension line
    const dimLineGap = 18;   // how far the dimension line sits from the shape

    ctx.save();
    ctx.strokeStyle = dimColor;
    ctx.fillStyle = dimColor;
    ctx.font = '12px Arial, sans-serif';
    ctx.lineWidth = 1;

    // --- Width dimension (bottom, horizontal) ---
    // Corners of the bounding box in canvas space
    const [bottomLeftX, bottomLeftY] = toCanvas(minX, minY);
    const [bottomRightX, bottomRightY] = toCanvas(maxX, minY);
    const dimY = Math.max(bottomLeftY, bottomRightY) + dimLineGap;

    // Extension lines (from shape corners down to the dimension line)
    ctx.strokeStyle = extLineColor;
    drawLine(ctx, bottomLeftX, bottomLeftY, bottomLeftX, dimY + extPastLine);
    drawLine(ctx, bottomRightX, bottomRightY, bottomRightX, dimY + extPastLine);

    // Dimension line with arrowheads
    ctx.strokeStyle = dimColor;
    drawLine(ctx, bottomLeftX, dimY, bottomRightX, dimY);
    drawArrowhead(ctx, bottomLeftX, dimY, 1);
    drawArrowhead(ctx, bottomRightX, dimY, -1);

    // Width label, centered under the dimension line
    const widthLabel = drawingWidth.toFixed(2) + ' мм';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText(widthLabel, (bottomLeftX + bottomRightX) / 2, dimY + 4);

    // --- Height dimension (left side, vertical) ---
    const [topLeftX, topLeftY] = toCanvas(minX, maxY);
    const dimX = Math.min(bottomLeftX, topLeftX) - dimLineGap;

    ctx.strokeStyle = extLineColor;
    drawLine(ctx, bottomLeftX, bottomLeftY, dimX - extPastLine, bottomLeftY);
    drawLine(ctx, topLeftX, topLeftY, dimX - extPastLine, topLeftY);

    ctx.strokeStyle = dimColor;
    drawLine(ctx, dimX, bottomLeftY, dimX, topLeftY);
    drawArrowhead(ctx, dimX, bottomLeftY, 1, true);
    drawArrowhead(ctx, dimX, topLeftY, -1, true);

    // Height label, rotated to read vertically alongside the dimension line
    const heightLabel = drawingHeight.toFixed(2) + ' мм';
    ctx.save();
    ctx.translate(dimX - 6, (bottomLeftY + topLeftY) / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText(heightLabel, 0, 0);
    ctx.restore();

    ctx.restore();
}

function drawLine(ctx, x1, y1, x2, y2) {
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
}

/**
 * Draws a small arrowhead at (x, y) pointing along the dimension line.
 * direction: 1 or -1, indicating which way the arrow "opens" toward.
 * vertical: true for the vertical (height) dimension line, false for horizontal.
 */
function drawArrowhead(ctx, x, y, direction, vertical) {
    const size = 5;
    ctx.beginPath();
    if (vertical) {
        ctx.moveTo(x, y);
        ctx.lineTo(x - size / 2, y + size * direction);
        ctx.lineTo(x + size / 2, y + size * direction);
    } else {
        ctx.moveTo(x, y);
        ctx.lineTo(x + size * direction, y - size / 2);
        ctx.lineTo(x + size * direction, y + size / 2);
    }
    ctx.closePath();
    ctx.fill();
}