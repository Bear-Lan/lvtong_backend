# 抓拍、LED 与雷达 LAZ

## 相机映射

自动与手动 `capture_category` 共用：head→cam_head（.102）、tail→cam_lane（.103）、top→cam_road（.107）。
分别支持 HEAD_CAPTURE_DEVICE_ID、TAIL_CAPTURE_DEVICE_ID、TOP_CAPTURE_DEVICE_ID 环境变量；相机 config 中同类别的 head_device_id 等可覆盖。
不再用 BODY_CAPTURE_DEVICE_ID/body_capture_device_id 把三类合并到一个相机。

## LED

每次分别发送 `$MDRMC,1,frontleddisplay=DISPLAYn`、`$MDRMC,1,backleddisplay=DISPLAYn`。
编号依据用户提供的语音/LED 图片表，未照抄 VOICE6～9：

| DISPLAY | 文案 |
|---|---|
| 1 | 绿通车辆，按键检测 |
| 2 | 申请已接收，请原地等待 |
| 3 | 申请已受理，请等待放行（原表写“方向”） |
| 4 | 待检车辆，请通行 |
| 5 | 检测中，请勿跟车 |
| 6 | 系统运维，暂停使用 |

业务 step1～5 对应 DISPLAY1～5；旧 step6 仍为检测中，用 DISPLAY5；step7 完成及预约取消用 DISPLAY4。
保留旧业务码入参兼容，但线上不再发送 leddisplay=旧码。维护文案可显式 set_display DISPLAY6，未增加自动触发。
任一方向发送失败返回 False；即使前屏失败也尝试后屏。成功仅代表命令已发，无物理屏显回执。

## LAZ

RADAR_HEAD_URL 默认 `http://192.168.88.201:5001/appt/image`，用于预约界面来车图（2026-11-06 用户提供）。校验真实图像后沿用上游测量响应头，生成同源临时预览 URL。
`http://192.168.88.201:5001/geom/laz` 是独立的原始点云接口，需要时可显式配置；保留以下 LAS/LAZ 解码能力。
用 laspy 2.7 + lazrs 解压实际 LAS/LAZ 二进制，先应用 LAS header 中各轴 scales/offsets，再按选定两轴等比例投影为 PNG。
沿用 `/api/booking/radar-image` 返回 temp 图片 URL；pointCloud 字段附带文件格式、点数、投影轴、单位比例和米制包络。
既有图像接口可显式覆盖 RADAR_HEAD_URL；响应必须能解码，错误 JSON/HTML 不作为图片缓存。

- RADAR_LAZ_AXES：默认 xz，前一轴横向，后一轴向上。现场有效文件到达后需核对实际坐标安装方向。
- RADAR_LAZ_METERS_PER_UNIT：默认 1，即坐标单位按米；毫米坐标应配置 0.001。LAS scales 是坐标量化比例，本身不能证明物理单位；未自动推断单位或 CRS。
- RADAR_LAZ_RESOLUTION：默认 0.01 米/像素；图像最长边超过 4096 时，两轴同步降分辨率，并把实际米/像素返回前端。
- 图像最多 4096×4096；下载最多128MiB，点数最多1000万，点记录解压预算512MiB；分块读取，渲染最多50万点，包络仍统计所有有效点。
- 不把点云包络高度当成车高。Vehicle-Height/Vehicle-SX 仅沿用上游物理测量头；没有头时保留现有默认车高和内容起点逻辑。真实车头定位/物理尺寸仍需现场样本校准。
- 下载与解码均在真实线程池执行，避免占用雷达距离推送的 gevent 线程。
- 每次刷新使用新图，不按固定接口 URL 复用上一辆车的图。
- 中间件少发14字节的已知问题仅对完整 status=false JSON 兼容读取，并明确报“尚未生成有效点云”；截断二进制拒绝解码。
- 前端拖线使用实际图片区域，留白不参与距离换算。

目前现场接口返回 status=false，尚无真实 LAZ 文件；合成压缩样本能验证解压、坐标和比例处理，无法替代现场坐标方向、单位、车头定位验收。

## 辐射仪

现场已有 `$NTRMC,RADIOMETER,...` 遥测，不是本次增加设备。一般为辐射剂量率监测仪；具体型号、单位与阈值尚未确认。本次未增加其控制、解析或页面功能。

## 测试

后端 `python -m unittest discover -s tests -v`；新增测试覆盖真实压缩 LAZ 1.2/1.4、比例/偏移、多投影/单位、采样/大小限制、空/损坏/截断数据、真实本地 HTTP 长度错误、预约刷新缓存，以及抓拍路由和 LED 双向新命令。
前端 `npm run build`；`npx vite --config tests/radar/vite.config.ts` 的 `/tests/radar/index.html` 验证宽/方/竖图、红线坐标和长度换算，可实际拖动首图红线。
