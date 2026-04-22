#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SEVERITY_SCORE = {
    "critical": 100,
    "high": 80,
    "medium": 50,
    "low": 25,
    "info": 5,
    "unknown": 10,
}

WEB_EXPOSED_NAMES = {
    "apache2",
    "apache2-bin",
    "httpd",
    "nginx",
    "php",
    "php5",
    "php7",
    "php8",
    "libapache2-mod-php5",
    "libapache2-mod-php7.0",
    "libapache2-mod-php",
    "joomla",
    "wordpress",
    "drupal",
    "mysql-server",
    "mariadb-server",
    "openssl",
    "libssl1.0.0",
    "libssl1.1",
    "libssl3",
    "curl",
    "libcurl3",
    "libcurl4",
    "libxml2",
    "imagemagick",
    "ghostscript",
    "php5-mysql",
    "php5-gd",
    "php5-curl",
    "php5-xml",
}

LOW_VALUE_SYSTEM_NAMES = {
    "apt",
    "apt-utils",
    "base-files",
    "base-passwd",
    "bash",
    "bsdutils",
    "coreutils",
    "dash",
    "debconf",
    "debian-archive-keyring",
    "debianutils",
    "diffutils",
    "dpkg",
    "e2fslibs",
    "e2fsprogs",
    "findutils",
    "gcc-4.9-base",
    "gcc-5-base",
    "gnupg",
    "gpgv",
    "grep",
    "gzip",
    "hostname",
    "init",
    "initscripts",
    "insserv",
    "libapt-pkg4.12",
    "libc-bin",
    "libc6",
    "libgcc1",
    "libsemanage-common",
    "libsemanage1",
    "libsepol1",
    "login",
    "lsb-base",
    "mawk",
    "mount",
    "multiarch-support",
    "ncurses-base",
    "ncurses-bin",
    "passwd",
    "perl-base",
    "sed",
    "sensible-utils",
    "sysv-rc",
    "sysvinit",
    "sysvinit-utils",
    "tar",
    "tzdata",
    "util-linux",
    "zlib1g",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize large SCA JSON reports into per-finding records.")
    parser.add_argument("input", help="Input SCA JSON report")
    parser.add_argument("-o", "--output", required=True, help="Output cleaned findings JSON")
    parser.add_argument("--tool", default="auto", help="Scanner name hint")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()
    data = json.loads(input_path.read_text(encoding="utf-8", errors="replace"))

    findings, skipped = normalize_report(data, source_file=str(input_path), scanner_tool=args.tool)
    findings = dedupe_findings(findings)
    findings.sort(key=lambda item: (-int(item.get("priority", 0)), item["finding_id"]))

    payload = {
        "schema_version": "sca-clean-v1",
        "source_file": str(input_path),
        "summary": {
            "total_findings": len(findings),
            "normalized_findings": len(findings),
            "deduplicated_findings": sum(1 for item in findings if item.get("deduplicated_from")),
            "skipped_records": skipped,
            "blocked_records": 0,
        },
        "findings": findings,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(findings)} findings to {output_path}")
    return 0


def normalize_report(data: Any, *, source_file: str, scanner_tool: str) -> tuple[list[dict[str, Any]], int]:
    if isinstance(data, dict) and isinstance(data.get("matches"), list):
        return normalize_grype_report(data, source_file=source_file, scanner_tool=scanner_tool)

    findings: list[dict[str, Any]] = []
    skipped = 0

    def walk(node: Any, path: str, component_stack: list[dict[str, Any]]) -> None:
        nonlocal skipped
        if isinstance(node, dict):
            component = extract_component(node)
            next_stack = component_stack
            if component["name"] or component["version"]:
                next_stack = component_stack + [component]

            vulns = extract_vulnerabilities(node)
            if vulns:
                current_component = component if component["name"] or component["version"] else (component_stack[-1] if component_stack else empty_component())
                for idx, vuln in enumerate(vulns):
                    if not has_vuln_identity(vuln):
                        skipped += 1
                        continue
                    findings.append(make_finding(current_component, vuln, path=f"{path}.vulnerabilities[{idx}]", source_file=source_file, scanner_tool=scanner_tool))

            for key, value in node.items():
                if key == "vulnerabilities":
                    continue
                child_path = f"{path}.{key}" if path else f"$.{key}"
                walk(value, child_path, next_stack)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                walk(item, f"{path}[{idx}]", component_stack)

    walk(data, "$", [])
    return findings, skipped


def normalize_grype_report(data: dict[str, Any], *, source_file: str, scanner_tool: str) -> tuple[list[dict[str, Any]], int]:
    findings: list[dict[str, Any]] = []
    skipped = 0
    for idx, match in enumerate(data.get("matches", [])):
        if not isinstance(match, dict):
            skipped += 1
            continue
        artifact = match.get("artifact")
        vulnerability = match.get("vulnerability")
        if not isinstance(artifact, dict) or not isinstance(vulnerability, dict):
            skipped += 1
            continue
        component = extract_grype_component(artifact)
        vuln = normalize_grype_vulnerability(vulnerability)
        if not has_vuln_identity(vuln):
            skipped += 1
            continue
        finding = make_finding(
            component,
            vuln,
            path=f"$.matches[{idx}]",
            source_file=source_file,
            scanner_tool="grype" if scanner_tool == "auto" else scanner_tool,
        )
        finding["scanner"]["match_details"] = match.get("matchDetails", [])
        finding["scanner"]["distro"] = data.get("distro", {})
        finding["raw"] = {
            "vulnerability": vulnerability,
            "artifact": artifact,
            "matchDetails": match.get("matchDetails", []),
            "relatedVulnerabilities": match.get("relatedVulnerabilities", []),
        }
        findings.append(finding)
    return findings, skipped


def extract_grype_component(artifact: dict[str, Any]) -> dict[str, Any]:
    name = first_str(artifact, "name")
    version = first_str(artifact, "version")
    language = first_str(artifact, "language", "type")
    package_type = first_str(artifact, "type")
    purl = first_str(artifact, "purl")
    ecosystem = infer_ecosystem(language or package_type, "", {"purl": purl, "type": package_type})
    locations = artifact.get("locations", [])
    paths: list[str] = []
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict):
                path = location.get("path")
                if isinstance(path, str):
                    paths.append(path)
    upstreams = artifact.get("upstreams", [])
    vendor = ""
    if isinstance(upstreams, list) and upstreams:
        first = upstreams[0]
        if isinstance(first, dict):
            vendor = first_str(first, "name")
    return {
        "name": name,
        "version": version,
        "vendor": vendor,
        "ecosystem": ecosystem,
        "language": language or package_type or ecosystem,
        "purl": purl or build_purl(ecosystem, vendor, name, version),
        "direct": False,
        "dependency_paths": paths,
    }


