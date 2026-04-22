---
name: sca-reachability-analysis
description: 当用户要基于 SCA、依赖扫描、SBOM、CVE/GHSA/NVD/GitHub Advisory、组件版本命中结果判断漏洞是否在当前项目中真实可达、是否误报、是否需要升级或是否可用 SAST/DAST 进一步验证时使用。适用于 Java/Maven/Gradle、Node.js/npm、Python/pip 等依赖项目的漏洞可达性分析。
---

# SCA 漏洞可达性分析

## 目标

这个 skill 用于把“组件版本命中漏洞”升级为“漏洞触发路径是否在项目中成立”的白盒分析。核心思想来自 SCA-enhancer：不要只相信版本特征，而是把每个 SCA finding 增强为证据包、SAST 可查线索、DAST 可验证条件，并最终给出可达性结论。

## 触发场景

- 用户提供 SCA 扫描结果、SBOM、依赖树、`pom.xml`、`package-lock.json`、`requirements.txt`、`composer.lock` 等，并询问漏洞是否真实可利用或是否误报。
- 用户给出 CVE/GHSA 和受影响组件，要求判断项目是否调用了漏洞函数、危险配置、受影响入口或完整依赖链。
- 用户希望把 SCA 结果转成 SAST 规则线索、调用链核查清单、DAST 测试前置条件或修复优先级。

## 总体流程

1. **标准化 finding**：抽取组件名、版本、语言、包坐标、依赖路径、直接/间接依赖、漏洞编号、CWE、严重级别、影响范围、公告链接。
2. **漏洞情报增强**：从 SCA 描述、NVD、GHSA、厂商公告、补丁 diff、提交记录、PoC 或官方 issue 中提取真实触发条件。
3. **解析漏洞签名**：把 CVE/GHSA/OSV 解析为具体文件、类、方法、API、配置门、协议、source、sink、guard 和触发条件。
4. **项目内可达性核查**：在当前代码中验证依赖链、代码调用链、配置链、数据流链和运行暴露面是否同时成立。
5. **生成验证线索**：输出 SAST sinks、危险 API、配置键、source-to-sink 模式、DAST 前置条件、payload 类型和探测器。
6. **按证据门槛分级**：使用 L0-L4 结论，缺证据时降级或列为待补证，不允许凭版本命中升级风险。

## 输入优先级

优先使用用户提供的本地材料；缺少漏洞触发细节时再检索权威来源。

- SCA 输出：OpenSCA、Snyk、OWASP Dependency-Check、Trivy、Grype、npm audit、pip-audit、Maven/Gradle dependency tree、CycloneDX/SPDX SBOM。
- 项目证据：依赖清单、锁文件、源码、配置文件、路由/API 定义、启动脚本、Docker/K8s/CI 配置、测试用例。
- 漏洞证据：NVD、GHSA、厂商公告、官方补丁、GitHub commit/PR、release note、PoC、漏洞分析文章。

## 漏洞签名

对每个 finding 先解析漏洞签名，避免只凭 CVE 摘要或包名搜索就下结论。优先使用厂商公告、GHSA/OSV、NVD、补丁 diff、回归测试、官方 issue、PoC 或 changelog；补丁 diff 和测试用例优先级高于二手描述。

使用这个结构组织结果，未知字段留空，不要臆造：

```json
{
  "vulnerability_id": "CVE-YYYY-NNNN",
  "aliases": ["GHSA-...", "OSV-..."],
  "component": {
    "name": "package-or-library",
    "ecosystem": "maven|pip|npm|go|composer|native|os|unknown",
    "affected_ranges": ["< 1.2.3"],
    "fixed_versions": ["1.2.3"]
  },
  "signatures": [
    {
      "type": "file|class|function|method|symbol|api|config|protocol",
      "language": "java|php|python|javascript|go|c|cpp|unknown",
      "path_patterns": ["src/example/File.java"],
      "class_names": ["com.example.Parser"],
      "function_names": ["parse"],
      "symbols": ["vulnerable_symbol"],
      "apis": ["Parser.parse(...)"],
      "config_gates": ["feature.enabled=true"],
      "trigger_condition": "攻击者可控输入以特定格式到达 parser。",
      "sources": ["HTTP body", "uploaded file"],
      "sinks": ["parser", "deserializer"],
      "sanitizers_or_guards": ["strict mode", "schema validation"],
      "exploit_preconditions": ["feature enabled", "authenticated user"],
      "references": ["vendor advisory", "fix commit", "regression test"]
    }
  ]
}
```

