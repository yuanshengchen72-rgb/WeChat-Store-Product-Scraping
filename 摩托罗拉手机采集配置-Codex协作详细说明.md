# 摩托罗拉手机配置为微信小店采集机——Codex 协作详细说明

适用范围：摩托罗拉 Android 手机，Windows 电脑，配合“微信小店商品列表采集”便携版使用。

文档目的：把本文件、便携版 ZIP 和 Magisk 官方 APK 发给另一位使用 Codex 的人。对方可以把本文件交给 Codex 阅读，然后由用户与 Codex 分工完成配置。

> 这不是普通软件安装。完整流程包含 Bootloader 解锁、恢复出厂设置、刷写启动分区和 Root，操作不当可能丢失数据或导致手机无法启动。只处理本人所有或已获明确授权的设备。

## 一、先看结论：需要哪些条件

一台新的摩托罗拉手机要用于采集，最终必须满足：

1. 该型号可以通过摩托罗拉官方渠道解锁 Bootloader。
2. Bootloader 已解锁。
3. 使用这台手机当前精确固件中的启动镜像，在这台手机上通过 Magisk 完成修补并刷回。
4. Magisk Root 正常，`Shell` 已被永久授予 Root 权限。
5. 开启开发者选项和 USB 调试。
6. 手机 CPU 为采集便携版支持的架构。当前便携版内置的是 `arm64-v8a` 对应的 Frida Server 17.17.0。
7. 微信已安装并登录，能够进入“微信小店”的关键词搜索结果页。
8. 用数据线连接一次，在采集软件中点击“一键配置新手机”。
9. 如需无线使用，再点击“通过 USB 启用 WiFi”。

如果手机已经完成 Magisk Root，可以直接从本文“第七部分：配置采集软件”开始。

## 二、需要发送给对方的文件

建议发送以下三项：

1. `微信小店商品采集-一键配置新手机便携版-20260901.zip`
2. Magisk 官方 APK，例如 `Magisk-v30.7.apk`
3. 本说明文件

不要发送以下文件：

- 任何手机的 `magisk_patched-*.img`
- 其他手机的 `boot.img`、`init_boot.img` 或 `recovery.img`
- 任何设备的 IMEI、序列号、摩托罗拉解锁码

Magisk 官方明确警告：不要刷别人分享的修补镜像，即使两台手机型号相同也不行；必须在目标手机上修补属于该手机当前固件的镜像。

## 三、用户与 Codex 的分工

### 用户必须亲自在手机上完成的操作

- 解锁屏幕并确认 USB 调试授权。
- 登录摩托罗拉网站并接收该设备专属解锁码。
- 在 Bootloader 确认界面用音量键和电源键确认解锁。
- 完成手机恢复出厂后的首次设置。
- 在 Magisk 中选择镜像并点击“修补文件”。
- 在 Magisk 的“超级用户”页面永久允许 `[SharedUID] Shell` 或 `com.android.shell`。
- 登录微信并进入关键词搜索结果页。

### 可以让 Codex 完成的操作

- 查找 ADB、Fastboot 和已连接设备。
- 读取型号、序列号、CPU 架构、Android 版本、内部版本号和当前槽位。
- 获取并整理 Bootloader Device ID。
- 校验 Bootloader 是否真的解锁。
- 监控 Software Fix 下载目录，找到并备份精确固件镜像。
- 安装 Magisk APK，向手机推送原始镜像，拉回修补镜像。
- 计算并记录镜像大小与 SHA-256。
- 在明确核对目标后刷写正确分区。
- 验证 Root，安装并启动 Frida Server。
- 启用 WiFi ADB 并验证采集链路。

### Codex 在两个危险步骤前必须暂停并让用户确认

1. 执行 Bootloader 解锁命令前。
2. 执行任何 `fastboot flash` 命令前。

确认信息至少应包含：设备序列号、准确型号、系统版本、当前槽位、目标分区、镜像完整路径、镜像大小和 SHA-256。

## 四、第一阶段：准备手机和电脑

### 1. 备份并移除不必要的账户

Bootloader 解锁会清除手机中的所有数据。摩托罗拉 Software Fix 的 Rescue/刷机也会恢复出厂设置。先备份照片、文件、联系人、验证码和聊天资料。

如果手机已经登录 Google 账户，Software Fix 完成后可能要求再次登录原账户。为了避免忘记账户导致无法激活，应提前确认账号密码；若需要移除账户，应在手机正常开机时从系统设置中移除。

### 2. 电量与连接

