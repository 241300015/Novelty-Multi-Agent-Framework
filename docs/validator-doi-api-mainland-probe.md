# Validator DOI 元数据 API 大陆直连探针（M0）

日期：2026-10-27。实验分支：`experiment/validator-doi-api-mainland-probe`。
基线：`origin/main` 的 `85ee6a84`。仅增加探针、离线测试及本文档，不接入
`EvidenceValidator`，不使用 LLM、账户或 API Key，不修改依赖、工作流或 Agent。

## 本次执行结果

用户确认：机器在中国大陆，使用大陆运营商校园网；Clash 系统代理指向境外节点，
未开启 TUN，系统代理保持不变。仅测试子进程移除 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY
（大小写均处理），使用原生 socket 建立连接，没有修改 shell、Clash 或全局环境。

**IPv4 一轮预检全部通过；按用户最新要求，当前观测缩短为累计 1 小时，结果待完成。**
默认地址族运行时 DOI.org 首选 IPv6，三个请求均在 TCP 阶段 10 秒超时；Crossref 和
DataCite 使用 IPv4，正负例成功。另建显式 IPv4 运行后，四个正例、三个负例全部成功，
返回 DOI 与预期一致，TLS 证书验证通过。各轮保留独立结果，未自动重试或混算成功率。
这支持“该校园网出口 IPv4 当前可用”，不支持把 IPv6 首地址超时概括为整个 API 不可用。

独立结果目录（均含 samples.jsonl、summary.json，原始文件不提交 Git）：

- `outputs/reference_metadata_probe/20261027T042228Z-4dfe8e1d/`：原环境代理门禁拦截，0 请求，退出码 2。
- `outputs/reference_metadata_probe/20261027T043158Z-6fc2e869/`：默认地址族一轮，4 正例+3 负例，退出码 2。
- `outputs/reference_metadata_probe/20261027T043329Z-bbd49ed0/`：指定 IPv4 一轮，4 正例+3 负例，退出码 0。

运行仍未设置 `--attest-direct`：用户已确认大陆位置和无 TUN，但未对校园网底层透明代理/
VPN 路由作完整确认。汇总保留 `mainland_direct_evidence_eligible=false`；旧状态字段
`NOT_EXECUTED_ON_MAINLAND` 在这里仅表示尚未取得完整操作者确认，不否认已知的大陆位置。
不追溯改写原始样本的确认字段。正式 M0 证据资格与跨出口终验仍待完成。

IPv4 观测于 **2026-10-27 12:34（北京时间）**启动。用户随后要求先观察一小时，
因此保留原有样本，以原启动时刻累计 **1 小时**，计划于 **2026-10-27 13:34**停止。
采样频率仍为每 300 秒一轮、每轮四个正例，预计约 12 轮、48 个正例请求，不重复负例。
调整时已有三轮、12 个请求，全部成功；这些结果保持在原目录中，不重新计时。

启动回执与日志：`outputs/reference_metadata_probe/observation-20261027T043401Z.{json,log}`。
结果目录为 `outputs/reference_metadata_probe/20261027T043401Z-69caec8b/`。
已启动一次性停止控制进程，到期校验目标 PID 和进程启动标识后发送 SIGTERM，让探针保存
最终汇总并退出；控制进程随后退出，不创建持久定时任务。调整原因、停止时间及执行状态
另存于结果目录的 `duration_change.json`，不改写已有样本。

原探针进程的内存参数仍为 288 轮，最终 summary.json 会如实保留 interrupted=true、
stop_reason=interrupted；应结合 duration_change.json 判读为“用户主动缩短为一小时”，
不能将其当作网络故障或完成 24 小时。若机器休眠、断网或进程提前结束，应按实际时长和
样本数报告；本次一小时结果不满足原任务书的 24 小时稳定性验收条件。

离线验证：60 个用例通过（包括地址族选择与禁止回退）。命令（本机系统 `python3` 没有 pytest，使用已有 Novelty 环境）：

```bash
/home/lya3106643285/miniconda3/envs/Novelty/bin/python -m pytest -q tests/test_reference_metadata_probe.py
```