最少要回答：

- **受影响面**：漏洞位于库的哪一层，属于 API 调用、解析器、反序列化、模板、表达式、网络客户端、服务端 endpoint、插件、自动配置还是 CLI。
- **危险点**：具体类、函数、方法、配置键、协议处理器、文件类型、header、参数、环境变量或反射入口是什么。
- **攻击者可控点**：哪些输入必须由攻击者控制，例如 HTTP 参数、请求体、上传文件、消息队列、日志内容、配置文件、数据库内容、环境变量、远程服务响应。
- **必要条件**：是否要求特定版本组合、可选依赖、插件启用、反向代理缓存、debug 模式、JNDI/Janino/LDAP/XML 外部实体、反序列化 gadget、特定序列化格式。
- **补丁语义**：补丁实际改了什么，新增校验、禁用危险默认值、限制协议、修复路径拼接、移除类、升级传递依赖还是仅更新文档。

如果无法解析到任何具体文件、类、函数、方法、符号、API、配置或协议条件，不能判为 L3_REACHABLE。

证据强度按这个顺序排序：官方补丁 diff 与公告一致 > 厂商公告明确触发条件 > 回归测试或 PoC 触发样例 > NVD/GHSA/OSV 描述 > 博客或二手摘要 > SCA 工具简述。

## 证据 ID

输出中使用这些证据 ID 约束结论。不要为了凑级别伪造证据；没有证据就列入 `missing_evidence`。

- `EVID_CVE_SCANNER_MATCH`：SCA/SBOM/依赖扫描命中了组件版本和漏洞 ID。
- `EVID_FINDING_NORMALIZED`：finding 已归一化为组件、版本、生态、路径、漏洞 ID。
- `EVID_AFFECTED_VERSION_CONFIRMED`：当前版本确实落在影响范围内。
- `EVID_BACKPORT_OR_PATCH_STATUS`：检查过 backport、厂商补丁或发行版修复状态。
- `EVID_COMPONENT_DEP_MANIFEST_PRESENT`：组件出现在依赖清单或锁文件。
- `EVID_COMPONENT_RUNTIME_PRESENT`：组件进入最终运行产物、镜像、JAR/WAR、site-packages、node_modules production 或二进制链接面。
- `EVID_RUNTIME_LOADED`：运行时证据确认模块、类、库或组件被加载。
- `EVID_VULN_SIGNATURE_RESOLVED`：漏洞已解析为具体文件、类、方法、符号、API、配置门或协议条件。
- `EVID_VULN_CODE_PRESENT`：受影响代码或符号存在于源码或构建产物。
- `EVID_VULN_CODE_ABSENT`：受影响代码或符号不存在。
- `EVID_FIX_COMMIT_ANALYZED`：分析过补丁 diff、修复提交或回归测试。
- `EVID_ENTRYPOINT_IDENTIFIED`：确认了外部入口，例如 HTTP route、RPC、CLI、MQ consumer、文件解析入口或插件 hook。
- `EVID_STATIC_CALLCHAIN`：存在从入口到漏洞签名的静态调用路径。
- `EVID_NO_STATIC_CALLCHAIN`：在声明范围内没有发现入口到漏洞签名的静态路径。
- `EVID_SOURCE_CONTROLLABLE`：攻击者可控输入能影响漏洞参数或状态。
- `EVID_GUARD_ANALYZED`：分析过认证、配置、feature flag、sanitizer、validator、编译门或部署门。
- `EVID_GUARD_BLOCKS_PATH`：当前配置或 guard 阻断漏洞路径。
- `EVID_RUNTIME_CALLED`：运行时 trace 确认漏洞函数、方法、类、符号或路径被调用。
- `EVID_RUNTIME_NOT_CALLED`：运行时 trace 在覆盖流量下未观察到漏洞路径。
- `EVID_RUNTIME_CONFIG`：捕获并分析了漏洞相关运行配置。
- `EVID_POC_AVAILABLE`：存在 PoC、回归测试或触发输入。
- `EVID_POC_EXECUTED`：在授权测试环境执行了 PoC 或触发输入。
- `EVID_POC_EFFECT`：观察到安全效果、崩溃、回连、文件效果、响应差异或可验证漏洞条件。
- `EVID_POC_INCONCLUSIVE`：PoC 执行失败或结果不确定。
- `EVID_SCOPE_DECLARED`：声明了分析范围，包括源码、构建产物、配置、运行面和限制。
- `EVID_MISSING_EVIDENCE_LISTED`：列明缺失证据和后续探针。

