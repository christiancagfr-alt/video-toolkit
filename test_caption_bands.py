import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["VIDEO_TOOLKIT_DISABLE_STYLE_MEMORY"] = "1"
from pathlib import Path
import subprocess
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from modules import dynamic_caption_page as d
from modules import caption_qt_burn as burn

root = Path(__file__).resolve().parent
output = root / "_smoke_caption_bands"
output.mkdir(exist_ok=True)
app = QApplication([])
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(output / "profile"))
ffmpeg = str(root / "tools/ffmpeg/bin/ffmpeg.exe")
page = d.DynamicCaptionPage(lambda *a: "", lambda *a: "", lambda: ffmpeg, ["本地"], "本地")
page.apply_preset("三行分色底板 · 绿白黑")
settings = page._current_settings()
assert d.resolve_caption_preset(settings)["effect"] == "three_bands"
assert page.text_color.text().startswith("第一行文字")
assert page.outline_color.text().startswith("第三行文字")
text = "Se eu não der o dízimo de\n10% do meu salário, Deus vai ficar zangado?\nVou ficar debaixo de maldição?"
srt = d.events_to_srt([(0, 3, text)])
settings = burn.normalize_caption_paint_settings(settings, text)
font, rows = burn.three_band_geometry(d.parse_srt(srt)[0][2], settings)
assert len(rows) == 3
image = burn.paint_caption_overlay_image(settings, srt, "", 1)
for row in rows:
    rect = row["rect"]
    assert image.pixelColor(int(rect.left() + 2), int(rect.top() + 2)).name().lower() == row["background"].lower(), (rect, row["background"], image.pixelColor(int(rect.left() + 2), int(rect.top() + 2)).name())
image.save(str(output / "bands.png"))
page.band_last_background.setText("第三行背景 #123456")
page.font.setCurrentText("Arial")
snapshot = page._style_template_snapshot()
assert snapshot["band_last_background"] == "#123456"
page.apply_preset("Descript 经典黄")
page._apply_style_template_data(snapshot)
restored = page._current_settings()
assert restored["band_last_background"] == "#123456"
assert d.resolve_caption_preset(restored)["effect"] == "three_bands"
ass = output / "bands.ass"
d.write_ass(ass, srt, restored)
assert ass.read_text(encoding="utf-8-sig").count("Dialogue:") == 6
command = [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "color=gray:s=360x640:r=10:d=1",
           "-vf", d.ass_filter_expression(ass), "-c:v", "libx264", str(output / "bands.mp4")]
result = subprocess.run(command, capture_output=True)
assert result.returncode == 0, result.stderr
page.close()
print("PASS three-band preset / per-line colors / Qt painting / font and color save-load / ASS render")