def normalize_grype_vulnerability(vulnerability: dict[str, Any]) -> dict[str, Any]:
    vuln_id = first_str(vulnerability, "id")
    cwes = vulnerability.get("cwes", [])
    cwe_id = ""
    if isinstance(cwes, list) and cwes:
        cwe_id = str(cwes[0])
    fix = vulnerability.get("fix", {})
    references = vulnerability.get("urls", [])
    if not references:
        references = [first_str(vulnerability, "dataSource")]
    normalized = {
        "id": vuln_id,
        "cve_id": vuln_id if vuln_id.startswith("CVE-") else first_cve({"description": json.dumps(vulnerability, ensure_ascii=False)}),
        "cwe_id": cwe_id,
        "title": vuln_id,
        "name": vuln_id,
        "description": first_str(vulnerability, "description"),
        "severity": first_str(vulnerability, "severity"),
        "references": references,
        "namespace": first_str(vulnerability, "namespace"),
        "knownExploited": vulnerability.get("knownExploited", []),
        "epss": vulnerability.get("epss", []),
        "fix": fix,
    }
    return normalized


def extract_component(item: dict[str, Any]) -> dict[str, Any]:
    name = first_str(item, "name", "package", "packageName", "component", "moduleName", "artifactId")
    vendor = first_str(item, "vendor", "group", "groupId", "namespace")
    version = first_str(item, "version", "installedVersion", "currentVersion")
    language = first_str(item, "language", "ecosystem", "type")
    ecosystem = infer_ecosystem(language, vendor, item)
    paths = item.get("paths") or item.get("dependency_path") or item.get("from") or []
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        paths = []
    direct = bool(item.get("direct", len(paths) <= 1 if paths else False))
    purl = first_str(item, "purl", "package_url")
    if not purl and name and version:
        purl = build_purl(ecosystem, vendor, name, version)
    return {
        "name": name,
        "version": version,
        "vendor": vendor,
        "ecosystem": ecosystem,
        "language": language,
        "purl": purl,
        "direct": direct,
        "dependency_paths": [str(path) for path in paths],
    }


def empty_component() -> dict[str, Any]:
    return {
        "name": "",
        "version": "",
        "vendor": "",
        "ecosystem": "unknown",
        "language": "unknown",
        "purl": "",
        "direct": False,
        "dependency_paths": [],
    }


def extract_vulnerabilities(item: dict[str, Any]) -> list[dict[str, Any]]:
    vulns = item.get("vulnerabilities")
    if isinstance(vulns, list):
        return [vuln for vuln in vulns if isinstance(vuln, dict)]
    if isinstance(vulns, dict):
        return [vulns]
    vuln = item.get("vulnerability")
    if isinstance(vuln, dict):
        return [vuln]
    if has_vuln_identity(item):
        return [item]
    return []