## 项目内核查

## 快速退出机制

在容器镜像或 OS 包扫描结果中，先做低价值项 quick-exit 判断，避免对明显不在目标攻击面的基础包执行完整长任务分析。

可以快速退出的典型条件：

- finding 来自 OS/distro 包，组件是 `apt`、`dpkg`、`coreutils`、`tar`、`gzip`、`sed`、`grep`、`findutils`、`login`、`passwd`、`perl-base`、`tzdata` 等基础 OS/CLI 包。
- 当前分析目标是 Web 应用攻击面，例如 Joomla/PHP/Apache，而该组件没有 daemon 暴露、没有 Web 请求路径调用证据、没有被应用作为解析器/处理器调用。
- 漏洞触发条件需要本地用户、CLI 执行、包管理操作、构建/安装阶段、手工处理 crafted archive/file，且没有远程 Web 可控路径。
- 严重级别低于 high，且无 RCE、SSRF、XXE、反序列化、命令注入、认证绕过、公开 PoC 或已知利用信号。

quick-exit 输出规则：

- 只做最小证据核查：组件存在性、包类型、攻击面不匹配原因、是否存在高影响例外信号。
- 默认结论用 `L1_PRESENT_UNREACHABLE` 或 `L2_POTENTIALLY_REACHABLE`。如果只是未查清调用关系，用 L2；只有明确当前运行面无入口/无调用/guard 阻断时才用 L1。
- 必须列出 `quick_exit_reason`，例如“基础 OS CLI 包，目标为 Joomla Web 请求面，未发现 Web 路径调用该工具”。
- 不进行深度 Web 文章阅读、PoC 复现或长任务 milestone，除非该 finding 触发下面的例外条件。

不能 quick-exit 的例外：

- 组件直接暴露在网络或 Web 请求路径中：`apache2`、`nginx`、`php`、`libapache2-mod-php`、Joomla/WordPress/Drupal。
- Web 常用底层库：`openssl/libssl`、`libxml2`、`curl/libcurl`、`imagemagick`、`ghostscript`、数据库客户端库、PHP 扩展。
- 漏洞描述或权威资料显示 remote/network/HTTP/TLS/RCE/SSRF/XXE/deserialization/command injection/auth bypass。
- 目标应用存在上传、解压、图片/PDF 处理、备份恢复、插件安装、系统命令调用等功能，可能间接调用该系统包。
- 用户显式要求全量分析或 `triage.quick_exit=false`。

quick-exit 是节省资源的分流机制，不是删除风险。最终汇总必须统计 quick-exit 数量，并允许后续抽查或按需复核。

### 1. 依赖链

- 确认漏洞组件是否实际进入构建产物，而不只是出现在父 POM、dev dependency、test scope、optional dependency、peer dependency 或未打包模块中。
- 对间接依赖记录完整路径，例如 `app -> starter -> vulnerable-lib`，判断调用方是否也在运行时启用。
- 检查版本冲突、dependencyManagement、shade/relocation、exclusion、lockfile resolution、容器镜像内实际版本。
- 若漏洞需要“完整组件链”，必须验证触发所需的上游封装、插件或可选库是否同时存在。

### 2. 调用链

- 从漏洞触发点反向搜索项目代码中是否调用危险 API、受影响类、包装方法、自动配置类或框架入口。
- 不只查字符串；结合 import、类型、接口实现、注解、路由、Bean 注入、反射、SPI、插件发现、序列化注册表。
- 对框架自动调用的库，确认应用是否启用了对应 feature，例如 Spring Boot auto-configuration、Jackson polymorphic typing、Logback receiver、Thymeleaf 表达式、SnakeYAML `load`。
- 区分“库存在”与“漏洞代码路径执行”：只有触发点可从项目入口到达，才进入可达或条件可达。

