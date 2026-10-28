# 发行包 · 3D 打印建模工具

本目录存放 **3D 打印建模工具** 的 Windows x64 可运行包体（由桌面快捷方式实际指向的安装/发布目录整理而来）。

| 项目 | 内容 |
| --- | --- |
| 版本 | `1.0.0` |
| 平台 | Windows x64 |
| 包体路径 | `release/1.0.0/3d-print-forge-1.0.0-win-x64/` |
| 文件数 | 173 |
| 体积 | 约 81.4 MB |

## 运行方式

双击 `3D打印建模工具.exe`

## 校验完整性

```bash
cd release
sha256sum -c SHA256SUMS.txt
```

Windows PowerShell：

```powershell
Get-Content release\SHA256SUMS.txt | ForEach-Object {
  $h, $p = $_ -split '  ', 2
  if ((Get-FileHash $p -Algorithm SHA256).Hash -ne $h.ToUpper()) { "MISMATCH $p" }
}
```

## 关于 3D 打印建模工具

3D 打印模型处理工具集：导入 → 检测 → 修复 → 镂空打孔 → 支撑 → 参数建议 → 导出，六步向导式界面。

## 未纳入的内容

开发用 `.venv/`、基准测试 `bench/` 未纳入（exe 为单文件打包，不依赖 venv）。

以上目录属于开发环境、运行期缓存或大体积模型权重，不影响程序本体运行。
