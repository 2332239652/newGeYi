# 歌伊 WebUI 素材记录

本轮使用用户提供的原图派生 WebUI 素材。没有生图、重绘、修改原素材、调用付费模型或调整机器配置。

## 原素材审阅与选择

- 总览立绘选用：[歌伊.png](<<素材目录>/歌伊.png>)。Pillow 实际识别为 **PNG**，2942 × 4035，6,992,335 bytes。
- 导航头像选用（用户指定）：[微信图片_20260102233525_142_16.png](<<素材目录>/头像源图.png>)。Pillow 识别为 **PNG（原生 RGBA 透明底）**，2048 × 2048，802,763 bytes，Q 版眨眼比心造型非常适合侧栏圆形品牌头像。
- 原素材目录通过 glob 列出；本轮只解码像素，不读取 EXIF 提示词，不复制原图 EXIF、PNG comment、XMP 或 ICC profile。

## 交付素材

裁切 bbox 使用原图坐标，格式为 `(left, top, right, bottom)`，右下坐标不包含在裁切范围中。

| 文件 | 用途 | 源文件 | 原图裁切 bbox | 导出尺寸 | WebP 参数 | 字节数 |
| --- | --- | --- | --- | --- | --- | ---: |
| [geyi-portrait.webp](<../webui/assets/geyi-portrait.webp>) | Overview 人物展示，完整原生水彩与薄荷星芒构图 | `歌伊.png` | `(0, 0, 2942, 4035)` | 630 × 864 | quality 88 / method 6 / exact true | 62,556 |
| [geyi-avatar.webp](<../webui/assets/geyi-avatar.webp>) | 侧栏导航圆形品牌头像（Q 版眨眼比心） | `微信图片_20260102233525_142_16.png` | `(100, 140, 1948, 1988)` | 192 × 192 | quality 92 / method 6 / exact true | 16,242 |

合计 **78,798 bytes**。两张文件都以真正的 WebP 编码导出，扩展名与解码格式一致，且均已通过 `data:image/webp;base64` 内嵌进 `webui/index.html`。

### 人物展示处理

1. 保留原图完整画面，LANCZOS 缩小至 630 × 864；未裁掉发梢、衣摆、靴子或法杖。
2. 在缩小后的像素上选择 `RGB min >= 248` 且 `RGB max - min <= 7` 的近白候选。
3. 只从画布四边开始进行 **四邻域连通洪泛**，把与外边界相连的候选区 alpha 设为 0，共处理 241,383 个像素。
4. 未使用全图白色阈值直接抠图。人物轮廓内白衣、头巾高光与原生水彩星芒中被非白像素包围的白区保留。
5. 用全新像素缓冲导出，避免携带来源元数据。

### 导航头像处理

- 对用户指定的 Q 版比心透明 PNG 按 `(100, 140, 1948, 1988)` 正方裁切，去除四周多余透明留白并完整保留头顶十字发饰、蝴蝶结与比心手势，LANCZOS 缩放至 192 × 192。
- 保留原图自带的 RGBA 透明通道，侧栏 `.brand-avatar` 使用 `#f0f8f4` 浅薄荷圆底承托。
- 新像素缓冲导出，无任何 EXIF / XMP / ICC / comment 元数据。

## 复现

独立工具：[build_webui_assets.py](<../tools/build_webui_assets.py>)。依赖机器已提供的 Pillow / numpy，仅用于离线素材构建，不是应用运行时依赖。

```powershell
& '<python.exe>' 'tools\build_webui_assets.py'
```
