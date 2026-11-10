`vehicle.laz` 仅用于离线解压、三维测量和投影回归测试，业务服务不会读取此文件。

真实采集使用 `RADAR_POINT_CLOUD_URL`；无数据时等待，不回退到测试文件。
