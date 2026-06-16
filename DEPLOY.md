# 稳赢量化交易系统 - 部署指南

## 快速部署（推荐：Railway）

### 第一步：准备 GitHub 仓库

```bash
cd /path/to/quant-trading-system

# 初始化 Git
git init
git add .
git commit -m "Initial commit: 稳赢量化交易系统"

# 在 GitHub 上创建仓库（需先安装 gh CLI）
gh repo create quant-trading-system --public --source=. --push
```

或者手动操作：
1. 登录 [GitHub](https://github.com) 创建一个新仓库（如 `quant-trading-system`）
2. 按上面命令初始化本地 Git 并推送到远程

### 第二步：部署到 Railway

1. 访问 [railway.app](https://railway.app) 并用 GitHub 账号登录
2. 点击 **"New Project"** → **"Deploy from GitHub repo"**
3. 选择你的 `quant-trading-system` 仓库
4. Railway 会自动检测 Dockerfile 并构建
5. 构建完成后，点击 **"Generate Domain"** 获取公网地址

完成！你的系统现在可以通过 `https://xxx.up.railway.app` 访问。

### 持久化数据（可选）

Railway 支持 Volume 挂载来持久化交易状态：

1. 在项目设置中点击 **"Volumes"**
2. 添加一个 Volume，挂载路径填 `/app/data`
3. 重新部署即可

---

## 备选方案：Render

### 部署步骤

1. 访问 [render.com](https://render.com) 并用 GitHub 账号登录
2. 点击 **"New +"** → **"Web Service"**
3. 连接你的 GitHub 仓库
4. 配置：
   - **Name**: `quant-trading-system`
   - **Environment**: `Docker`
   - **Plan**: `Free`
5. 点击 **"Create Web Service"**

Render 会自动使用 `render.yaml` 和 Dockerfile 进行部署。

> ⚠️ **注意**：Render 免费版会在 15 分钟无访问后休眠，下次访问需等待 ~30 秒启动。
> 数据存储在临时文件系统，重启后会丢失（除非升级到付费计划添加持久磁盘）。

---

## 本地 Docker 测试

部署前可以在本地用 Docker 验证：

```bash
cd quant-trading-system

# 构建镜像
docker build -t quant-trading .

# 运行（映射端口 8050）
docker run -p 8050:8050 quant-trading

# 访问 http://localhost:8050
```

---

## 环境变量说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8050` | 服务端口（Railway/Render 自动注入） |
| `HOST` | `0.0.0.0` | 绑定地址 |
| `FLASK_DEBUG` | `false` | 是否开启调试模式 |

---

## 系统架构

```
用户浏览器
    │
    ▼
Railway/Render (Nginx 反向代理)
    │
    ▼
Gunicorn (WSGI Server, 2 workers)
    │
    ▼
Flask App (dashboard/app.py)
    │
    ├── /api/kline → 东方财富K线 (curl subprocess)
    ├── /api/stock_search → 股票搜索
    ├── /api/smart/* → 智能选股交易
    ├── /api/daily/* → 每日实盘
    └── / → 暗色主题仪表盘
```

---

## 常见问题

### Q: 东方财富 API 在服务器上能用吗？
A: 可以。系统通过 `curl` 子进程调用东方财富 API，绕过了 Python SSL 兼容性问题。但注意：
- 国内服务器（阿里云/腾讯云）访问东方财富更快更稳定
- Railway 的服务器在海外，可能会有偶尔延迟，但基本可用
- 如遇到限流，系统会自动降级到备用数据源（新浪/腾讯/模拟数据）

### Q: 免费额度够用吗？
A: Railway 每月 $5 免费额度，大约可以运行一个小型实例 500 小时。如果只需偶尔使用，完全够用。Render 免费版不限时长但有休眠机制。

### Q: 如何添加用户认证？
A: 当前系统没有登录功能。如需限制访问，可以：
1. Railway：在项目设置中开启 "Basic Auth"（免费功能）
2. 自行添加 Flask-Login 认证模块
