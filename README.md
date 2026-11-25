# Richi Proxy

Richi Proxy 为多个客户项目提供统一的视频生成 API。客户使用各自的业务 Key，服务端使用配置的官方 Ark Key 提交和查询任务。

这是独立重建版的开发起点。当前包含客户账号、业务 Key、异步 Seedance 任务、本地素材上传与登记、URL 素材登记和逐项目用量查询，以及客户侧概览、API 接入和用量页面。

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
| 上传本地素材到服务器暂存 | `POST /api/asset/upload-file` |
| 查询本业务 Key 的上传状态 | `GET /api/asset/upload-file/{uploadId}` |
| 登记暂存素材或公网 HTTPS 素材 | `POST /api/asset/create` |
| 列出素材与查询处理状态 | `GET /api/asset/list` 和 `GET /api/asset/get?assetId=...` |

提交后使用同一业务 Key 查询本站任务 ID。提交时建议提供 `Idempotency-Key`，使用同一个键重试同一请求体。上游结果 URL 可能过期。每日提交次数由项目共享，默认每个项目 5 次。配置官方 Ark Key 后，应用可后台补查正在排队或运行且已知上游 ID 的任务。

初始支持范围为国内 Seedance 2.5、2.0 的标准创建和查询。Draft、4K、视频编辑、延长和取消接口仍待接入。

本地开发可以在不配置官方凭证的情况下验证注册、业务 Key 和客户页面。配置服务器环境变量 `SEEDANCE_ARK_API_KEY` 后，视频请求会发往火山并可能产生费用。自动测试使用 `MockTransport` 和临时 SQLite。

素材登记需配置服务端资产 AK/SK 与共享的火山项目名。视频推理凭证的火山项目需与素材项目匹配。

### 上传本地素材

通过业务 Key 调用 `POST /api/asset/upload-file`，以 `multipart/form-data` 发送必填的 `file` 和可选的 `assetType`。省略 `assetType` 时由服务端识别。文件内容须与声明的媒体类型一致，并通过解码校验。

| `assetType` | 支持的格式 | 单文件上限 |
| --- | --- | --- |
| `Image` | JPEG、PNG、WebP、GIF、BMP、TIFF | 30 MiB |
| `Video` | MP4、MOV | 200 MiB |
| `Audio` | WAV、MP3 | 15 MiB |

解码时同时检查资源上限。图片单帧最多 3600 万像素，所有帧累计最多 1.44 亿像素。视频最多 3600 帧，累计最多 162 亿像素。音频最多 1 亿个样本，各声道分别计数。

```powershell
curl.exe "$env:BASE_URL/api/asset/upload-file" `
  -H "Authorization: Bearer $env:API_KEY" `
  -H "Idempotency-Key: portrait-upload-001" `
  -F "file=@./portrait.png;type=image/png" `
  -F "assetType=Image"
```

首次成功返回 HTTP `201`，包括 `uploadId`、`assetType`、`contentType`、`sizeBytes`、`status` 和 UTC `expiresAt`。可选的 `Idempotency-Key` 须为 1 至 128 个可见 ASCII 字符。同一业务 Key 用同一个幂等键重试同一文件会复用原上传记录，已暂存时返回 `200`。同一个幂等键上传不同文件会返回 `409 idempotency_key_conflict`。使用原业务 Key 调用 `GET /api/asset/upload-file/{uploadId}` 查询状态，其他 Key 查询会得到 `404 upload_not_found`。

| 状态或错误 | 处理方式 |
| --- | --- |
| `uploading` 或 `409 upload_in_progress` | 稍后用原业务 Key 查询状态或用原幂等键重试 |
| `staged` | 文件已存入服务器暂存目录，保留 `uploadId` 和 `expiresAt` |
| `502 upload_storage_failed` | 保存 `detail.uploadId`，查询状态等待清理完成 |
| `cleanup_pending` | 等待服务端清理暂存对象 |
| `failed` | 清理已完成，可用原幂等键重传同一文件，沿用 `uploadId` |
| `expired` 或 `409 upload_expired` | 暂存已过期，使用新的幂等键上传 |
| `413 file_too_large`、`415 media_type_mismatch` / `unsupported_media_type` 或 `422 invalid_media` | 核对文件大小、内容和声明类型 |
| `415 multipart_required` 或 `422 invalid_upload_form` | 核对 `multipart/form-data` 及表单字段 |
| `503 upload_storage_unavailable` | 联系服务运营方核对服务器暂存目录 |
| `409 upload_storage_changed` | 旧试验记录使用了不同的存储方式，以新的幂等键上传 |

### 登记素材并用于生成

通过 `POST /api/asset-group/create` 创建本站素材组，再向 `POST /api/asset/create` 发送 JSON。请求包含 `groupId`、`name`、`assetType`，并且在 `uploadId` 和 `url` 中填写一项。使用 `uploadId` 时，必须使用上传时的业务 Key，且 `assetType` 必须与暂存记录一致。使用 `url` 时，填写公网 HTTPS 地址。提交本站取件链接会返回 `422 upload_source_requires_id`。

