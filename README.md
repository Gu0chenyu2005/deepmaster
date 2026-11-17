# deepmaster

足臂机器人数据采集与审核平台，采用服务端与采集端分离架构，支持设备管理、三相机实时预览、遥操作采集及数据审核。

- `server/`：服务端与采集端代理代码
- `assets/`、`index.html`：网页界面
- `orin/`：采集端相机与数据路径适配
- `packaging/`：部署脚本与说明
- `tests/`：测试

服务端使用 Ubuntu；采集端使用 Ubuntu aarch64，需安装 ROS Humble 和 RealSense 驱动。

部署：[服务端](packaging/README-server.md)  [采集端](packaging/README-orin.md)

网页地址：`http://服务器IP:8790`。连接在线设备后，在“程序配置”中填写 DDS 主机信息。