覆盖：四个正例、DOI 替换/大小写、HTML/无效 JSON/错误结构、正负例 404、403/429/重定向、
代理拦截、DNS/TCP/TLS/HTTP 异常与超时、证书错误、IPv6/实际连接地址、无回退重试、
中断落盘、统计口径、24 小时虚拟时钟调度和负例仅首轮执行。测试完全离线；
自动夹具阻止真实 DNS 和 socket 调用。虚拟时钟测试不构成真实 24 小时实测。

| 来源 | 节点/模式 | 正例成功/样本 | 正例成功率 | 成功正例 P50/P95(ms) | 负例通过 | 结论 |
|---|---|---:|---:|---|---:|---|
| DOI.org | 当前校园网，auto 首选 IPv6 | 0/2 | 0% | 无成功样本 | 0/1 | TCP 超时；不代表 IPv4 不可用 |
| Crossref | 当前校园网，auto 选用 IPv4 | 1/1 | 100% | 1681.342 / 1681.342 | 1/1 | 单轮成功 |
| DataCite | 当前校园网，auto 选用 IPv4 | 1/1 | 100% | 3439.576 / 3439.576 | 1/1 | 单轮成功 |
| DOI.org | 当前校园网，指定 IPv4 | 2/2 | 100% | 551.689 / 569.687 | 1/1 | 单轮成功 |
| Crossref | 当前校园网，指定 IPv4 | 1/1 | 100% | 1363.839 / 1363.839 | 1/1 | 单轮成功 |
| DataCite | 当前校园网，指定 IPv4 | 1/1 | 100% | 1631.553 / 1631.553 | 1/1 | 单轮成功 |
| DOI.org | 独立大陆出口 B，待提供 | 0/0 | 未测 | 未测 | 未测 | pending |
| Crossref | 独立大陆出口 B，待提供 | 0/0 | 未测 | 未测 | 未测 | pending |
| DataCite | 独立大陆出口 B，待提供 | 0/0 | 未测 | 未测 | 未测 | pending |

一轮一两个正例的 P50/P95 仅描述当前样本，不是 24 小时延迟分布或可靠性承诺。

## 测试目标与固定用例

验证 DNS、TCP 443、TLS 证书验证、HTTPS 请求及返回 DOI 的 JSON 语义，随后观察低频查询的
成功率、成功样本 P50/P95 与故障类型。本文不验证论文正文，不实现 DOI 真伪判定。

| 用例 | Provider | DOI | 预期 |
|---|---|---|---|
| P1 | DOI.org | `10.1038/nature14539` | HTTP 200、responseCode=1、handle 一致且含 values |
| P2 | DOI.org | `10.14454/qdd3-ps68` | 同上 |
| P3 | Crossref | `10.1038/nature14539` | HTTP 200、status=ok、message.DOI 一致 |
| P4 | DataCite | `10.14454/qdd3-ps68` | HTTP 200、data.type=dois、data.id 一致 |
| N1–N3 | 各 Provider 一次 | `10.1038/novelty-probe-unregistered-<随机 UUID>` | 服务明确返回未找到 |

P3 是 Nature 论文《Deep learning》；P4 是 DataCite 官方示例资源，不是期刊论文。
正例 DOI 比较不区分大小写。负例在显式指定 `--negative-controls` 的首轮各执行一次，
24 小时运行不再携带该参数。随机负例若意外命中，会失败而不是被认定“未注册”。

负例判定保守：DOI.org 要求 HTTP 404、responseCode=100 且 handle 一致；Crossref
接受明确的 `text/plain` “Resource not found” 响应；DataCite 要求 JSON errors 中
status=404 且明确的 not-found 标题。不接受任意 CDN HTML 404。403、429、超时
一律不是“DOI 不存在”。新的未识别错误格式会记录为失败，需人工核查后调整契约。

## 大陆节点前提