- 手机电量建议在 60% 以上。
- 使用稳定、支持数据传输的 USB 数据线。
- 直接连接电脑 USB 接口，尽量不要经过扩展坞或不稳定的集线器。
- 配置期间不要让电脑休眠。
- 同一时间只连接一台需要操作的 USB 手机。

### 3. 安装电脑端工具

在 Windows 上安装摩托罗拉官方 Software Fix。它可以识别 IMEI、匹配官方固件，并通常会安装所需驱动。

另准备：

- 采集便携版 ZIP，解压到普通本地目录。
- Magisk 官方 APK。
- ADB/Fastboot。采集便携版内已带 ADB；Software Fix 也通常带有 ADB/Fastboot。

不要混用来历不明的刷机包、第三方“一键 Root”工具或别人提供的修补镜像。

## 五、第二阶段：开启开发者选项和解锁 Bootloader

### 1. 打开开发者选项

摩托罗拉常见路径：

1. 打开“设置”。
2. 进入“关于手机”。
3. 找到“版本号/内部版本号（Build number）”。有些系统需先进入“Android 版本”。
4. 连续点击 7 次。
5. 输入锁屏密码，系统提示已进入开发者模式。
6. 返回“设置 → 系统 → 开发者选项”。
7. 开启“允许 OEM 解锁/OEM 解锁”。
8. 开启“USB 调试”。

如果“OEM 解锁”不存在或无法开启：

- 先让手机联网并完成正常激活，再重试。
- 检查该设备是否为运营商定制且不允许官方解锁。
- 交给摩托罗拉官方解锁页面判断资格。
- 不要尝试绕过 OEM 锁。

### 2. 允许 USB 调试

1. 手机正常开机并解锁。
2. 连接数据线。
3. 手机出现“是否允许 USB 调试”时，勾选“始终允许使用这台计算机”。
4. 点击“允许”。

让 Codex 验证：

```powershell
adb devices -l
```

正确状态应为 `device`。如果显示 `unauthorized`，需要查看手机屏幕并允许；如果完全没有设备，检查数据线、USB 模式和驱动。

多台设备同时出现时，后续每条命令都必须使用：

```powershell
adb -s <目标序列号> ...
fastboot -s <目标序列号> ...
```

禁止在多设备环境中省略 `-s <目标序列号>`。

### 3. 取得摩托罗拉 Device ID

让 Codex先记录设备信息：

```powershell
adb -s <序列号> shell getprop ro.product.model
adb -s <序列号> shell getprop ro.product.device
adb -s <序列号> shell getprop ro.build.version.release
adb -s <序列号> shell getprop ro.build.version.incremental
adb -s <序列号> reboot bootloader
fastboot -s <序列号> devices
```

确认 Fastboot 能看到同一台设备后，取得 Device ID：

```powershell
fastboot -s <序列号> oem get_unlock_data
```

终端通常返回多行字符串。让 Codex完成以下处理：

1. 删除每行前面的 `(bootloader)`。
2. 删除所有空格、换行和多余字符。
3. 拼成一整行 Device ID。
4. 只把 Device ID 私下交给设备所有者，不公开发布。

### 4. 在摩托罗拉官方页面申请解锁码

1. 打开摩托罗拉官方 Bootloader 解锁页面。
2. 登录摩托罗拉/Lenovo 账户。
3. 粘贴整理后的 Device ID。
4. 点击检查设备是否可解锁。
5. 阅读并接受风险条款。
6. 请求解锁码。
7. 从邮件中取得设备专属解锁码。

解锁码只属于这一台手机。不要把某台手机的解锁码用于另一台手机，也不要公开分享。

### 5. 执行解锁

此步骤会清除全部数据。Codex 应先再次显示目标序列号、型号和即将执行的命令，并得到用户明确确认。

典型命令：

```powershell
fastboot -s <序列号> oem unlock <该设备专属解锁码>
```

然后观察手机屏幕：

1. 使用音量键选择 `Unlock the bootloader`、`Yes` 或同义选项。
2. 按电源键确认。
3. 不要拔数据线。
4. 如果终端明确要求再次发送同一命令，只在核对序列号和解锁码后再发送一次。

手机会恢复出厂并重新启动，首次启动可能较慢。

### 6. 解锁后的验证

完成首次设置，再次打开开发者选项和 USB 调试，然后让 Codex读取：

```powershell
adb -s <序列号> shell getprop ro.boot.flash.locked
adb -s <序列号> shell getprop ro.boot.vbmeta.device_state
```