方舟登记时会另行校验文件格式、尺寸、时长等限制。 [方舟素材登记接口](https://docs.volcengine.com/docs/ark/create-asset-api?lang=zh)

```powershell
$assetRequest = @{
  groupId = $env:GROUP_ID
  uploadId = $env:UPLOAD_ID
  name = "portrait"
  assetType = "Image"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$env:BASE_URL/api/asset/create" `
  -Headers @{ Authorization = "Bearer $env:API_KEY" } -ContentType "application/json" `
  -Body $assetRequest
```

首次登记成功返回 `201`，素材标识在 `asset.id`。对同一 `uploadId` 重放相同登记请求时返回已有结果及 `200`。改用同项目内其他素材组或不同名称会返回 `409 upload_registration_conflict`，类型不符会返回 `422 asset_media_type_mismatch`。方舟明确拒绝时可用原 `uploadId` 重试。方舟创建结果未知时，首次请求返回 `502`，重试返回 `409 upload_registration_pending`，文件和记录保留用于核对。使用 `GET /api/asset/get?assetId=...` 查询素材状态。达到 `Active` 后，将返回的 `reference` 传入生成请求，本站验证归属并映射为上游素材。

### 命令行素材接入示例

在当前进程环境中设置业务 `API_KEY` 和已开通素材与视频能力的 `BASE_URL`，从仓库根目录运行以下命令。远程实例使用 HTTPS，本机回环地址可使用 HTTP。示例创建素材组、上传本地图片并用 `uploadId` 登记，等素材进入 `Active` 后提交引用该素材的视频任务。通过已配置真实方舟凭证的实例调用可能产生费用。 [完整脚本](examples/asset_video.py)

```powershell
.\.venv\Scripts\python.exe -B .\examples\asset_video.py `
  --base-url "$env:BASE_URL" --image .\portrait.png
```

示例提交 4 秒、480p 的视频任务。`--model` 默认使用 `doubao-seedance-2.5`，可改为项目已启用的模型。`--poll-interval` 和 `--asset-timeout` 分别设置素材轮询间隔与最长等待时间，默认 2 秒和 120 秒。任务被受理后，标准输出为单条 JSON，包含 `uploadId`、`assetId`、`reference` 和 `taskId`。使用同一业务 Key 查询 `GET /api/v3/contents/generations/tasks/{id}` 获取视频任务进度。

请求失败时，标准错误输出会标明请求阶段、HTTP 状态及业务错误码（如有），进程以非零状态退出。素材进入 `Failed` 或等待超时时，可通过素材列表定位素材并查询状态。每次运行会创建新的素材组和上传记录。方舟登记或视频提交结果未知时，先核对项目记录再发起新流程。

在 `backend/` 目录运行以下测试，可在本机使用临时数据库、暂存目录和模拟方舟复现完整命令行调用，并检查客户隔离及失败处理。

```powershell
..\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_asset_workflow_cli.py
```

### 服务器暂存配置

默认将上传内容保存到 SQLite 同级的 `asset-uploads/`，可通过 `SEEDANCE_UPLOAD_DIRECTORY` 改为服务器进程可读写的持久目录。暂存默认有效 172800 秒（2 天），通过 `SEEDANCE_UPLOAD_TTL_SECONDS` 在 60 至 604800 秒之间调整。后台默认每 60 秒清理一次，通过 `SEEDANCE_UPLOAD_CLEANUP_SECONDS` 调整，设为 `0` 时关闭后台清理。旧 TOS 试验记录保留在 SQLite 中，文件系统清理只处理服务器暂存文件。 [示例配置](.env.example)

使用 `uploadId` 登记需要将 `SEEDANCE_UPLOAD_PUBLIC_ORIGIN` 设为方舟可访问的 HTTPS 根地址。留空时仍可上传文件和查询上传状态，使用 `uploadId` 登记会返回 `503 upload_registration_unavailable`。服务端为方舟生成有限期的取件链接，`SEEDANCE_UPLOAD_LINK_TTL_SECONDS` 默认 3600 秒，可设为 60 至 86400 秒。链接由随机令牌和已验证的文件扩展名组成，SQLite 只存令牌摘要。原暂存期不足时，文件会保留到取件链接到期。上传和登记响应返回本站资源标识，取件链接由服务端交给方舟。

反向代理须将 `/api/asset/source/` 下的 `GET` 和 `HEAD` 转发给后端，并透传 `Range` 请求头。关闭该路径的代理访问日志，确保错误日志也不会写入取件令牌。应用会跳过该路径的 Uvicorn 访问日志。将暂存目录设在网站静态根目录之外，通过后端按令牌提供文件。真实方舟联调需确认公网 HTTPS 可达、素材与推理凭证对应同一项目，并取得调用授权。

## 测试与当前边界

```powershell
cd backend
..\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider
```

前端可用 `pnpm test` 验证 API 客户端，使用 `pnpm build` 生成静态文件。设置正式的 `SEEDANCE_SITE_ORIGIN` 后可生成公开首页的 canonical 和站点地图。客户用量页按北京时间的任务创建月份展示已记录的任务和上游输出用量，缺失 Token 保留为空。价目、折扣结算、火山账单导入、Native 支付、开票和生产环境验收列入首期后续交付。

## 许可证

本项目以 [Apache-2.0](LICENSE) 许可发布。客户工作台的信息架构参考 [New API 的公开示例](https://github.com/QuantumNous/new-api#screenshots)，源代码和界面资产为独立实现。

## 开发协作

仓库工作规则见 [AGENTS.md](AGENTS.md)。任务顺序和验收标准在 [工作计划](docs/WORK_PLAN.md)，已完成事项和验证记录在 [工作日志](docs/WORK_LOG.md)。
