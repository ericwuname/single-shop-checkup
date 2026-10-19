# M7 · APK 安装手册（WP-6）

> **这是 debug 签名包：仅作者自用，不能上架应用商店。**
> APK 的真正价值是**本地定时提醒**（PWA 在安卓做不可靠）；只是想"手机上能用"，PWA 加主屏幕就够。

---

## 1. 打包（GitHub 云端编译，本机不需要 Android SDK）

1. 推送代码后，打开仓库 **Settings → Secrets and variables → Actions → Variables** 标签
2. 新建变量：名字 `CAP_SERVER_URL`，值 = 你的服务地址（例如 `http://192.168.1.6:8000`——就是电脑的局域网 IP + 8000）
3. 仓库 **Actions** 页 → 左侧选 **android-apk** → **Run workflow** → 等约 5 分钟
4. 完成后进本次 run 页面，底部 **Artifacts** 下载 **app-debug**

## 2. 装进 MuMu 模拟器

- 直接把下载的 `app-debug.apk` **拖进 MuMu 窗口**，它自动安装（或者用 MuMu 的"APK 安装"按钮选这个文件）
- 装好后桌面出现"单店体检"图标

## 3. 第一次打开前

1. 电脑上先把服务跑起来：`scripts\start_server.bat`（默认绑 127.0.0.1；**APK 用远程 URL 模式时改成 `lan`**：`scripts\start_server.bat lan`，绑 0.0.0.0）
2. APK 的 WebView 直接加载你填的 `CAP_SERVER_URL`——**改了网页不用重新打包 APK**
3. 电脑关机 APK 打不开页面（壳还在，数据在电脑上）

## 4. 已知取舍

- **debug 签名**：手机装会提示"未知来源应用"，允许即可；不能上架
- **cleartext http**：局域网 IP 是 http，`capacitor.config.json` 里 `cleartext: true` 是为此开的——**上 HTTPS 后记得关掉**
- **包名**：`com.example.danDianTiJian` 是占位，要改就改 `capacitor.config.json` 的 `appId` 后重新触发打包
- 给**老板**装：仍然不要用 APK（"允许未知来源"一步就会流失一半人）——用 PWA 加主屏幕，或者等 HTTPS 部署