常见已解锁结果：

- `ro.boot.flash.locked` 为 `0`
- `ro.boot.vbmeta.device_state` 为 `unlocked`

也可进入 Bootloader 后检查：

```powershell
fastboot -s <序列号> getvar unlocked
fastboot -s <序列号> getvar current-slot
```

不同型号显示格式可能不同，Codex 应结合多个字段判断，不要只凭开机警告画面判断。

## 六、第三阶段：取得精确固件并安装 Magisk Root

这一阶段最容易出错。核心原则只有一句：

> 只能修补并刷写“目标手机当前正在运行的精确固件版本”中的启动镜像，而且修补动作必须在目标手机本机完成。

### 1. 记录当前系统版本

让 Codex保存以下输出：

```powershell
adb -s <序列号> shell getprop ro.product.model
adb -s <序列号> shell getprop ro.product.device
adb -s <序列号> shell getprop ro.build.version.release
adb -s <序列号> shell getprop ro.build.version.incremental
adb -s <序列号> shell getprop ro.build.fingerprint
adb -s <序列号> shell getprop ro.boot.slot_suffix
```

建议以以下格式建立独立工作目录：

```text
手机Root工作区/<型号>-<内部版本号>-<序列号后四位>/
```

目录中保存设备信息、原始镜像、修补镜像和哈希记录。不同手机绝不混放。

### 2. 用 Software Fix 匹配官方固件

1. 打开摩托罗拉 Software Fix 并登录。
2. 选择“手机”。
3. 优先使用 Fastboot 连接识别设备，或输入该手机自己的 15 位 IMEI。
4. 确认软件识别出的准确型号，不要选择“差不多”的型号。
5. 点击“下载”，等待固件下载和解压完成。

Software Fix 常见下载/解压位置：

```text
C:\ProgramData\RSA\Download\RomFiles
```

实际位置可能因版本而不同，让 Codex搜索 Software Fix 当前下载目录中的：

```text
boot.img
init_boot.img
recovery.img
flashfile.xml
servicefile.xml
*.xml.zip
```

Software Fix 可能在刷机结束或关闭程序后删除解压文件，所以应在文件出现后立即复制到该手机的独立工作目录。

### 3. 判断是否需要先运行官方 Rescue/刷机

Codex 必须比较：

- 手机当前内部版本号/指纹
- Software Fix 匹配到的固件版本

处理规则：

- 如果两者精确匹配，可以直接备份该固件中的启动镜像。
- 如果两者不匹配，不要直接刷其中的 `boot` 或 `init_boot`。
- 最稳妥做法是先用 Software Fix 对手机执行完整官方 Rescue/刷机，使手机运行该下载包对应的完整系统；这个过程会清除数据。
- 官方刷机完成后，重新完成首次设置、开发者选项和 USB 调试，再次读取版本确认匹配。

不要只因为型号相同就认为镜像可用。型号相同但内部版本、地区、运营商或安全补丁不同，启动镜像也可能不同。

### 4. 确定应该修补哪个镜像

先在 Magisk 首页查看 `Ramdisk` 状态，并检查固件包中有哪些镜像及刷机 XML 如何映射分区。

一般原则来自 Magisk 官方说明：

- 固件中存在 `init_boot.img` 时，现代设备通常修补 `init_boot.img`。
- 没有 `init_boot.img`、且 Magisk 显示 `Ramdisk: Yes` 时，通常修补 `boot.img`。
- `Ramdisk: No` 的设备可能需要 `recovery.img` 和 Magisk Recovery 模式，不能套用普通流程。

Codex 不得猜测。应同时检查：

1. 固件实际文件列表。
2. `flashfile.xml`/`servicefile.xml` 中该文件写入的分区。
3. `fastboot getvar current-slot`。
4. Magisk 首页的 `Ramdisk` 状态。

如果仍无法确定，停止并针对准确型号、准确版本查官方或可信技术资料，不要试刷。

### 5. 安装 Magisk 并推送原始镜像

让 Codex确认 APK 来源与版本，再安装：

```powershell
adb -s <序列号> install -r "<Magisk官方APK完整路径>"
```

把确定的原始镜像推送到手机：

```powershell
adb -s <序列号> push "<原始boot或init_boot完整路径>" /sdcard/Download/
```

Codex 应在电脑端先计算并记录原始镜像：

```powershell
Get-Item -LiteralPath "<原始镜像>" | Select-Object FullName,Length
Get-FileHash -Algorithm SHA256 -LiteralPath "<原始镜像>"
```

