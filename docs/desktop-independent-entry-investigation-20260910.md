# 独立启动入口：现场排查，尚未完成

## 目标与范围

用户要求内置独立启动信息，宿主失效后无需文件传输助手窗口即可正常打开小店，接着恢复当前关键词。v12 的窗口/卡片定位不满足此目标。本记录不是 v13 发布说明。

用户关闭采集器、打开 lining李宁 店铺，并授权短暂操作小店菜单。操作范围只在小店：查看标题栏右键菜单、关闭菜单、查看“微信小店”徽标面板并收起。没有进入其他聊天、发送消息、退出微信或操作登录。

## 已取得的证据

1. `python tools/diagnostics/independent_launch_probe.py 49856`，旧源码现场结果：

   ```text
   BEFORE {"pid": 56760, "window": false, "shop_title": false, "initialized": false}
   LEGACY_RETURNED_PID 56760
   AFTER {"pid": 56760, "window": false, "shop_title": false, "initialized": false}
   FAIL: URI dispatch did not initialize a shop; native attach was not attempted
   ```

   这是“已有未初始化后台宿主”的失败复现，不是从完全没有宿主的环境启动。

2. 李宁首页正常显示。所查看的标题栏菜单只有 Windows 系统项；页面“微信小店”徽标打开最近访问店铺面板。此次查看没有发现“添加到桌面”或可导出的独立启动链接。不能据此断言所有版本/所有入口都不支持。

3. 只读分析已安装 .63 DLL：`launchapplet` 处理函数在解析 AppID 后调用 `0x6a9540` 查找一个集合，失败时不进入后续启动。未修改集合、条件或返回值。这与此前正常 URI 不打开小店的现场结果相符；不是新发现了可用入口。

4. 用户再次关闭小店并从文件传输助手正常打开李宁。对已校验 hash 的 Weixin.dll `0x7b51b0` 消息发送点进行 120 秒只读观察，限定小程序相关命令、公开 AppID、无查询参数的页面路径。结果 `CAPTURED_EVENTS 0`、`OBSERVER_REMOVED`。因此这个观察点没有取得该操作的可复用启动请求；不能解释成该操作没有发生，也不能据此宣称无法实现。

## 已改正的局部缺陷

旧 `_launch_wmpf_host` 只看 PID：无窗口/其他窗口都返回成功，且真正没有宿主时错误提示用户检查登录。

新增 `tests/test_desktop_launch_confirmation.py`，修改前四项全失败。修正为等待属于目标宿主的小店/小店与卡包窗口，而不是单凭后台 PID 返回。失败文案明确“启动协议已调用，但未打开小店窗口”，不再暗示需要退出或重新登录微信。

这是第一阶段的窗口确认，不是页面内容确认；原有初始化守卫和搜索运行时校验保留，没有写入伪造的 initialized 标记。没有新增可独立启动的 URI，也没有改变采集、分页、断点或商品存储规则。

## 验证与未完成工作

- `python tools/diagnostics/run_offline_tests.py`：249 项通过，真实 Frida attach/spawn 和产品 UIAutomation 构造均被隔离。
- 当前李宁首页留给用户，未为重复负向测试再次关闭正常页面。新代码尚未在“宿主退出→独立正常唤起→搜索页就绪→继续断点”完整现场链路上通过；没有打包或替换现有可执行版本。
- 下一步需要找到并验证正常店铺启动实际经过的入口，或者微信提供的可重用启动产物。不得以重复旧 URI、保存店铺名称、取消初始化守卫或多等待几次代替该验证。
- 临时脚本均留在 `tools/diagnostics/`，不是生产启动路径；本轮观察器已经移除。
