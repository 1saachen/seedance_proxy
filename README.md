# Richi Proxy

Richi Proxy 为多个客户项目提供统一的视频生成 API。客户使用各自的业务 Key，服务端使用配置的官方 Ark Key 提交和查询任务。

这是独立重建版的开发起点。当前包含客户账号、业务 Key、异步 Seedance 任务、URL 素材登记和逐项目用量查询，以及客户侧概览、API 接入和用量页面。

## 本地运行

安装 Python 3.11 及以上、Node.js 22.13 及以上和 pnpm。后端从运行进程的环境变量读取配置，默认使用本地 SQLite。配置项见 [示例配置](.env.example)。

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "./backend[test]"
$env:SEEDANCE_COOKIE_SECURE = "false"
$env:SEEDANCE_REGISTRATION_ENABLED = "true"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

在另一个终端启动客户工作台：

```powershell
cd web
pnpm install --frozen-lockfile
pnpm dev
```

通过前端控制台打印的本地地址访问，客户工作台入口为 `/console/`。开发服务器将 API 请求代理到本地后端。公开部署前应启用 HTTPS 和 Secure Cookie，并控制客户开通流程。注册默认关闭。

## API 接入

客户登录后领取或撤销站内业务 Key。凭证只在创建时返回完整值。调用下列接口使用 `Authorization: Bearer <业务 Key>`。

| 能力 | 接口 |
| --- | --- |
| 已启用视频模型 | `GET /v1/models` |
| 提交 Seedance 视频任务 | `POST /api/v3/contents/generations/tasks` |
| 查询任务和实际输出 Token | `GET /api/v3/contents/generations/tasks/{id}` |
| 创建与列出本项目素材组 | `POST /api/asset-group/create` 和 `GET /api/asset-group/list` |
| 登记公网 HTTPS 素材地址 | `POST /api/asset/create` |
| 列出素材与查询处理状态 | `GET /api/asset/list` 和 `GET /api/asset/get?assetId=...` |

提交后使用同一业务 Key 查询本站任务 ID。提交时建议提供 `Idempotency-Key`，使用同一个键重试同一请求体。上游结果 URL 可能过期。每日提交次数由项目共享，默认每个项目 5 次。配置官方 Ark Key 后，应用可后台补查正在排队或运行且已知上游 ID 的任务。

初始支持范围为国内 Seedance 2.5、2.0 的标准创建和查询。Draft、4K、视频编辑、延长和取消接口仍待接入。

本地开发可以在不配置官方凭证的情况下验证注册、业务 Key 和客户页面。配置服务器环境变量 `SEEDANCE_ARK_API_KEY` 后，视频请求会发往火山并可能产生费用。自动测试使用 `MockTransport` 和临时 SQLite。

素材接口需配置服务端资产 AK/SK 与共享的火山项目名。视频推理凭证的火山项目需与素材项目匹配。创建素材组后，将本站素材组 ID、公开 HTTPS URL、素材名称和类型提交登记。素材状态为 `Active` 时使用本站返回的 `reference`，由中转站检查归属并映射到上游素材。文件上传与 TOS 中转将在后续接入。

## 测试与当前边界

```powershell
cd backend
..\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider
```

前端可用 `pnpm test` 验证 API 客户端，使用 `pnpm build` 生成静态文件。设置正式的 `SEEDANCE_SITE_ORIGIN` 后可生成公开首页的 canonical 和站点地图。客户用量页按北京时间的任务创建月份展示已记录的任务和上游输出用量，缺失 Token 保留为空。价目、折扣结算、火山账单导入、Native 支付、开票和生产环境验收列入首期后续交付。

## 许可证

本项目以 [Apache-2.0](LICENSE) 许可发布。客户工作台的信息架构参考 [New API 的公开示例](https://github.com/QuantumNous/new-api#screenshots)，源代码和界面资产为独立实现。
