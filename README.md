# Richi Proxy

Richi Proxy 为多个客户项目提供统一的视频生成 API。客户使用各自的业务 Key，服务端使用配置的官方 Ark Key 提交和查询任务。

这是独立重建版的开发起点。当前包含客户账号、业务 Key、异步 Seedance 任务、本地素材上传与登记、URL 素材登记和逐项目用量查询，以及客户侧概览、API 接入和用量页面。

## 本地运行

后端需要 Python 3.11 及以上，并要求 Python 的 SQLite 运行库为 3.35.0 及以上。用 `python -c "import sqlite3; print(sqlite3.sqlite_version)"` 检查实际版本。客户工作台需要 Node.js 22.13 及以上和 pnpm。后端从运行进程的环境变量读取配置，默认使用本地 SQLite。 [SQLite RETURNING 支持说明](https://www.sqlite.org/lang_returning.html) · [示例配置](.env.example)

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

## 工作台视频体验和任务记录

- 登录工作台后打开「视频体验」。输入本项目的业务 Key，或在页面上创建一枚用于体验的 Key。选择 Seedance 模型、视频参数和参考素材，填写提示词并生成。页面展示当前任务的进度、用量和可用结果。业务 Key 只保留在本次工作台会话的页面内存中。
- 「任务记录」可查看本项目通过 API 和视频体验提交的任务。支持按完整任务 ID、北京时间提交日期范围和状态筛选，分页查看模型、提交和结束时间、输出 Token 以及失败说明。查阅视频需要创建该任务时使用的业务 Key，结果地址可能过期。
- 工作台使用登录会话读取 `GET /api/customer/tasks`。可选参数为 `taskId`、`fromDate`、`toDate`、`status`、`page` 和 `pageSize`。起止日期包含当天，默认每页 20 条，最多 100 条。`GET /api/customer/playground/key` 同时校验会话和 `Authorization: Bearer <业务 Key>`。本项目的有效 Key 返回 `204`。视频、素材和任务结果仍使用下文的业务 API。

## API 接入

客户账号密码长度为 8 至 128 个字符。客户登录后可领取或撤销站内业务 Key。凭证只在创建时返回完整值。调用下列接口使用 `Authorization: Bearer <业务 Key>`。

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

下列模型支持标准视频创建和查询。通过 `GET /v1/models` 获取当前可用的业务别名和参数限制。 [方舟模型能力对比](https://docs.volcengine.com/docs/ark/video-generation-tutorial?lang=zh)

| `model` 业务别名 | 方舟模型版本 | 生成时长 | 输出分辨率 |
| --- | --- | --- | --- |
| `doubao-seedance-2.5` | `doubao-seedance-2-5-260628` | 4–30 秒 | 480p、720p、1080p、4K |
| `doubao-seedance-2.5-flash` | `doubao-seedance-2-5-flash-261115` | 4–30 秒 | 480p、720p |
| `doubao-seedance-2.0` | `doubao-seedance-2-0-260128` | 4–15 秒 | 480p、720p、1080p、4K |
| `doubao-seedance-2.0-fast` | `doubao-seedance-2-0-fast-260128` | 4–15 秒 | 480p、720p |
| `doubao-seedance-2.0-mini` | `doubao-seedance-2-0-mini-260615` | 4–15 秒 | 480p、720p |

`GET /v1/models` 的 `data[].id` 用于提交任务。`data[].video` 提供 `max_duration`（秒）、`resolutions`、`audio_only` 和 `max_reference_images`、`max_reference_videos`、`max_reference_audios`。`frame_ratio_adaptive` 为 `true` 时，首帧或首尾帧生成只接受省略 `ratio` 或设为 `adaptive`。单独输入参考音频适用于 2.5 和 2.5 Flash。2.0 系列使用参考音频时要一起提供参考图片或视频。API 也可将 `duration` 设为 `-1` 交由方舟选择时长。Draft、视频编辑、视频延长和取消接口仍待接入。

标准版的 4K 请求使用 `"resolution": "4k"`，实际分辨率由任务查询返回。方舟 4K 视频使用 10 位 H.265（HEVC）编码。浏览器无法预览时，可以下载后用兼容的本地播放器打开。 [方舟创建视频任务](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)

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

示例提交 4 秒、480p 的视频任务。`--model` 默认使用 `doubao-seedance-2.5`，也可填写 `GET /v1/models` 返回的业务别名。`--poll-interval` 和 `--asset-timeout` 分别设置素材轮询间隔与最长等待时间，默认 2 秒和 120 秒。任务被受理后，标准输出为单条 JSON，包含 `uploadId`、`assetId`、`reference` 和 `taskId`。使用同一业务 Key 查询 `GET /api/v3/contents/generations/tasks/{id}` 获取视频任务进度。

请求失败时，标准错误输出会标明请求阶段、HTTP 状态及业务错误码（如有），进程以非零状态退出。素材进入 `Failed` 或等待超时时，可通过素材列表定位素材并查询状态。每次运行会创建新的素材组和上传记录。方舟登记或视频提交结果未知时，先核对项目记录再发起新流程。

在 `backend/` 目录运行以下测试，可在本机使用临时数据库、暂存目录和模拟方舟复现从文件上传、登记和生成任务，到计价、账单冻结和供应商账单对比的全流程。用例还会检查客户隔离及失败处理。

```powershell
..\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_asset_workflow_cli.py
```

### 服务器暂存配置

默认将上传内容保存到 SQLite 同级的 `asset-uploads/`，可通过 `SEEDANCE_UPLOAD_DIRECTORY` 改为服务器进程可读写的持久目录。暂存默认有效 172800 秒（2 天），通过 `SEEDANCE_UPLOAD_TTL_SECONDS` 在 60 至 604800 秒之间调整。后台默认每 60 秒清理一次，通过 `SEEDANCE_UPLOAD_CLEANUP_SECONDS` 调整，设为 `0` 时关闭后台清理。旧 TOS 试验记录保留在 SQLite 中，文件系统清理只处理服务器暂存文件。 [示例配置](.env.example)

使用 `uploadId` 登记需要将 `SEEDANCE_UPLOAD_PUBLIC_ORIGIN` 设为方舟可访问的 HTTPS 根地址。留空时仍可上传文件和查询上传状态，使用 `uploadId` 登记会返回 `503 upload_registration_unavailable`。服务端为方舟生成有限期的取件链接，`SEEDANCE_UPLOAD_LINK_TTL_SECONDS` 默认 3600 秒，可设为 60 至 86400 秒。链接由随机令牌和已验证的文件扩展名组成，SQLite 只存令牌摘要。原暂存期不足时，文件会保留到取件链接到期。上传和登记响应返回本站资源标识，取件链接由服务端交给方舟。

反向代理须将 `/api/asset/source/` 下的 `GET` 和 `HEAD` 转发给后端，并透传 `Range` 请求头。关闭该路径的代理访问日志，确保错误日志也不会写入取件令牌。应用会跳过该路径的 Uvicorn 访问日志。将暂存目录设在网站静态根目录之外，通过后端按令牌提供文件。真实方舟联调需确认公网 HTTPS 可达、素材与推理凭证对应同一项目，并取得调用授权。

## 逐次用量计价

成功的 Seedance 视频任务按任务 ID 归集一次，使用方舟查询响应的 `usage.completion_tokens`、实际输出分辨率（480p、720p、1080p、4K）、提交时的确切方舟模型版本以及是否含 `video_url` 输入。方舟按实际输出分辨率和视频输入区分每百万 Token 单价，返回的 `completion_tokens` 已反映适用的最低用量。 [方舟模型价格](https://docs.volcengine.com/docs/ark/model-pricing?lang=zh&redirect=1) · [方舟任务查询](https://docs.volcengine.com/docs/ark/get-video-generation-task-api?lang=zh)

项目默认未计费。按任务创建时间（UTC）匹配已生效的客户价目和项目计费条款，价目由运营录入。已开通项目的成功任务缺少真实用量、计价条件、适用价目或任务结束时间时进入待计价。项目折扣以应付比例的基点表达，`8000` 表示按原价的 80% 支付（8 折）。折前和折后金额各自四舍五入到整数微元，响应以人民币元六位小数的字符串返回。

工作台通过客户会话读取 `GET /api/customer/usage?month=YYYY-MM`。`summary.pricedCalls` 为所选月份的已计价任务数。`items[].billing` 为 `null` 或计价明细，其 `status` 可为 `priced`（已计价）、`pending`（待计价）或 `unbilled`（未计费）。其他字段包括 `pendingReason`、`resolution`、`containsVideoInput`、`priceVersion`、`termsVersion`、`unitPrice`、`payableBps`、`listAmount` 和 `amount`。`unitPrice` 为人民币元／百万 Token，`listAmount` 为折前金额，`amount` 为折后金额。

### 配置价目与项目折扣

运营在 `backend/` 目录运行以下命令，指定已初始化的 SQLite 文件、客户项目 ID、约定单价和来源。`BILLING_EFFECTIVE_AT` 使用带时区、精确到秒的 ISO 8601 时间，例如 `2026-12-01T00:00:00+08:00`。按相同方式为适用的模型、输出分辨率和视频输入档位分别录入价目。标准版的 4K 价目使用 `--resolution 4k`。2.5 Flash、2.0 Fast 和 2.0 Mini 的价目按各自的方舟模型版本录入，包含 480p 和 720p 的有视频输入、无视频输入档位。

```powershell
..\.venv\Scripts\python.exe -B -m app.billing_cli --database "$env:BILLING_DB" add-price `
  --upstream-model doubao-seedance-2-5-260628 --resolution 720p --video-input no `
  --effective-at "$env:BILLING_EFFECTIVE_AT" --yuan-per-million "$env:BILLING_RATE" `
  --source "$env:BILLING_PRICE_SOURCE"

..\.venv\Scripts\python.exe -B -m app.billing_cli --database "$env:BILLING_DB" set-project-terms `
  --project-id "$env:PROJECT_ID" --effective-at "$env:BILLING_EFFECTIVE_AT" --enabled --payable-bps 8000

..\.venv\Scripts\python.exe -B -m app.billing_cli --database "$env:BILLING_DB" reconcile
```

`add-price` 返回 `priceVersion`，`set-project-terms` 返回 `termsVersion`。追加 `--disabled` 条款可从指定时间起停用项目计费。应用启动和视频后台查单时补录遗漏的成功任务。在资料补齐后运行 `reconcile` 重查遗漏或待计价任务，返回 `processedTasks`。已计价条目保留当时的价目与折扣版本。

## 月度账单

账单按北京时间的任务结束月份归集，工作台的用量明细仍按任务创建月份查询。成功任务以方舟查询响应中有效的 `updated_at` 确认结束时间。缺少有效时间时保留已返回的用量。已启用计费的任务进入待计价状态，暂缓归集。已计价任务的折后金额先按微元汇总，再对账单总额四舍五入至分。 [方舟任务查询](https://docs.volcengine.com/docs/ark/get-video-generation-task-api?lang=zh)

本月及尚未确认的往期账单显示预估。运营确认已结束的账期后，金额、任务明细及确认时待计价数量一并冻结。同一账期重复确认会返回已有账单。确认后才补齐的用量列入来源账期之后按时间最早的开放账期，其明细保留 `sourceMonth`，标记为 `late_usage`。

通过客户会话读取当前项目的账单。

| 操作 | 接口 |
| --- | --- |
| 查询月份的预估或已确认账单 | `GET /api/customer/billing?month=YYYY-MM&page=1&pageSize=50` |
| 按账单 ID 查询已确认明细 | `GET /api/customer/billing/{statementId}?page=1&pageSize=50` |
| 下载已确认账单完整明细 | `GET /api/customer/billing/{statementId}/export.csv` |

省略 `month` 时默认本月，分页默认每页 50 条，最多 100 条。预估的 `status` 为 `estimate`，`id` 为空。已确认的 `status` 为 `confirmed`，`id` 为账单 ID。`summary` 返回本期已计价任务数 `usageCalls`、补计任务数 `lateCalls`、待计价任务数 `pendingCalls` 和折前、折后及应付金额。明细金额和折前、折后汇总为六位小数的人民币元字符串，`summary.payableAmount` 为两位小数。CSV 使用带 BOM 的 UTF-8，导出包含所有页的已确认明细。

运营在 `backend/` 目录按项目确认一个已结束的账期，`STATEMENT_MONTH` 格式为 `YYYY-MM`。

```powershell
..\.venv\Scripts\python.exe -B -m app.billing_cli --database "$env:BILLING_DB" confirm-statement `
  --project-id "$env:PROJECT_ID" --month "$env:STATEMENT_MONTH"
```

确认命令返回 `statementId`、`month`、`status` 和 `payableAmount`，客户可在工作台选择账期查看和下载。

## 火山账单导入与对账

运营可离线导入火山费用中心 `ListBillDetail` 的计费项明细。准备样本时按 `PayerID`、`OwnerID`、`Product` 和 `BillPeriod` 选定范围，使用 `GroupTerm=0`、`GroupPeriod=2`、`NeedRecordNum=1`、`IgnoreZero=0`。文件为 UTF-8 编码的一页完整 JSON 响应，或包含全部响应页的 JSON 数组，每页保留 `ResponseMetadata.Action`、`Result.List` 和 `Result.Total`。 [火山账单明细接口](https://docs.volcengine.com/docs/billing-center/list-bill-detail-page-query-bill-details?lang=zh)

导入核对明细的账号、产品、账务账期、人民币币种和明细 ID，并用唯一明细数核对分页完整性。带筛选降级警告的响应需要重新获取。将账单文件和导出的对账报告存放在仓库外的授权目录。控制台 CSV 和分账账单的导入待取得真实样本后另行接入。

在 `backend/` 目录运行以下命令。`VOLC_BILL_JSON` 指向完整响应文件，`VOLC_BILL_MONTH` 为 `YYYY-MM` 格式的账务账期。产品编码以账单明细为准。

```powershell
..\.venv\Scripts\python.exe -B -m app.billing_cli --database "$env:BILLING_DB" import-provider-bill `
  --file "$env:VOLC_BILL_JSON" --payer-id "$env:VOLC_PAYER_ID" --owner-id "$env:VOLC_OWNER_ID" `
  --product "$env:VOLC_BILL_PRODUCT" --month "$env:VOLC_BILL_MONTH"

..\.venv\Scripts\python.exe -B -m app.billing_cli --database "$env:BILLING_DB" provider-cost-report `
  --payer-id "$env:VOLC_PAYER_ID" --owner-id "$env:VOLC_OWNER_ID" `
  --product "$env:VOLC_BILL_PRODUCT" --month "$env:VOLC_BILL_MONTH"
```

重复导入当前有效版本的同一明细会复用该版本。对已导入范围切换到更新后的完整账单时，使用 `import-provider-bill --replace`，旧版本保留供核查。导入返回 `importId`、`rows` 和 `disposition`。

报告的 `provider` 部分保留供应商原金额精度，分别汇总原价 `OriginalBillAmount`、优惠金额 `PreferentialBillAmount`、折后价 `DiscountBillAmount`、应付 `PayableAmount` 和现金支付 `PaidAmount`。代金券、抹零、信控额度退款抵扣及欠费各按原字段汇总。任一明细缺少某项支付数据时，该字段的合计保留为 `null`。现金支付、信控退款抵扣及欠费均完整时，`paymentBalanceDifference` 返回应付减去三项之和，供核对供应商金额组成。`customers` 独立汇总本站各项目在所选月份的已确认账单应付。 [账单金额说明](https://docs.volcengine.com/docs/billing-center/bill-management-1?lang=zh) · [支付字段](https://docs.volcengine.com/docs/BillingCenter/ListBill-PageQueryBill?lang=zh)

核实供应商账单与本站成功任务的用量覆盖范围一致后，可在 `provider-cost-report` 加上 `--confirm-usage-scope` 做模型级 Token 核对。匹配要求供应商业务账期 `BusiPeriod` 与所选月份一致，明细为按量消费使用，`Unit` 为 `Token` 或 `Tokens`，`Count` 为非负整数。`ConfigName` 可直接匹配已知上游模型版本，也可通过 `--model-map` 提供 JSON 对象，键为账单配置名，值为已核实的上游模型版本。

`usage` 返回模型用量比较和未匹配的供应商明细、本站任务及原因。只有覆盖范围明确、相关模型和用量完整时才计算 `tokenDifference`。费用中心分账可按方舟接入点或官方 API Key 资源 ID 查看聚合用量，本报告按模型核对，保留无法关联到单次视频任务的明细。 [方舟账单分账](https://docs.volcengine.com/docs/ark/practice-guidance-for-modelark-bill-split?lang=zh)

## 测试与当前边界

```powershell
cd backend
..\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider
```

前端可用 `pnpm test` 验证 API 客户端，使用 `pnpm build` 生成静态文件。设置正式的 `SEEDANCE_SITE_ORIGIN` 后可生成公开首页的 canonical 和站点地图。测试站点已实测从公网 HTTPS 上传到方舟取件和 Seedance 2.5 生成。火山月账单样本核验和生产计费口径仍待确认。Native 支付、开票和公开站点 SEO 已暂停。 [工作计划](docs/WORK_PLAN.md)

## 许可证

本项目以 [Apache-2.0](LICENSE) 许可发布。客户工作台的信息架构参考 [New API 的公开示例](https://github.com/QuantumNous/new-api#screenshots)，源代码和界面资产为独立实现。

## 开发协作

仓库工作规则见 [AGENTS.md](AGENTS.md)。任务顺序和验收标准在 [工作计划](docs/WORK_PLAN.md)，已完成事项和验证记录在 [工作日志](docs/WORK_LOG.md)。