### 3. 数据流

- 标出 source：HTTP 请求、RPC、WebSocket、消息队列、文件上传、配置中心、数据库字段、第三方 API 响应、环境变量、日志输入。
- 标出 sink：漏洞公告中的危险函数、解析器、模板渲染、反序列化、命令执行、路径访问、SSRF 请求、表达式求值。
- 追踪 source 到 sink 是否有完整链路，记录关键中间变量、对象字段、转换函数、过滤或编码。
- 若存在强校验、固定值、allowlist、不可控配置、只读内部数据，应降低可达性。

### 4. 配置链

- 搜索漏洞相关配置键、默认值和运行环境变量，例如 `enable*`、`trusted*`、`allowed*`、`deserialization`、`polymorphic`、`jndi`、`janino`、`xxe`、`proxy-cache`。
- 检查配置来源优先级：默认配置、应用配置、环境变量、启动参数、容器配置、CI/CD 注入、运行时配置中心。
- 对“危险默认启用”的漏洞，说明即使项目没有显式配置也可能可达；对“危险功能需显式启用”的漏洞，找不到启用证据时通常判为不可达或证据不足。

### 5. 运行暴露面

- 判断入口是否在生产运行路径中：控制器、API、管理端点、后台任务、consumer、CLI、定时任务、反向代理、开放端口。
- 对 DoS、缓存投毒、SSRF、XXE、RCE 等漏洞，确认外部攻击者是否能到达相关入口，或是否需要本地/管理员/供应链前置权限。
- DAST 线索必须是低风险、授权环境可执行的验证思路；默认不要生成破坏性 payload。

## SAST 线索输出

从漏洞证据中提取面向静态分析的最小可查集合：

```json
{
  "finding_id": "组件@版本:CVE",
  "package": "组件名",
  "version": "版本",
  "language": "语言",
  "vuln_type": "RCE/XSS/SQLI/SSRF/XXE/DESER/PATH_TRAVERSAL/DOS/OTHER",
  "dangerous_apis": ["类或函数"],
  "call_patterns": [{"api": "函数名", "arg_positions": [0], "note": "危险参数含义"}],
  "sources": ["攻击者可控输入"],
  "sinks": ["漏洞触发点"],
  "config_keys": [{"key": "配置键", "dangerous_values": ["危险值"], "safe_values": ["安全值"]}],
  "evidence_refs": ["公告或补丁链接"],
  "confidence": "high/medium/low"
}
```

优先提取补丁中出现的函数、测试用例调用、公告明确点名的 API。没有证据时不要臆造精确 API，只输出待查假设并标低置信度。

## DAST 线索输出

仅在项目存在可访问入口时输出 DAST 线索：

```json
{
  "finding_id": "组件@版本:CVE",
  "preconditions": ["必须启用的功能或配置"],
  "attack_vectors": [{"protocol": "HTTP/RPC/MQ/FILE", "interface_type": "web/api/worker", "injection_points": ["参数、header、文件或消息字段"]}],
  "payload_strategy": ["使用无害探测、错误回显、状态码、延迟、OOB 回连或日志关键字"],
  "detectors": [{"type": "status_code/keyword/exception/oob/timing/log", "value": "观测点", "hint": "判定说明"}],
  "risk_notes": "仅在授权测试环境验证；避免破坏性、持久化或横向移动行为"
}
```

如果入口不可达、需要管理员上传配置、需要篡改 classpath、需要本地文件写入等强前置条件，应把 DAST 标为“不建议直接测”或“仅条件可测”。

## 判定标准

使用一个最终分级，不要同时给多个等级。可以在中文摘要中映射为“不受影响、存在但不可达、潜在可达、可达、已确认可利用”。

