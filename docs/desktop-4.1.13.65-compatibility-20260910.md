# 微信 4.1.13.65 / WMPF 25560 静态适配

## 请求与边界

用户文字写 6.1.13.65，截图“关于微信”明确为 **4.1.13.65**，按截图适配。本机已安装并运行的是 4.1.13.63；本轮没有安装、升级、执行下载的微信，也没有挂载正在采集的进程。v11 继承 v10 恢复逻辑，不代表前一轮真实恢复验证已经完成。

## 官方输入

- [微软 winget 4.1.13.65 安装清单](https://raw.githubusercontent.com/microsoft/winget-pkgs/master/manifests/t/Tencent/WeChat/Universal/4.1.13.65/Tencent.WeChat.Universal.installer.yaml)
- 清单中的腾讯地址：`https://dldir1v6.qq.com/weixin/Universal/Windows/WeChatWin_4.1.13.exe`
- 安装包 SHA256：`A1978685C5D12EE6ADC372A6348FFE1C9B2CA95A59787E4ED66279436B57C5E1`，下载后完全一致。
- 安装包、两个提取 DLL 的 Authenticode 均 Valid，签署者 Tencent Technology (Shenzhen) Company Limited。
- 只用现有 7-Zip 静态提取 NSIS 中 install.7z，再提取 Weixin.dll 和 RadiumWMPF.bin 中的 flue.dll；从未运行安装程序。
- 真实 Weixin.dll 版本 4.1.13.65；file_component.xml 和 flue.dll 都确认 WMPF 25560（2.5.6.25560）。

| 文件 | 字节数 | SHA256 |
| --- | ---: | --- |
| Weixin.dll 4.1.13.65 | 198060584 | `8f7406a8a465e851ee10eaecccb572d0e5b4e00d480c770938c714c396097b74` |
| flue.dll 25560 | 236554288 | `60630cc1ba346b0e485fbf1981a07a650d1566e004c5b2b84933544860e2b3cf` |

## 红测试与定位

先用真实提取文件调用 `validate_desktop_profile`，旧代码立即抛出与截图一致的“当前电脑版微信内核版本暂未适配，现支持 4.1.13.63”。

排名假设：仅签名/资源变化；函数位置移动；接口逻辑变化。静态比较确认代码段确有变化，并且旧的 IPC、load_start、cdp_filter 地址在新版已位于其他函数中段，绝不能只放开版本检查。

工具 `tools/diagnostics/compare_desktop_binaries.py` 比较 PE `.pdata` 函数范围和完整函数字节。归一化只屏蔽 RIP 相对地址与指向函数外的直接分支目标；保留指令、字段偏移、常量、函数内分支。每个接口在新版只有一个完整匹配；可解析的一级直接调用目标完整函数归一化也一致，引用的可打印协议字符串一致。这支持入口搬迁，不能证明所有运行时对象/间接调用行为完全相同。

| 接口 | 旧地址 | 新地址 |
| --- | --- | --- |
| 微信 IPC task | `0x5443e10` | `0x5444400` |
| WMPF OnCommandCall | `0x26a7de0` | `0x26a7de0` |
| WMPF Reply | `0x26a9a10` | `0x26a9a10` |
| WMPF OnLoadStart | `0x2ce7c50` | `0x2ce7cf0` |
| WMPF CDP filter | `0x3979560` | `0x3979600` |

报告保存在 `build/compat-4.1.13.65/static-comparison.json`。原始 DLL 不随便携包发布。

## 实现与验证

- `SUPPORTED_PROFILES` 明确列出 .63/.65 与 25558/25560 四种组合；每组必须同时匹配两个文件的大小与完整 SHA256，按实际组件选对应入口，不混用偏移。未知版本和同尺寸未知字节仍拒绝。
- Scene 指针链维持 `(64,1536,8,1472,16,456)` 和原有坏指针/已知场景检查。该运行时指针链尚未在 25560 实例中实测，不能宣称已完成端到端验证。
- 新组合写明日志“静态接口核对，尚待真实采集验证”。错误提示提供两文件的大小或哈希标识，便于后续核对组件独立更新。
- 新增注册表选择、旧版保持、未知组合/同大小篡改拒绝、缓存失效、生成脚本使用新入口回归测试。首次红测试出现 1 FAIL / 5 ERROR；修改后全套 **232 项离线测试通过**。
- 使用真实 .65/25560 文件重跑原失败调用，现在正确返回 `微信 4.1.13.65 / WMPF 25560`。
- `tools/diagnostics/verify_desktop_profiles_on_disk.py` 用真实文件验证四组身份匹配，并要求所有五个入口落在对应 PE 函数起点。

## 待目标电脑验证

关闭旧采集器后，在 4.1.13.65 电脑使用 v11：测试自动恢复 → 真实关键词完整读到末页 → 停止/重新开始 → 再次正常小程序恢复。未执行这些步骤前，本版只能称为静态适配候选，不能称为该版本已实测稳定。无需为本次验证升级本机正在使用的微信。

## v11 交付自检

- 构建目录：`build/desktop-recovery-v11/微信小店商品列表采集`。
- EXE SHA256：`3C67F83278C1CC343335592715CF69A52D98092183692E7BBF8175368BFCC0E6`。
- 完整便携包：`dist/微信小店商品采集-v11-20260910-Windows64-完整便携版.zip`，106098476 字节、1314 个文件。
- ZIP SHA256：`98A96AA9A42A2414B602607CAD659BB918B946C189C9BC5290E3EBB92599D964`。
- 白名单打包后解压，逐文件 SHA256 与构建源一致；不含腾讯安装包、微信 DLL、用户数据、授权或保存目录偏好。
- 构建 EXE 和从 ZIP 解压的 EXE 均以 `--self-test` 在正常 Windows 环境运行，等待实际进程退出，退出码均为 0。这只验证离线依赖、资源与解析流程，没有挂载微信或进行真实采集。