### 6. 用户在目标手机上修补镜像

1. 打开 Magisk。
2. 在 Magisk 卡片中点击“安装”。
3. 选择“选择并修补一个文件”。
4. 进入“下载/Download”，选择刚推送的原始镜像。
5. 等待出现 `All done`。

必须在目标手机上操作。不要拿另一台手机代为修补。

### 7. 拉回修补镜像并保存

让 Codex查找修补结果：

```powershell
adb -s <序列号> shell ls -lt /sdcard/Download/magisk_patched*.img
```

拉回电脑：

```powershell
adb -s <序列号> pull /sdcard/Download/<实际修补文件名>.img "<该手机独立工作目录>\"
```

建议重命名：

```text
<型号>-<内部版本>-magisk-<Magisk版本>.img
```

然后记录文件大小和 SHA-256。原始镜像与修补镜像都要保留。

### 8. 刷写修补镜像

先进入 Bootloader：

```powershell
adb -s <序列号> reboot bootloader
fastboot -s <序列号> devices
fastboot -s <序列号> getvar current-slot
```

Codex 此时必须停止，向用户显示并核对：

- Fastboot 序列号是否仍是目标设备。
- 型号与固件版本。
- 当前槽位，例如 `a` 或 `b`。
- 应写入 `boot`、`init_boot` 还是 `recovery`。
- 是否需要带槽位后缀，例如 `boot_a`。
- 修补镜像完整路径、大小和 SHA-256。
- 原始镜像备份位置。

用户明确确认后，Codex 才能执行实际命令。下面只是格式示例，不能原样照抄：

```powershell
fastboot -s <序列号> flash <经核实的目标分区> "<修补镜像完整路径>"
fastboot -s <序列号> reboot
```

例如，某台 A/B 设备当前槽位是 `a`，固件 XML 明确把 `boot.img` 写入 `boot_a`，目标才可能是 `boot_a`。另一型号可能完全不同。

不要在没有明确需要时随意刷 `vbmeta`、`vendor_boot`、`dtbo`、`super` 或其他分区。不要执行全盘擦除命令。不要在修改后的启动镜像仍存在时重新锁定 Bootloader。

### 9. 验证 Root

手机开机后：

1. 打开 Magisk。
2. 如果提示“需要额外设置/环境修复”，允许它完成并重启。
3. 重新连接 USB 并允许调试。
4. 让 Codex执行：

```powershell
adb -s <序列号> shell su -c id
```

第一次执行时，手机可能弹出 Root 授权：

1. 打开 Magisk →“超级用户”。
2. 找到 `[SharedUID] Shell` 或 `com.android.shell`。
3. 打开授权，选择永久允许。
4. 再次执行命令。

成功结果必须包含：

```text
uid=0(root)
```

只看到 Magisk 图标或 Bootloader 警告，不代表 Root 已成功；以 `su -c id` 为准。

## 七、第四阶段：配置微信小店采集软件

### 1. 安装并准备微信

1. 在手机安装正常可用的微信。
2. 登录用于采集的微信账户。
3. 确认能够进入“微信小店/小店与卡包”。
4. 进入关键词搜索结果页做一次人工测试。

只采集本人有权处理的公开商品信息，遵守微信平台规则、适用法律和合理的请求频率。

### 2. 解压并启动便携版

1. 完整解压便携版 ZIP，不要直接在压缩包内运行。
2. 双击：

```text
微信小店商品列表采集\微信小店商品列表采集.exe
```

3. 选择“安卓手机关键词采集”。

便携版不要求另外安装 Python 或电脑端 Frida。

### 3. 一键配置新手机

1. 同一时间只保留这一台目标手机通过 USB 连接。
2. 手机保持解锁，USB 调试已允许。
3. 点击“一键配置新手机”。
4. 软件会自动：
   - 识别手机型号和 CPU 架构；
   - 检查 Magisk Root；
   - 上传 Frida Server 17.17.0；
   - 设置执行权限；
   - 启动 Frida Server；
   - 从电脑端枚举手机进程，验证通信。
5. 如果 Magisk 弹出 Shell 授权，选择永久允许。

成功提示应类似：

```text
手机 Root 权限检查通过
Frida Server 已运行
配置成功；Frida Server 17.17.0 已就绪
```

如果软件提示 CPU 架构不支持，应停止，不要把 arm64 文件强行上传到其他架构。

### 4. USB 采集测试

