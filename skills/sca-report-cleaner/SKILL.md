---
name: sca-report-cleaner
description: 当用户提供大型 SCA、SBOM、依赖扫描、OpenSCA、Snyk、Trivy、Grype、npm audit、OWASP Dependency-Check 或类似 JSON/CSV 报告，需要清洗、归一化、去重、拆分为逐 CVE 可达性分析任务，或需要生成供 CodeSlave 并发 flow 消费的 cleaned findings JSON 时使用。
---

# SCA 报告清洗与 CodeSlave 投递

## 目标

把巨大的扫描报告变成稳定、可追踪、可批处理的 finding 清单，并投递给 CodeSlave 的 `sca_reachability` flow 做逐 CVE 并发可达性分析，避免把 100-1000 条漏洞一次性塞进模型提示词导致漏项和幻觉。

## 何时使用

- 用户给出大型 SCA JSON/SBOM/依赖树，要求后续做漏洞可达性分析。
- 用户担心模型只分析部分 finding 却伪装成全量完成。
- 用户要把扫描结果投喂给 CodeSlave，让每个 worker 只处理一个 CVE/finding。

## 工作流

1. **只做清洗，不做可达性结论**：清洗阶段禁止给出“可达/不可达”判断。
2. **建立 finding index**：抽取 `finding_id`、组件、版本、生态、依赖路径、漏洞 ID、CWE、严重级别、原始位置。
3. **去重与保留映射**：按 `(component, version, vulnerability_id, dependency_path)` 去重；同一 CVE 多路径要保留所有路径。
4. **优先级排序**：按严重级别、漏洞类型、直接依赖、运行时线索、PoC/已知利用、外部入口相关性排序。
5. **筛去明显低价值目标**：对明显不在目标攻击面的基础 OS/CLI 包标记 `triage.quick_exit=true` 和 `status=quick_exit_candidate`，默认不投递长任务 worker；这些 finding 保留在 cleaned JSON 和统计中，但不进入默认并发分析队列。
6. **生成 coverage manifest**：输出总数、已归一化数、跳过数、去重数、blocked 数，确保后续批处理可核对。
7. **输出 cleaned JSON**：供 `sca-reachability-analysis` 或 `CodeSlave` flow 逐条消费。
8. **投递 CodeSlave**：除非用户明确只要清洗结果，否则清洗后继续调用 CodeSlave `sca-run` 或提示用户使用该快捷命令批量入队。

## 推荐脚本

优先运行内置脚本：

```bash
python3 skills/sca-report-cleaner/scripts/normalize_sca_report.py input.json -o cleaned-findings.json
```

脚本会尽力解析常见 JSON 结构，包括 OpenSCA dependency tree、`vulnerabilities`、`findings`、`results`、`dependencies[].vulnerabilities`。不认识的结构会保留 raw path 和 blocked reason。

## CodeSlave 快捷投递

推荐直接使用 CodeSlave 快捷方式完成“清洗 + 入队”：

```bash
codeslave sca-run raw-sca.json --target /path/to/project --max-workers 8
```

这个命令会：

- 调用本 skill 的 `normalize_sca_report.py` 生成 cleaned JSON。
- 自动切换到 `sca_reachability` flow。
- 把每个 pending finding 展开成一个独立 worker job。
- 把目标项目路径通过 `cleaned.json::target_path` 传给每个 worker。

如果希望立即开始并等待所有 worker 结束：

```bash
codeslave sca-run raw-sca.json --target /path/to/project --max-workers 8 --start
```

如果已经生成 cleaned JSON，也可以手动入队：

```bash
codeslave enqueue --flow sca_reachability 'cleaned-findings.json::/path/to/project'
```

注意：大批量任务建议先不加 `--start`，进入 TUI 检查 job 数量、并发数和目标路径后再启动。

默认情况下，`sca-run` 会筛去 `triage.quick_exit=true` 的明显低价值目标，不为它们创建 worker。若要强制全量入队：

```bash
codeslave sca-run raw-sca.json --target /path/to/project --include-quick-exit
```

## 筛去明显低价值目标

容器镜像扫描，尤其是 Debian 8、CentOS 6/7、Ubuntu EOL 基础镜像，常会产生大量系统包 CVE。清洗器必须区分“镜像中存在”与“Web 攻击面可达”。

默认筛去并标记为 quick-exit candidate 的典型项：

- `apt`、`dpkg`、`coreutils`、`tar`、`gzip`、`sed`、`grep`、`findutils`、`login`、`passwd`、`perl-base`、`tzdata` 等基础 OS/CLI 包。
- 漏洞描述主要是 local attacker、CLI、安装/包管理、构建阶段、crafted archive/file，但当前目标是 Joomla/PHP/Apache Web 请求面。
- 严重级别不是 critical/high，且没有 RCE、SSRF、XXE、反序列化、命令注入、认证绕过、已知 PoC 等高影响信号。

