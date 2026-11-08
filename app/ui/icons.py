"""手绘矢量图标：不依赖开发机字体或第三方图标资源，可随包分发。"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _canvas(size: int) -> tuple[QPixmap, QPainter]:
    scale = 2
    pix = QPixmap(size * scale, size * scale)
    pix.setDevicePixelRatio(scale)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    return pix, painter


def _pen(painter: QPainter, color: QColor, width: float = 1.6) -> QPen:
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    return pen


def make_icon(name: str, color: str = "#3B6EA5", size: int = 18) -> QIcon:
    pix, painter = _canvas(size)
    c = QColor(color)
    _pen(painter, c)
    s = size
    if name == "folder":
        path = QPainterPath()
        path.moveTo(s * 0.14, s * 0.28)
        path.lineTo(s * 0.42, s * 0.28)
        path.lineTo(s * 0.5, s * 0.38)
        path.lineTo(s * 0.86, s * 0.38)
        path.lineTo(s * 0.86, s * 0.76)
        path.lineTo(s * 0.14, s * 0.76)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "disc":
        painter.drawEllipse(QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68))
        painter.drawEllipse(QRectF(s * 0.42, s * 0.42, s * 0.16, s * 0.16))
        painter.drawArc(QRectF(s * 0.26, s * 0.26, s * 0.48, s * 0.48), 40 * 16, 90 * 16)
    elif name == "note":
        painter.drawEllipse(QRectF(s * 0.18, s * 0.6, s * 0.24, s * 0.2))
        painter.drawEllipse(QRectF(s * 0.56, s * 0.52, s * 0.24, s * 0.2))
        painter.drawLine(QPointF(s * 0.42, s * 0.62), QPointF(s * 0.42, s * 0.24))
        painter.drawLine(QPointF(s * 0.8, s * 0.54), QPointF(s * 0.8, s * 0.18))
        painter.drawLine(QPointF(s * 0.42, s * 0.24), QPointF(s * 0.8, s * 0.18))
    elif name == "archive":
        painter.drawRect(QRectF(s * 0.16, s * 0.3, s * 0.68, s * 0.46))
        painter.drawLine(QPointF(s * 0.16, s * 0.42), QPointF(s * 0.84, s * 0.42))
        painter.drawLine(QPointF(s * 0.42, s * 0.56), QPointF(s * 0.58, s * 0.56))
    elif name == "warn":
        path = QPainterPath()
        path.moveTo(s * 0.5, s * 0.18)
        path.lineTo(s * 0.88, s * 0.78)
        path.lineTo(s * 0.12, s * 0.78)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(s * 0.5, s * 0.4), QPointF(s * 0.5, s * 0.58))
        painter.drawPoint(QPointF(s * 0.5, s * 0.68))
    elif name == "check":
        pen = _pen(painter, c, 2.0)
        painter.drawPolyline([QPointF(s * 0.22, s * 0.52), QPointF(s * 0.42, s * 0.72),
                              QPointF(s * 0.78, s * 0.3)])
    elif name == "history":
        painter.drawArc(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64), 60 * 16, 300 * 16)
        painter.drawLine(QPointF(s * 0.5, s * 0.34), QPointF(s * 0.5, s * 0.52))
        painter.drawLine(QPointF(s * 0.5, s * 0.52), QPointF(s * 0.64, s * 0.6))
    elif name == "undo":
        painter.drawArc(QRectF(s * 0.2, s * 0.24, s * 0.6, s * 0.56), 30 * 16, 150 * 16)
        painter.drawPolyline([QPointF(s * 0.2, s * 0.26), QPointF(s * 0.2, s * 0.48),
                              QPointF(s * 0.42, s * 0.48)])
    elif name == "settings":
        painter.drawEllipse(QRectF(s * 0.3, s * 0.3, s * 0.4, s * 0.4))
        painter.drawEllipse(QRectF(s * 0.42, s * 0.42, s * 0.16, s * 0.16))
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            painter.drawLine(QPointF(s * 0.5 + dx * s * 0.22, s * 0.5 + dy * s * 0.22),
                             QPointF(s * 0.5 + dx * s * 0.36, s * 0.5 + dy * s * 0.36))
    elif name == "sun":
        painter.drawEllipse(QRectF(s * 0.34, s * 0.34, s * 0.32, s * 0.32))
        for i in range(8):
            import math
            angle = math.pi * i / 4
            painter.drawLine(
                QPointF(s * 0.5 + math.cos(angle) * s * 0.24, s * 0.5 + math.sin(angle) * s * 0.24),
                QPointF(s * 0.5 + math.cos(angle) * s * 0.36, s * 0.5 + math.sin(angle) * s * 0.36))
    elif name == "moon":
        path = QPainterPath()
        path.moveTo(s * 0.62, s * 0.2)
        path.arcTo(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64), 60, 250)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "paper":
        painter.drawRect(QRectF(s * 0.24, s * 0.16, s * 0.52, s * 0.68))
        for y in (0.34, 0.48, 0.62):
            painter.drawLine(QPointF(s * 0.34, s * y), QPointF(s * 0.66, s * y))
    elif name == "plus":
        painter.drawLine(QPointF(s * 0.5, s * 0.26), QPointF(s * 0.5, s * 0.74))
        painter.drawLine(QPointF(s * 0.26, s * 0.5), QPointF(s * 0.74, s * 0.5))
    elif name == "search":
        painter.drawEllipse(QRectF(s * 0.2, s * 0.2, s * 0.42, s * 0.42))
        painter.drawLine(QPointF(s * 0.58, s * 0.58), QPointF(s * 0.82, s * 0.82))
    elif name == "target":
        painter.drawEllipse(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64))
        painter.drawEllipse(QRectF(s * 0.36, s * 0.36, s * 0.28, s * 0.28))
        painter.drawPoint(QPointF(s * 0.5, s * 0.5))
    elif name == "play":
        path = QPainterPath()
        path.moveTo(s * 0.3, s * 0.2)
        path.lineTo(s * 0.82, s * 0.5)
        path.lineTo(s * 0.3, s * 0.8)
        path.closeSubpath()
        painter.setBrush(QBrush(c))
        painter.drawPath(path)
    elif name == "open":
        painter.drawRect(QRectF(s * 0.2, s * 0.34, s * 0.44, s * 0.44))
        painter.drawLine(QPointF(s * 0.4, s * 0.6), QPointF(s * 0.82, s * 0.18))
        painter.drawLine(QPointF(s * 0.6, s * 0.18), QPointF(s * 0.82, s * 0.18))
        painter.drawLine(QPointF(s * 0.82, s * 0.18), QPointF(s * 0.82, s * 0.4))
    elif name == "trash":
        painter.drawLine(QPointF(s * 0.24, s * 0.3), QPointF(s * 0.76, s * 0.3))
        painter.drawRect(QRectF(s * 0.32, s * 0.3, s * 0.36, s * 0.5))
        painter.drawLine(QPointF(s * 0.42, s * 0.22), QPointF(s * 0.58, s * 0.22))
    elif name == "refresh":
        painter.drawArc(QRectF(s * 0.2, s * 0.2, s * 0.6, s * 0.6), 30 * 16, 270 * 16)
        painter.drawPolyline([QPointF(s * 0.72, s * 0.14), QPointF(s * 0.74, s * 0.34),
                              QPointF(s * 0.54, s * 0.36)])
    elif name == "grid":
        for x in (0.18, 0.54):
            for y in (0.18, 0.54):
                painter.drawRect(QRectF(s * x, s * y, s * 0.28, s * 0.28))
    elif name == "panel":
        painter.drawRoundedRect(QRectF(s * 0.16, s * 0.2, s * 0.68, s * 0.6), 2, 2)
        painter.drawLine(QPointF(s * 0.62, s * 0.2), QPointF(s * 0.62, s * 0.8))
    elif name == "shield":
        path = QPainterPath()
        path.moveTo(s * 0.5, s * 0.16)
        path.lineTo(s * 0.82, s * 0.28)
        path.lineTo(s * 0.82, s * 0.52)
        path.quadTo(s * 0.82, s * 0.76, s * 0.5, s * 0.86)
        path.quadTo(s * 0.18, s * 0.76, s * 0.18, s * 0.52)
        path.lineTo(s * 0.18, s * 0.28)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawPolyline([QPointF(s * 0.36, s * 0.5), QPointF(s * 0.46, s * 0.6),
                              QPointF(s * 0.66, s * 0.4)])
    elif name == "arrow":
        painter.drawLine(QPointF(s * 0.2, s * 0.5), QPointF(s * 0.8, s * 0.5))
        painter.drawPolyline([QPointF(s * 0.6, s * 0.3), QPointF(s * 0.8, s * 0.5),
                              QPointF(s * 0.6, s * 0.7)])
    elif name == "close":
        painter.drawLine(QPointF(s * 0.3, s * 0.3), QPointF(s * 0.7, s * 0.7))
        painter.drawLine(QPointF(s * 0.7, s * 0.3), QPointF(s * 0.3, s * 0.7))
    elif name == "eye":
        path = QPainterPath()
        path.moveTo(s * 0.12, s * 0.5)
        path.quadTo(s * 0.5, s * 0.14, s * 0.88, s * 0.5)
        path.quadTo(s * 0.5, s * 0.86, s * 0.12, s * 0.5)
        painter.drawPath(path)
        painter.setBrush(QBrush(c))
        painter.drawEllipse(QRectF(s * 0.39, s * 0.39, s * 0.22, s * 0.22))
    else:  # 兜底：圆点
        painter.setBrush(QBrush(c))
        painter.drawEllipse(QRectF(s * 0.36, s * 0.36, s * 0.28, s * 0.28))
    painter.end()
    return QIcon(pix)


def multi_color_icon() -> QIcon:
    """可执行程序图标（多尺寸）。"""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_app_pixmap(size))
    return icon


def draw_note(painter: QPainter, size: int, color: str) -> None:
    """在给定画笔上绘制白色音符（品牌图标用，不闭合画笔）。"""
    pen = _pen(painter, QColor(color), size * 0.09)
    s = size
    painter.setBrush(QBrush(QColor(color)))
    painter.drawEllipse(QRectF(s * 0.22, s * 0.6, s * 0.2, s * 0.17))
    painter.drawEllipse(QRectF(s * 0.52, s * 0.54, s * 0.2, s * 0.17))
    pen2 = QPen(QColor(color))
    pen2.setWidthF(size * 0.085)
    pen2.setCapStyle(Qt.RoundCap)
    painter.setPen(pen2)
    painter.setBrush(Qt.NoBrush)
    painter.drawPolyline([QPointF(s * 0.42, s * 0.68), QPointF(s * 0.42, s * 0.26),
                          QPointF(s * 0.72, s * 0.2), QPointF(s * 0.72, s * 0.62)])


def _app_pixmap(size: int) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#3B6EA5"))
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawEllipse(QRectF(size * 0.26, size * 0.26, size * 0.48, size * 0.48))
    painter.setBrush(QColor("#3B6EA5"))
    painter.drawEllipse(QRectF(size * 0.42, size * 0.42, size * 0.16, size * 0.16))
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawEllipse(QRectF(size * 0.46, size * 0.46, size * 0.08, size * 0.08))
    painter.end()
    return pix