1. 软件连接方式选择“USB数据线”。
2. 手机微信进入“小店与卡包”，打开任意一个商品搜索结果页，并保持微信在前台。
3. 在软件“关键词列表”中每行填写一个完整关键词。
4. 如需限定价格，在“预设价格”填写整数元。例如最低价 `50`、最高价 `500`；任一项留空表示该方向不限，两个都留空表示不限价格。该区间对本批全部关键词生效。
5. 点击“开始采集”。软件会逐个自动输入关键词并点击搜索。
6. 每个关键词出现稳定结果后，软件会自动打开“筛选/已筛选”，选择“好评多”，清理页面旧价格并填写预设区间，然后点击“确定”。
7. 软件自动向下滚动；到达底部且连续没有新商品后自动切换下一个关键词，不需要设置滑动次数。
8. 全部关键词完成后自动停止并导出数据。
9. 检查统一数据表中是否出现商品 ID、关键词、店铺名、标题、价格、销量和主图链接。

普通页面失败时，软件会等待 60 秒后重试当前关键词，最多重试 3 次。一个关键词重试后仍失败，会记录并跳到下一个；若紧接着的下一个关键词也重试失败，整批任务才暂停。中间任一关键词成功后，连续失败计数会清零。等待期间点击“停止”会立即结束，不必等满一分钟。

软件会在 `data\关键词采集汇总\keyword_checkpoint.json` 保存完整关键词列表、价格区间和当前位置。手动停止、异常暂停或软件重开后，会自动回填并从断点继续；全部关键词完成后自动清除断点。登录失效、安全验证、操作频繁等需要人工处理的情况会立即暂停，不进行自动重试。

先完成 USB 测试，再配置 WiFi，便于区分 Root/Frida 问题与网络问题。

## 八、第五阶段：启用 WiFi 无线采集

### 1. 首次启用

1. 电脑和手机连接同一个可信 WiFi。
2. 手机仍用数据线连接电脑。
3. 软件中点击“通过 USB 启用 WiFi”。
4. 等待软件自动取得手机局域网地址并连接，格式通常为：

```text
192.168.x.x:5555
```

5. 软件提示连接成功后，连接方式选择“WiFi无线”。
6. 拔掉数据线，再进行一次采集测试。

该功能本质上会执行类似操作：

```powershell
adb -s <USB序列号> tcpip 5555
adb connect <手机局域网IP>:5555
```

### 2. WiFi 使用注意事项

- 只在可信局域网中使用，不要在公共 WiFi、酒店 WiFi 或陌生网络上开放 ADB 5555。
- 电脑和手机必须能够互相访问；访客网络或路由器“设备隔离”会导致连接失败。
- 手机 IP 变化后，需要更新软件中的地址。
- 手机重启后，Frida Server 会由采集软件自动重新启动；但 ADB WiFi 通常需要重新插线并再次点击“通过 USB 启用 WiFi”。
- 使用结束后若希望关闭网络 ADB，可插线执行 `adb -s <USB序列号> usb`，恢复为 USB 模式。
- 可在路由器中为手机设置 DHCP 地址保留，减少 IP 变化，但这不是必需条件。

## 九、完成标准

只有以下项目全部通过，才算一台新手机配置完成：

- [ ] Bootloader 官方解锁成功。
- [ ] 当前系统版本和用于修补的固件版本精确匹配。
- [ ] 原始启动镜像已单独备份并记录 SHA-256。
- [ ] 修补镜像由目标手机本机生成并记录 SHA-256。
- [ ] 修补镜像刷入经固件 XML与槽位共同确认的正确分区。
- [ ] 手机能正常开机。
- [ ] Magisk 显示已安装。
- [ ] `adb shell su -c id` 返回 `uid=0(root)`。
- [ ] Magisk 已永久允许 Shell Root。
- [ ] “一键配置新手机”显示 Frida 17.17.0 通信成功。
- [ ] USB 模式能采集到至少一条商品。
- [ ] 如需无线，WiFi 模式拔线后也能采集。
- [ ] 原始镜像、修补镜像和设备信息记录已安全保存。

## 十、常见故障与处理

### `adb devices` 没有设备

- 换一根确认支持数据传输的线。
- 更换电脑 USB 接口。
- 解锁手机并查看 USB 调试弹窗。
- 将 USB 用途切换为“文件传输”。
- 安装或重装 Software Fix/摩托罗拉驱动。

### 显示 `unauthorized`

- 解锁手机，点击允许 USB 调试。
- 在开发者选项中撤销 USB 调试授权，拔插数据线后重新允许。

### Fastboot 看不到设备