def has_vuln_identity(item: dict[str, Any]) -> bool:
    candidates = [
        item.get("cve_id"),
        item.get("cve"),
        item.get("id"),
        item.get("name"),
        item.get("ghsa_id"),
        item.get("osv_id"),
    ]
    return any(isinstance(value, str) and value.strip() for value in candidates)


def make_finding(component: dict[str, Any], vuln: dict[str, Any], *, path: str, source_file: str, scanner_tool: str) -> dict[str, Any]:
    vuln_id = vuln_identifier(vuln)
    severity = normalize_severity(vuln.get("severity", vuln.get("security_level_id", "unknown")))
    aliases = aliases_for(vuln, vuln_id)
    finding_id = stable_id(component, vuln_id, path)
    triage = triage_finding(component, vuln, severity)
    priority = priority_score(severity, component, vuln, triage)
    return {
        "finding_id": finding_id,
        "status": "quick_exit_candidate" if triage["quick_exit"] else "pending",
        "priority": priority,
        "triage": triage,
        "component": component,
        "vulnerability": {
            "id": vuln_id,
            "aliases": aliases,
            "cve_id": first_cve(vuln) or (vuln_id if vuln_id.startswith("CVE-") else ""),
            "cwe_id": first_str(vuln, "cwe_id", "cwe", "cweId"),
            "title": first_str(vuln, "title", "name", "summary") or vuln_id,
            "description": first_str(vuln, "description", "details", "overview"),
            "severity": severity,
            "references": normalize_references(vuln.get("references", [])),
        },
        "scanner": {
            "tool": scanner_tool,
            "source_file": source_file,
            "raw_path": path,
        },
        "evidence": [
            {
                "id": "EVID_CVE_SCANNER_MATCH",
                "detail": "scanner matched component/version to vulnerability",
            },
            {
                "id": "EVID_FINDING_NORMALIZED",
                "detail": "finding normalized by sca-report-cleaner",
            },
        ],
        "raw": vuln,
    }


def dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for finding in findings:
        component = finding["component"]
        vuln = finding["vulnerability"]
        key = (component.get("name", ""), component.get("version", ""), vuln.get("id", ""))
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = finding
            continue
        existing_paths = existing["component"].setdefault("dependency_paths", [])
        for dep_path in component.get("dependency_paths", []):
            if dep_path not in existing_paths:
                existing_paths.append(dep_path)
        existing.setdefault("deduplicated_from", []).append(finding["finding_id"])
        existing["priority"] = max(int(existing.get("priority", 0)), int(finding.get("priority", 0)))
    return list(grouped.values())