操作者必须确认服务器实际部署在大陆，并确认 Region、网络出口、云商/运营商，且没有 VPN、
显式或透明代理。`--attest-direct` 将这份确认及脱敏 `region/network` 写入本地汇总；
脚本不做 IP 地理定位，也不能独立证明机器位置或底层路由。只有标注 Region 而没有确认不够。

默认检查大小写不敏感的 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY；任意非空配置都会拒绝运行，
即使存在 NO_PROXY。只输出变量名，不保存含凭证的代理地址。请在运维已确认的直连会话运行，
不要仅删除变量就宣称大陆无代理；也不要将通过 VPN 切换到大陆的机器当作大陆部署服务器。

建议两个不同地域/运营商/出口 AS 的大陆节点。脱敏标签可以包含机型、云商及 ASN，
不要填账号、服务器公网 IP 或密钥。原始结果中含 API DNS/远端 IP，仅保留本地或私有交付。

## 运行方法

探针只依赖 Python 3.11+ 标准库。运行平台为 Linux/POSIX 主线程，使用 `setitimer`
限制整个 DNS/TCP/TLS/HTTP 阶段；不支持没有该机制的 Windows。

从实验分支根目录执行离线测试：

```bash
python -m pytest -q tests/test_reference_metadata_probe.py
```

在已确认的大陆直连节点预检（下面 region/network 是示例，必须替换成真实脱敏值）：

```bash
python3 scripts/probe_reference_metadata_apis.py \
  --region cn-beijing --network aliyun-ecs-mainland \
  --attest-direct --negative-controls --count 1
```

四个正例和三个负例全部通过后，在同一节点运行 24 小时低频观测：

```bash
python3 scripts/probe_reference_metadata_apis.py \
  --region cn-beijing --network aliyun-ecs-mainland \
  --attest-direct --count 288 --interval-seconds 300 --timeout-seconds 10
```

运行于已有的持久终端会话（如 tmux），不创建默认定时任务。程序前台运行；不要把退出前的
部分结果当作完整报告。未确认网络时可不指定 `--attest-direct` 做诊断，汇总始终标为
`NOT_EXECUTED_ON_MAINLAND`，意思是未取得合格大陆执行证据，并非脚本确认地理位置在海外。

当前 Clash 非 TUN 环境的独立 IPv4 诊断可复现为（只移除子进程变量，系统代理不变）：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    -u http_proxy -u https_proxy -u all_proxy \
    python3 scripts/probe_reference_metadata_apis.py \
    --region cn-mainland --network mainland-campus-isp \
    --ip-family ipv4 --negative-controls --count 1