- 确认手机画面确实是 Bootloader/Fastboot。
- 检查驱动管理器中的摩托罗拉 Fastboot 设备。
- 换线和 USB 接口。
- 不要因为 ADB 能看到就假设 Fastboot 一定能看到。

### 官方页面说设备不可解锁

- 确认 Device ID 已删除 `(bootloader)`、空格和换行。
- 再取一次 Device ID 比较。
- 若官方仍判定不可解锁，停止操作；运营商定制或地区版本可能不支持。
- 不要尝试绕过官方资格检查。

### Software Fix 匹配不到正确型号

- 使用手机自己的 15 位 IMEI，优先尝试 IMEI1。
- 使用 Fastboot 连接识别。
- 不要从列表中挑一个“差不多”的型号。

### Root 命令被拒绝

- 打开 Magisk →“超级用户”。
- 找到 `[SharedUID] Shell`/`com.android.shell` 并允许。
- 保持手机解锁后重试 `adb shell su -c id`。

### Frida Server 启动失败

- 确认 Root 命令已经返回 `uid=0(root)`。
- 点击“一键配置新手机”重新自动上传、授权和验证。
- 确认便携版电脑端和手机端都使用 Frida 17.17.0。
- 只连接一台 USB 手机。

### 手机刷完修补镜像无法启动

立即停止继续试刷：

1. 保持 Bootloader 解锁，不要重新锁定。
2. 进入 Fastboot。
3. 核对序列号、当前槽位和刚才写入的目标分区。
4. 使用该手机同一精确固件中备份的原始镜像恢复相同分区。
5. 如果无法恢复，使用摩托罗拉 Software Fix 对该设备执行完整官方 Rescue；这会清除数据。

不要尝试其他手机的镜像，也不要随机刷 `vbmeta` 或切换槽位。

## 十一、可以直接发给另一位 Codex 的主提示词

把本说明文件上传给 Codex，并粘贴下面这段：

```text
我要把一台摩托罗拉 Android 手机配置成微信小店商品采集机。请完整阅读我上传的《摩托罗拉手机配置为微信小店采集机——Codex 协作详细说明》后再操作。

请先只做只读检查：找到 ADB/Fastboot、列出设备、记录准确序列号、型号、device、Android 版本、内部版本号、fingerprint、CPU 架构和当前连接状态。任何命令都必须用 -s 指定目标序列号，不能误操作其他手机。

分阶段协助我完成：
1. 开启并验证开发者选项、OEM 解锁和 USB 调试；
2. 获取并整理摩托罗拉 Bootloader Device ID；
3. 等我从官方页面取得本机解锁码；
4. 解锁前先明确告诉我会清除数据，并在执行命令前让我确认；
5. 用 Software Fix 匹配这台手机的精确官方固件，比较当前版本；
6. 立即备份正确的 boot/init_boot/recovery 镜像和固件 XML，记录大小和 SHA-256；
7. 安装 Magisk，把正确原始镜像推到本机，让我在本机修补，再拉回电脑；
8. 刷写前再次显示序列号、型号、版本、槽位、目标分区、镜像路径、大小和 SHA-256，等我确认后才能 fastboot flash；
9. 验证 Magisk Root 和 Shell 永久授权；
10. 打开采集便携版的一键配置新手机功能，安装并验证 Frida Server 17.17.0；
11. 完成 USB 采集测试，再启用 WiFi 采集。

安全规则：绝不使用别的手机的修补镜像；即使同型号也不共用；不猜测目标分区；固件版本不完全匹配时不单刷启动镜像；不随意刷 vbmeta/vendor_boot/dtbo/super；不执行全盘擦除命令；修改系统后不重新锁定 Bootloader；遇到不确定情况先停止并说明证据。

现在从只读检查开始，不要直接解锁或刷写。
```

## 十二、官方参考资料

- [摩托罗拉 Bootloader 解锁入口](https://en-us.support.motorola.com/app/home/device/app/standalone/topic/bootloader/unlock-your-device-a)
- [摩托罗拉 Software Fix Rescue 官方说明](https://en-us.support.motorola.com/app/answers/detail/a_id/167770/p/30,)
- [Magisk 官方安装说明](https://topjohnwu.github.io/Magisk/install.html)
- [Android 官方 ADB 与无线调试说明](https://developer.android.com/tools/adb)

官方资料的关键提醒：Bootloader 解锁和 Software Fix Rescue 都可能清除数据；Magisk 要求使用目标设备精确固件的镜像，并明确反对共享或跨设备使用修补镜像。