def stable_id(component: dict[str, Any], vuln_id: str, path: str) -> str:
    raw = "|".join([
        component.get("ecosystem", ""),
        component.get("vendor", ""),
        component.get("name", ""),
        component.get("version", ""),
        vuln_id,
        path,
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def vuln_identifier(vuln: dict[str, Any]) -> str:
    cve = first_cve(vuln)
    if cve:
        return cve
    return first_str(vuln, "ghsa_id", "osv_id", "id", "cve_id", "cve", "name") or "UNKNOWN"


def first_cve(vuln: dict[str, Any]) -> str:
    for key in ("cve_id", "cve", "id", "name", "description"):
        value = vuln.get(key)
        if isinstance(value, str):
            match = re.search(r"CVE-\d{4}-\d{4,}", value)
            if match:
                return match.group(0)
    identifiers = vuln.get("identifiers")
    if isinstance(identifiers, dict):
        cves = identifiers.get("CVE")
        if isinstance(cves, list) and cves:
            return str(cves[0])
    return ""


def aliases_for(vuln: dict[str, Any], primary: str) -> list[str]:
    aliases: list[str] = []
    for key in ("id", "cve_id", "cve", "ghsa_id", "osv_id", "cnnvd_id", "cnvd_id", "name"):
        value = vuln.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != primary:
            aliases.append(value.strip())
    return sorted(set(aliases))


def normalize_severity(raw: Any) -> str:
    if isinstance(raw, int):
        return {1: "critical", 2: "high", 3: "medium", 4: "low", 5: "info"}.get(raw, "unknown")
    value = str(raw or "unknown").strip().lower()
    if value in SEVERITY_SCORE:
        return value
    return "unknown"


def priority_score(severity: str, component: dict[str, Any], vuln: dict[str, Any], triage: dict[str, Any] | None = None) -> int:
    score = SEVERITY_SCORE.get(severity, 10)
    if component.get("direct"):
        score += 10
    text = " ".join(str(vuln.get(key, "")) for key in ("title", "name", "description", "cwe_id", "cwe"))
    lowered = text.lower()
    if any(word in lowered for word in ("rce", "remote code", "deserial", "ssrf", "xxe", "command injection")):
        score += 15
    if "poc" in lowered or "exploit" in lowered:
        score += 10
    if triage:
        score += int(triage.get("priority_adjustment", 0))
    return min(score, 100)


def triage_finding(component: dict[str, Any], vuln: dict[str, Any], severity: str) -> dict[str, Any]:
    name = str(component.get("name", "")).lower()
    ecosystem = str(component.get("ecosystem", "unknown")).lower()
    language = str(component.get("language", "")).lower()
    purl = str(component.get("purl", "")).lower()
    text = " ".join(str(vuln.get(key, "")) for key in ("title", "name", "description", "cwe_id", "cwe")).lower()
    raw_text = json.dumps(vuln, ensure_ascii=False).lower()
    package_type = "application_or_library"
    reasons: list[str] = []
    quick_exit = False
    priority_adjustment = 0

    is_os_package = (
        ecosystem in {"deb", "rpm", "apk", "os", "linux", "debian"}
        or "pkg:deb/" in purl
        or language in {"deb", "rpm", "apk"}
        or any(token in raw_text for token in ('"type": "deb"', '"type":"deb"', '"distro"', '"namespace": "debian'))
    )
    web_exposed = name in WEB_EXPOSED_NAMES or any(marker in name for marker in ("apache", "nginx", "php", "openssl", "libssl", "libxml", "curl", "mysql", "mariadb", "imagemagick", "ghostscript"))
    high_impact = any(word in text for word in ("remote code", "rce", "ssrf", "xxe", "deserial", "command injection", "authentication bypass", "http", "network", "remote attacker", "remotely exploitable"))
    local_or_cli = any(word in text for word in ("local", "locally", "local user", "local attacker", "command line", "cli", "crafted archive", "crafted file")) and not any(word in text for word in ("remote", "http", "web", "network"))

    if is_os_package:
        package_type = "os_package"
        reasons.append("OS/distro package from container scan")
        priority_adjustment -= 15

    if web_exposed:
        reasons.append("component may be in web/runtime attack surface")
        priority_adjustment += 20

    if name in LOW_VALUE_SYSTEM_NAMES and not web_exposed:
        reasons.append("common base-system/CLI package with low Joomla web reachability by default")
        priority_adjustment -= 35

    if local_or_cli and not web_exposed:
        reasons.append("description suggests local/CLI/file-processing precondition rather than remote web path")
        priority_adjustment -= 20

    if is_os_package and not web_exposed and not high_impact:
        quick_exit = True
        reasons.append("quick-exit: OS package lacks remote/Web/high-impact trigger signal and is not expected in Joomla/PHP web request path")
    elif is_os_package and name in LOW_VALUE_SYSTEM_NAMES and local_or_cli and not web_exposed:
        quick_exit = True
        reasons.append("quick-exit: base OS package plus local/CLI precondition")

    lane = "quick_exit" if quick_exit else ("hot_path" if web_exposed or high_impact else "normal")
    return {
        "lane": lane,
        "package_type": package_type,
        "quick_exit": quick_exit,
        "reasons": reasons,
        "priority_adjustment": priority_adjustment,
    }


def first_str(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return ""


def infer_ecosystem(language: str, vendor: str, item: dict[str, Any]) -> str:
    text = " ".join([language or "", vendor or "", str(item.get("purl", ""))]).lower()
    if "pkg:deb/" in text or "debian" in text or text.strip() == "deb":
        return "deb"
    if "pkg:rpm/" in text or text.strip() == "rpm":
        return "rpm"
    if "pkg:apk/" in text or text.strip() == "apk":
        return "apk"
    if "maven" in text or "java" in text or "." in (vendor or ""):
        return "maven"
    if "npm" in text or "javascript" in text or "node" in text:
        return "npm"
    if "pip" in text or "python" in text or "pypi" in text:
        return "pip"
    if "composer" in text or "php" in text:
        return "composer"
    if "go" in text or "golang" in text:
        return "go"
    return "unknown"


def build_purl(ecosystem: str, vendor: str, name: str, version: str) -> str:
    if ecosystem == "maven" and vendor:
        return f"pkg:maven/{vendor}/{name}@{version}"
    if vendor:
        return f"pkg:{ecosystem}/{vendor}/{name}@{version}"
    return f"pkg:{ecosystem}/{name}@{version}"


def normalize_references(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        refs: list[str] = []
        for item in raw:
            if isinstance(item, str):
                refs.append(item)
            elif isinstance(item, dict):
                value = item.get("url") or item.get("href")
                if isinstance(value, str):
                    refs.append(value)
        return refs
    return []


if __name__ == "__main__":
    raise SystemExit(main())