```

这是不带完整直连确认的诊断命令；只有完整操作者确认到位后才能添加 --attest-direct。
本次一小时观测已在运行，无需并行启动重复观测。

每轮四个串行请求，288 轮共 1152 个正例请求。轮次以开始时间间隔至少 300 秒，最后一轮
结束后也等待该轮剩余间隔，使观察窗口覆盖完整 86400 秒；慢轮次不会触发追赶式突发请求。
多轮时最低允许间隔 60 秒。单轮诊断不等待额外间隔。遇到 429 立即停止本次运行并记录
Retry-After，查阅当时官方限流政策后另建低频运行；没有自动重试。

`--timeout-seconds 10` 是**每阶段**硬超时，而非整个请求的 10 秒总预算；单个请求最坏约
40 秒，total_ms 会如实包含各阶段和本地处理时间。响应体限制 4 MiB。仅连接系统解析结果中
的第一个地址，不做 IPv4/IPv6 回退、不跟随 HTTP 重定向，因此不能据一次首地址失败断言所有
DNS 地址均不可达。可用 `--ip-family ipv4` 或 `--ip-family ipv6` 显式选择地址族；
默认 `auto` 使用系统解析顺序。requested_ip_family 写入样本和汇总，某地址族失败后不会
自动切换另一地址族。不同地址族须独立运行和统计。DNS 列表、选用地址和实际 peer 地址/IP
协议族均写入样本。

所有 TLS 连接使用系统默认信任库和主机名验证。每个样本新建 socket/TLS，延迟因此包含握手，
不是连接池下的延迟。没有浏览器、HTTP 转发、镜像站或账户认证。

## 输出与判读

默认目录 `outputs/reference_metadata_probe/<UTC时间>-<随机标识>/`，也可用 `--output-root`
指定私有输出根目录。每次运行创建新目录，避免覆盖与混入其他网络条件的数据。

- `samples.jsonl`：每个样本完成后立即追加并 flush，含 UTC 时间、region/network、Provider、
  host、用例、请求 DOI、期望/实际语义、返回 DOI、HTTP 状态、异常阶段/类型、
  DNS/TCP/TLS/HTTP/total 毫秒耗时、DNS 地址列表、远端 IP/IPv4/IPv6、TLS 验证及操作者确认。
- `summary.json`：每个样本后原子替换。按 Provider 分开正负例数量，正例业务成功率、成功
  正例 P50/P95（nearest-rank）、超时数、按阶段/原因的失败计数、负例通过数、完整周期数、
  连续整轮失败最长长度、中断标记、证据资格和完整 24 小时条件。

Reachable 表示 TCP 和 TLS 已成功；Functional 表示返回预期 API JSON/目标 DOI，或负例的
明确未命中。正例的 HTTP 404 会降低业务成功率，负例不进入正例稳定性分母。三个 Provider
分别统计：DOI.org 每轮两个正例，共 576 个；Crossref/DataCite 各 288 个。

Ctrl-C 或 SIGTERM 会保留当前失败样本/部分汇总；睡眠期间中断没有虚构请求样本。
SIGKILL/机器掉电不能写最后状态，之前检查点仍保持 interrupted=true，不能误报完成。
启动代理门禁被拦截时仍创建空 samples.jsonl 和原因明确的 summary.json。

退出码：`0` = 本次所有请求按预期完成；`2` = 请求失败、配置错误、限流或环境门禁；
`130` = 用户/终止信号中断。退出码 0 不自动表示合格大陆直连，更不表示已通过跨出口验收。

## 验收及后续 API 选择

项目建议阈值（非供应商 SLA）：

1. 操作者已确认真实大陆服务器、无代理且使用 TLS 验证；预检全部正例和负例通过。
2. 完整 24 小时每 Provider 正例成功率 ≥99%，成功请求 total_ms P95 ≤5000ms。
3. 同一 Provider 没有连续三个完整周期所有正例均失败。若每轮 300 秒，此为三个采样周期；
   并非对两次采样间实际连续故障时长的测量。
4. 另一个独立大陆出口重复预检和观测。

程序最多标记 `meets_single_egress_metrics=true`；不会自动输出 `candidate_usable`。
需要人工关联同节点预检结果、24 小时结果和另一出口结果后再完成终验。少于 288 轮、
缺失样本、观察不足 24 小时、429 提前停止或中断都不通过完整窗口条件。

当前校园网 IPv4 的网络连通及正负例语义已验证，IPv6 首地址 TCP 存在超时；
尚无完整 24 小时稳定性数据、完整路由确认或第二独立出口数据，不能判为 candidate_usable。
本轮支持继续观测 IPv4，不支持依据单轮延迟决定生产接口优先级。
后续可按职责考虑 DOI.org 做 Handle 注册查询、Crossref 做其登记论文题录查询、DataCite
做其资源题录查询；这种职责划分不是本次网络测量结论。三者不可用一个正例相互替代。
DataCite 匿名 API 只返回 Findable DOI，其 404 无法证明 DOI 未注册。

## 官方依据

- [DOI Resolution / REST API 与响应码](https://www.doi.org/the-identifier/resources/factsheets/doi-resolution-documentation)
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
- [Crossref Access & Authentication](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication)
- [DataCite 公共 API 与 Findable 范围](https://support.datacite.org/docs/rest-api)
- [DataCite 单 DOI 官方示例](https://support.datacite.org/docs/api-get-doi)
- [DataCite 限流说明](https://support.datacite.org/docs/rate-limit)

仅提交本实验的三份文件到实验分支，不提交原始结果、不自动创建 PR、不合并 main。
