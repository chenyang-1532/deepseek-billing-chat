# DeepSeek Chat Demo 自服务器部署指南

> 源码：[deepseek-ai/chat-demo](https://github.com/deepseek-ai/chat-demo) · MIT 协议 · 本包为 v0.1.1 官方源码在 Node 22.13 下 `pnpm build` 的产物，未做任何改动。

## 一、多用户隔离是怎么实现的（重要）

这个应用是**纯静态、无后端**的，所有用户数据都不经过、也不存放在你的服务器上：

| 数据 | 存放位置 | 隔离方式 |
|---|---|---|
| API Key 和设置 | 用户自己浏览器的 `localStorage` | 按浏览器 + 站点域名隔离 |
| 对话记录、附件 | 用户自己浏览器的 `IndexedDB` | 同上 |
| 导入/导出 | ZIP 文件，用户手动操作 | 本地文件 |

也就是说：**只要每个用户用自己的设备/浏览器访问，信息和 API Key 天然互相隔离**，A 用户看不到 B 用户的任何东西，服务器上没有任何数据库需要维护，也不存在用户数据泄露风险（服务器被黑也拿不到任何聊天记录）。

两个必须注意的边界情况：

1. **同一台电脑共用浏览器** 的两个人会共享同一份 localStorage/IndexedDB（不算隔离）。如需严格区分，建议各用各的浏览器 Profile 或各自设备。
2. **API Key 是每人填自己的**。切勿把你的 Key 告诉用户或在群里发，否则所有人的费用都算你的账号。

如果你需要「用户在服务器上注册登录、换设备也能同步聊天记录」这类能力，静态站做不到，需要另行开发后端（可作为二期）。

## 二、部署步骤（3 步）

```bash
# 1. 上传解压（把 deepseek-chat-demo-dist.zip 传到服务器）
unzip deepseek-chat-demo-dist.zip -d /var/www/
# 得到 /var/www/dist（index.html + static/ + LICENSE）

# 2. 用 Nginx 托管（配置见下）

# 3. 浏览器打开你的域名，进入 设置 → 填入各自的 DeepSeek API Key 即可聊天
```

API Key 获取：[platform.deepseek.com](https://platform.deepseek.com) → API keys → 创建（按 token 计费，充值后使用）。

## 三、Nginx 配置示例

```nginx
server {
    listen 80;
    server_name chat.yourdomain.com;          # 换成你的域名
    root /var/www/dist;
    index index.html;

    gzip on;
    gzip_types text/css application/javascript application/wasm image/svg+xml;
    gzip_min_length 1024;

    # 静态资源带内容哈希，可长期缓存
    location /static/ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

上线后建议加 HTTPS（用 certbot 免费申请证书，`listen 443 ssl` 即可），避免部分浏览器对 HTTP 站点限制存储 API。

## 四、可选：用反向代理统一 API 出口

默认构建直接从用户浏览器调用 `https://api.deepseek.com`。如果遇到跨域(CORS)报错，或希望 API 请求从你的域名统一走，可二选一：

**方案 A：Nginx 反向代理（推荐，无需重新构建）**

```nginx
    # 在上面的 server 块里追加
    location /dsapi/ {
        proxy_pass https://api.deepseek.com/;
        proxy_ssl_server_name on;
        proxy_set_header Host api.deepseek.com;
    }
```

然后重新构建前端，把 API 地址指到同域路径：

```bash
# 修改 rspack.config.ts 中默认值：
#   apiOrigin = 'https://api.deepseek.com'  →  apiOrigin = '/dsapi'
pnpm build
```

**方案 B：不改任何东西直接用**。官方托管版（github.io 域名）就是浏览器直连官方 API 的，一般域名的 HTTPS 站点也直接可用；只有真遇到 CORS 报错时才需要方案 A。

## 五、常见问题

| 问题 | 处理 |
|---|---|
| 打开是空白页 | 确认部署的是 `dist` 目录本身（`index.html` 必须在站点根），而不是外层多套了一层文件夹 |
| 提示 402 | 用户的 DeepSeek 账户余额不足，去平台充值 |
| 提示 429 | 触发并发上限（flash 单账号 2500 并发），与前端无关 |
| 想换 API 地址 | 见上文方案 A，改 `rspack.config.ts` 的 `apiOrigin` 后 `pnpm build` |
| 升级版本 | `git clone` 最新源码 → `pnpm install && pnpm build` → 覆盖 `dist` 目录，用户浏览器缓存会自动失效（文件名带哈希） |

## 六、合规提醒

- 发行包内已包含 MIT 许可证副本（`LICENSE`），请保留该文件。
- 本站只托管界面；所有模型调用费用由填 Key 的用户各自承担。