- **L0_NOT_AFFECTED**：扫描候选不适用于当前目标。典型证据是组件缺失、版本不受影响、backport/补丁已存在、漏洞代码或符号不存在。至少需要 `EVID_CVE_SCANNER_MATCH` 加一个否定证据，例如 `EVID_VULN_CODE_ABSENT` 或 `EVID_BACKPORT_OR_PATCH_STATUS`。
- **L1_PRESENT_UNREACHABLE**：受影响组件或漏洞代码存在，但当前运行面无法到达。至少需要 `EVID_COMPONENT_RUNTIME_PRESENT` 或 `EVID_VULN_CODE_PRESENT`，并需要 `EVID_NO_STATIC_CALLCHAIN` 或 `EVID_GUARD_BLOCKS_PATH`，同时必须有 `EVID_SCOPE_DECLARED`。
- **L2_POTENTIALLY_REACHABLE**：存在部分可达证据，但关键证明缺失。典型情况是组件存在、漏洞签名已解析、有部分 import/call/config 关系，但缺入口、数据可控性、guard、运行时或 PoC 证据。至少需要 `EVID_VULN_SIGNATURE_RESOLVED` 和 `EVID_MISSING_EVIDENCE_LISTED`。
- **L3_REACHABLE**：漏洞签名通过静态或动态证据可达，但未确认漏洞效果。需要 `EVID_STATIC_CALLCHAIN` 加 `EVID_SOURCE_CONTROLLABLE`，或 `EVID_RUNTIME_CALLED`。建议同时包含 `EVID_GUARD_ANALYZED`。
- **L4_EXPLOITABLE_CONFIRMED**：在授权环境观察到漏洞效果。必须有 `EVID_POC_EXECUTED` 和 `EVID_POC_EFFECT`，建议同时有 `EVID_STATIC_CALLCHAIN` 或 `EVID_RUNTIME_CALLED`。

升级门槛：

- 没有 `EVID_COMPONENT_RUNTIME_PRESENT` 或等价运行面证据，不得判 L1 以上。
- 没有 `EVID_VULN_SIGNATURE_RESOLVED`，不得判 L3。
- 没有 `EVID_STATIC_CALLCHAIN`、`EVID_SOURCE_CONTROLLABLE` 或 `EVID_RUNTIME_CALLED`，不得判 L3。
- 没有 `EVID_POC_EXECUTED` 和 `EVID_POC_EFFECT`，不得判 L4。
- 不可达结论必须限定在已分析的源码、构建产物、配置和运行面，不能写成无条件“永不可达”。

## 置信度

- **高**：公告/补丁/PoC 与本地调用链、配置链、数据流均能互相印证。
- **中**：漏洞触发条件清楚，本地存在部分证据，但运行配置、入口暴露或数据可控性仍有缺口。
- **低**：主要依赖 SCA 描述、NVD 摘要或模糊文本，缺少补丁/调用链/配置证据。

## 输出格式

默认用中文输出，按 finding 分组。每个 finding 至少包含：

- `结论`：L0_NOT_AFFECTED、L1_PRESENT_UNREACHABLE、L2_POTENTIALLY_REACHABLE、L3_REACHABLE 或 L4_EXPLOITABLE_CONFIRMED，并附中文解释。
- `置信度`：高、中、低。
- `误报/风险原因`：一句话说明核心原因。
- `漏洞签名`：受影响版本、修复版本、危险文件/类/函数/API/配置/协议、source、sink、guard、触发条件。
- `证据链`：依赖链、运行面存在性、代码调用、配置、数据流、运行暴露面。
- `证据 ID`：列出已满足的 `EVID_*`。
- `缺失证据`：列出阻止升级结论的证据缺口和下一步探针。
- `SAST 线索`：危险 API、source、sink、配置键、建议搜索模式。
- `DAST 线索`：前置条件、入口、无害探测方式、观测点；不可测时说明原因。
- `修复建议`：升级、排除依赖、禁用功能、配置缓解、增加测试或补充证据。

## 分析纪律

- 不要把“组件版本受影响”直接等同于“漏洞可达”。
- 不要因为找不到字符串就立即判不可达；先考虑框架自动配置、反射、SPI、注解扫描和传递调用。
- 不要把 PoC payload 原样当作 DAST 建议；优先给无害验证策略和观测点。
- 如果需要最新漏洞公告、GHSA、NVD 或补丁 diff，必须查权威来源并标明引用来源。
- 对每个结论保留可复查证据：文件路径、函数名、配置键、依赖路径、公告/补丁链接。
- 当证据相互矛盾时，优先相信本地构建产物和官方补丁语义，并把矛盾点列为开放问题。
- 先降级再解释，不要先给高等级结论再用“可能、应该、推测”补洞。