不能默认筛去的系统包：

- Web 直接暴露组件：`apache2`、`nginx`、`php`、`libapache2-mod-php`、Joomla/WordPress/Drupal。
- Web 请求路径常见运行库：`openssl/libssl`、`libxml2`、`curl/libcurl`、`imagemagick`、`ghostscript`、数据库客户端库、PHP 扩展。
- 描述中存在远程、网络、HTTP、TLS、RCE、SSRF、XXE、反序列化、命令注入、认证绕过或公开利用迹象。

筛去不是删除 finding。它表示该项默认不消耗长任务 worker，并应在汇总报告中作为“低优先级待抽查/按需复核”保留。

清洗器输出和汇总时必须明确：

- `quick_exit_candidate` 数量。
- 默认投递 CodeSlave 的 `pending` 数量。
- 被筛去项的主要原因，例如“基础 OS 包”“本地/CLI 前置条件”“不在 Joomla/PHP Web 请求路径”。
- 如何复核：使用 `--include-quick-exit` 或只抽样分析 quick-exit 项。

## Cleaned JSON Schema

输出应符合这个结构：

```json
{
  "schema_version": "sca-clean-v1",
  "source_file": "raw-report.json",
  "summary": {
    "total_findings": 200,
    "normalized_findings": 200,
    "deduplicated_findings": 0,
    "skipped_records": 0,
    "blocked_records": 0
  },
  "findings": [
    {
      "finding_id": "sha1-stable-id",
      "status": "pending",
      "priority": 90,
      "triage": {
        "lane": "hot_path|normal|quick_exit",
        "package_type": "application_or_library|os_package",
        "quick_exit": false,
        "reasons": ["component may be in web/runtime attack surface"],
        "priority_adjustment": 20
      },
      "component": {
        "name": "spring-boot-autoconfigure",
        "version": "2.3.8.RELEASE",
        "vendor": "org.springframework.boot",
        "ecosystem": "maven",
        "language": "Java",
        "purl": "pkg:maven/org.springframework.boot/spring-boot-autoconfigure@2.3.8.RELEASE",
        "direct": false,
        "dependency_paths": ["app -> starter -> component"]
      },
      "vulnerability": {
        "id": "CVE-2023-20883",
        "aliases": ["scanner-specific-id"],
        "cve_id": "CVE-2023-20883",
        "cwe_id": "CWE-400",
        "title": "Spring Boot DoS",
        "description": "...",
        "severity": "high",
        "references": []
      },
      "scanner": {
        "tool": "auto",
        "raw_path": "$.children[0].children[1].vulnerabilities[0]"
      },
      "evidence": [
        {
          "id": "EVID_CVE_SCANNER_MATCH",
          "detail": "scanner matched component/version to vulnerability"
        }
      ],
      "raw": {}
    }
  ]
}
```

## 批处理纪律

- 禁止在未生成 finding 清单前直接分析大 JSON。
- 禁止对未出现在 cleaned JSON 的 finding 给可达性结论。
- 后续分析必须维护状态：`pending`、`quick_exit_candidate`、`in_progress`、`analyzed`、`skipped`、`blocked`、`deduplicated`。
- 最终报告必须核对：`analyzed + skipped + blocked + deduplicated + pending == total_findings`。
- 如果只分析了 50/200，必须明确剩余 150 条是 pending，不能写成全量完成。

## 与可达性分析配合

清洗完成后，把 `findings[*]` 逐条交给 `sca-reachability-analysis`。每个 worker 的提示词只包含一条 finding、项目路径、分析范围和输出位置。

大型任务推荐链路：

```text
raw SCA JSON
  -> sca-report-cleaner
  -> cleaned-findings.json
  -> CodeSlave sca-run / sca_reachability flow
  -> 每个 finding 一个长任务 worker
  -> 汇总 coverage 和结论
```

## Worker 行为预期

`sca_reachability` flow 的每个 worker 会被要求：

- 只分析自己的 `finding.json`。
- 使用 CodeSlave 内置 `sca-reachability-analysis` skill 的 L0-L4 分级和证据门槛。
- 主动 Web 搜索厂商公告、GHSA/OSV/NVD、补丁 diff、回归测试、可信文章和 PoC 说明，先理解漏洞触发条件。
- 参考长任务模式，维护 `Prompt.md`、`Plan.md`、`Implement.md`、`Documentation.md`，避免长时间分析漂移。
- 输出 `result.md` 和 `result.json`，便于后续汇总 coverage。
