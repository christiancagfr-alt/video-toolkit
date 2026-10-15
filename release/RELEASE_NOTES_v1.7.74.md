# VideoToolkit v1.7.74

- **安装修复**：升级时强制覆盖全部程序文件（`ignoreversion`），避免新旧 Qt/PySide DLL 混装
- 修复从旧版覆盖安装后启动失败：`DLL load failed while importing QtCore` / 弹窗「找不到指定的程序」
- 若仍打不开：先卸载旧版，再安装本版 Setup；或删除 `%LOCALAPPDATA%\Programs\VideoToolkit\_internal` 后重装

功能变更同 v1.7.73（时间轴裁切、字幕空档、片头片尾、分段识别、压缩与水印）。

详见 `docs/FEATURES.md`
