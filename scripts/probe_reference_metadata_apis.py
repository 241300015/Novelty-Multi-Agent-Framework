#!/usr/bin/env python3
"""Low-frequency, standard-library-only DOI API probe (Linux/POSIX).

Never uses proxies, redirects, automatic retries, API keys, or an LLM.
Operator attestation is required for mainland evidence; it is not geolocation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import json
import math
import os
from pathlib import Path
import signal
import socket
import ssl
import sys
import time
from urllib.parse import quote
import uuid


PROVIDERS = {
    "doi.org": ("doi.org", "/api/handles/"),
    "crossref": ("api.crossref.org", "/works/"),
    "datacite": ("api.datacite.org", "/dois/"),
}
PROXY_KEYS = {"http_proxy", "https_proxy", "all_proxy"}
MAX_BODY_BYTES = 4 * 1024 * 1024
ATTESTATION = (
    "Operator confirms this is a server physically deployed in mainland China, "
    "with no explicit/transparent proxy or VPN on the outbound path. "
    "The script cannot independently verify geography or routing."
)


@dataclass(frozen=True)
class Case:
    name: str
    provider: str
    doi: str
    expected: str = "found"


POSITIVE_CASES = (
    Case("P1", "doi.org", "10.1038/nature14539"),
    Case("P2", "doi.org", "10.14454/qdd3-ps68"),
    Case("P3", "crossref", "10.1038/nature14539"),
    Case("P4", "datacite", "10.14454/qdd3-ps68"),
)


def negative_cases(token: str) -> tuple[Case, ...]:
    # Under a syntactically valid prefix; never asserts global non-registration.
    doi = "10.1038/novelty-probe-unregistered-" + token
    return tuple(Case(f"N{i}", provider, doi, "not_found")
                 for i, provider in enumerate(PROVIDERS, 1))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def proxy_variables(environ=None) -> list[str]:
    environ = os.environ if environ is None else environ
    # Only retain names, never values which might contain credentials.
    return sorted(k for k, v in environ.items() if k.lower() in PROXY_KEYS and v)


class StageTimeout(TimeoutError):
    pass


@contextmanager
def stage_timer(seconds: float):
    """Bound the whole stage, including getaddrinfo and slow HTTP bodies.

    POSIX main thread only; socket timeouts alone do not bound DNS or slow reads.
    """
    def expired(signum, frame):
        raise StageTimeout("stage deadline exceeded")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def same_doi(value, expected: str) -> bool:
    return isinstance(value, str) and value.casefold() == expected.casefold()


def classify(case: Case, status: int, content_type: str, body: bytes) -> tuple[str, bool, str | None]:
    """Return actual outcome, functional success, and observed record DOI."""
    if status == 429:
        return "rate_limited", False, None
    if status in (401, 403):
        return "access_denied", False, None
    if status not in (200, 404):
        return f"http_{status}", False, None
    media_type = content_type.split(";", 1)[0].strip().lower()
    # Crossref's singleton 404 is commonly a plain-text Resource not found.
    if (case.provider == "crossref" and status == 404
            and media_type == "text/plain"
            and body.strip().lower() in (b"resource not found.", b"resource not found")):
        return "not_found", case.expected == "not_found", None
    if not (media_type == "application/json" or media_type.endswith("+json")):
        return "non_json_response", False, None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return "invalid_json", False, None
    if not isinstance(payload, dict):
        return "invalid_schema", False, None
    actual_doi = None
    if case.provider == "doi.org":
        actual_doi = payload.get("handle")
        if not same_doi(actual_doi, case.doi):
            return "doi_mismatch", False, actual_doi if isinstance(actual_doi, str) else None
        if status == 404 and payload.get("responseCode") == 100:
            return "not_found", case.expected == "not_found", actual_doi
        valid = (status == 200 and type(payload.get("responseCode")) is int
                 and payload["responseCode"] == 1
                 and isinstance(payload.get("values"), list) and bool(payload["values"])
                 and all(isinstance(v, dict) and isinstance(v.get("data"), dict)
                         and isinstance(v.get("type"), str) for v in payload["values"]))
    elif case.provider == "crossref":
        message = payload.get("message")
        if not isinstance(message, dict):
            return "invalid_schema", False, None
        actual_doi = message.get("DOI")
        valid = status == 200 and payload.get("status") == "ok"
    else:
        errors = payload.get("errors")
        if (status == 404 and isinstance(errors, list) and errors
                and all(isinstance(e, dict) and str(e.get("status")) == "404"
                        and isinstance(e.get("title"), str)
                        and e["title"].lower() in (
                            "the resource you are looking for doesn't exist.", "not found")
                        for e in errors)):
            return "not_found", case.expected == "not_found", None
        data = payload.get("data")
        if not isinstance(data, dict):
            return "invalid_schema", False, None
        actual_doi = data.get("id")
        valid = status == 200 and data.get("type") == "dois"
        attributes = data.get("attributes")
        if isinstance(attributes, dict) and "doi" in attributes:
            valid = valid and same_doi(attributes["doi"], case.doi)
    if not same_doi(actual_doi, case.doi):
        return "doi_mismatch", False, actual_doi if isinstance(actual_doi, str) else None
    if not valid:
        return "invalid_schema", False, actual_doi
    return "found", case.expected == "found", actual_doi


def probe(case: Case, args, cycle: int) -> dict:
    host, prefix = PROVIDERS[case.provider]
    sample = {
        "timestamp_utc": utc_now(), "region": args.region, "network": args.network,
        "provider": case.provider, "host": host, "case": case.name, "doi": case.doi,
        "cycle": cycle, "expected": case.expected, "actual": None, "functional": False,
        "reachable": False, "http_status": None, "failure_stage": None,
        "exception_type": None, "record_doi": None, "remote_ip": None,
        "ip_version": None, "requested_ip_family": args.ip_family,
        "resolved_addresses": [], "tls_verified": False,
        "operator_attested_mainland_direct": args.attest_direct,
        "dns_ms": None, "tcp_ms": None, "tls_ms": None, "http_ms": None,
        "total_ms": None,
    }
    start = time.monotonic()
    stage = "environment"
    raw = secure = response = None

    @contextmanager
    def measured(name):
        nonlocal stage
        stage = name
        begin = time.monotonic()
        try:
            with stage_timer(args.timeout_seconds):
                yield
        finally:
            sample[name + "_ms"] = round((time.monotonic() - begin) * 1000, 3)

    try:
        if proxy_variables():
            sample["actual"] = "proxy_environment_rejected"
            sample["failure_stage"] = stage
            return sample
        with measured("dns"):
            requested_family = {"auto": socket.AF_UNSPEC, "ipv4": socket.AF_INET,
                                "ipv6": socket.AF_INET6}[args.ip_family]
            addresses = socket.getaddrinfo(host, 443, requested_family, socket.SOCK_STREAM)
            sample["resolved_addresses"] = [
                {"ip": item[4][0], "family": "IPv6" if item[0] == socket.AF_INET6 else "IPv4"}
                for item in addresses
            ]
            # First system resolver result only: do not conceal failures via fallback.
            family, socktype, proto, _, address = addresses[0]
            sample["remote_ip"] = address[0]
            sample["ip_version"] = "IPv6" if family == socket.AF_INET6 else "IPv4"
        with measured("tcp"):
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(args.timeout_seconds)
            raw.connect(address)
            sample["remote_ip"] = raw.getpeername()[0]
        with measured("tls"):
            context = ssl.create_default_context()
            secure = context.wrap_socket(raw, server_hostname=host)
            sample["tls_verified"] = True
            sample["reachable"] = True
        with measured("http"):
            path = prefix + quote(case.doi, safe="/")
            request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                       "User-Agent: Novelty-DOI-Metadata-Probe/1.0\r\n"
                       "Accept: application/json\r\nAccept-Encoding: identity\r\n"
                       "Connection: close\r\n\r\n")
            secure.sendall(request.encode("ascii"))
            response = http.client.HTTPResponse(secure)
            response.begin()
            sample["http_status"] = response.status
            sample["content_type"] = response.getheader("Content-Type", "")
            sample["retry_after"] = response.getheader("Retry-After")
            body = response.read(MAX_BODY_BYTES + 1)
        stage = "semantic"
        if len(body) > MAX_BODY_BYTES:
            sample["actual"] = "response_too_large"
        else:
            sample["actual"], sample["functional"], sample["record_doi"] = classify(
                case, response.status, sample["content_type"], body)
        if not sample["functional"]:
            sample["failure_stage"] = "http" if response.status not in (200, 404) else "semantic"
    except KeyboardInterrupt:
        sample.update(actual="interrupted", failure_stage=stage, exception_type="KeyboardInterrupt")
    except (OSError, ValueError, IndexError, http.client.HTTPException) as exc:
        if isinstance(exc, TimeoutError):
            outcome = "timeout"
        elif isinstance(exc, ssl.SSLCertVerificationError):
            outcome = "tls_certificate_error"
        elif isinstance(exc, ssl.SSLError):
            outcome = "tls_error"
        else:
            outcome = "network_or_protocol_error"
        sample.update(actual=outcome, failure_stage=stage, exception_type=type(exc).__name__)
    finally:
        for resource in (response, secure, raw):
            if resource is not None:
                resource.close()
        sample["total_ms"] = round((time.monotonic() - start) * 1000, 3)
    return sample


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile, so small samples never invent intermediate values."""
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(samples: list[dict], args, started: str, elapsed: float,
              interrupted: bool, stop_reason: str | None) -> dict:
    eligible = (args.attest_direct and not proxy_variables()
                and all(s["actual"] != "proxy_environment_rejected" for s in samples))
    summary = {
        "schema_version": 1, "started_utc": started, "updated_utc": utc_now(),
        "region": args.region, "network": args.network, "elapsed_seconds": round(elapsed, 3),
        "planned_cycles": args.count, "interval_seconds": args.interval_seconds,
        "timeout_seconds_per_stage": args.timeout_seconds,
        "requested_ip_family": args.ip_family,
        "operator_attestation": ATTESTATION if args.attest_direct else None,
        "operator_attested_mainland_direct": args.attest_direct,
        "mainland_direct_evidence_eligible": bool(eligible),
        "mainland_execution_status": "OPERATOR_ATTESTED" if eligible else "NOT_EXECUTED_ON_MAINLAND",
        "geography_independently_verified": False,
        "interrupted": interrupted, "stop_reason": stop_reason,
        "positive_samples": sum(s["expected"] == "found" for s in samples),
        "negative_samples": sum(s["expected"] == "not_found" for s in samples),
        "cross_egress_validation": "pending", "providers": {},
    }
    for provider in PROVIDERS:
        rows = [s for s in samples if s["provider"] == provider]
        positive = [s for s in rows if s["expected"] == "found"]
        negative = [s for s in rows if s["expected"] == "not_found"]
        good = [s for s in positive if s["functional"]]
        latency = [s["total_ms"] for s in good]
        rate = len(good) / len(positive) if positive else None
        expected_per_cycle = sum(c.provider == provider for c in POSITIVE_CASES)
        by_cycle = {}
        for row in positive:
            by_cycle.setdefault(row["cycle"], []).append(row)
        streak = longest = complete_cycles = 0
        previous_cycle = None
        for cycle, cycle_rows in sorted(by_cycle.items()):
            complete = len(cycle_rows) == expected_per_cycle
            complete_cycles += complete
            if previous_cycle is not None and cycle != previous_cycle + 1:
                streak = 0
            streak = streak + 1 if complete and not any(r["functional"] for r in cycle_rows) else 0
            longest = max(longest, streak)
            previous_cycle = cycle
        p95 = percentile(latency, .95)
        full_window = (not interrupted and stop_reason is None and args.count >= 288
                       and complete_cycles == args.count and elapsed >= 86400)
        meets_metrics = bool(full_window and rate is not None and rate >= .99
                             and p95 is not None and p95 <= 5000 and longest < 3)
        summary["providers"][provider] = {
            "samples": len(rows), "positive_samples": len(positive),
            "positive_functional_successes": len(good), "positive_functional_success_rate": rate,
            "reachable_samples": sum(s["reachable"] for s in rows),
            "functional_success_rate_including_controls":
                sum(s["functional"] for s in rows) / len(rows) if rows else None,
            "successful_positive_p50_ms": percentile(latency, .50),
            "successful_positive_p95_ms": p95,
            "failures": dict(Counter(f"{s['failure_stage']}:{s['actual']}" for s in rows if not s["functional"])),
            "positive_timeout_count": sum(s["actual"] == "timeout" for s in positive),
            "negative_controls": len(negative),
            "negative_controls_passed": sum(s["functional"] for s in negative),
            "complete_cycles": complete_cycles,
            "max_consecutive_all_failed_cycles": longest,
            "full_24h_window": full_window, "meets_single_egress_metrics": meets_metrics,
            "conclusion": ("single_egress_metrics_pass_preflight_and_cross_validation_pending"
                           if eligible and meets_metrics else "pending"),
        }
    return summary


def save_summary(path: Path, data: dict):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="unconfirmed")
    parser.add_argument("--network", default="unconfirmed")
    parser.add_argument("--attest-direct", action="store_true", help=ATTESTATION)
    parser.add_argument("--negative-controls", action="store_true", help="Three controls, first cycle only")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=300)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--ip-family", choices=("auto", "ipv4", "ipv6"), default="auto",
                        help="Select address family for an independent run; never falls back")
    parser.add_argument("--output-root", type=Path, default=Path("outputs/reference_metadata_probe"))
    args = parser.parse_args(argv)
    if not hasattr(signal, "setitimer"):
        parser.error("Linux/POSIX with setitimer is required for bounded DNS and HTTP stages")
    if args.count < 1 or not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("count and timeout must be positive and finite")
    if not math.isfinite(args.interval_seconds) or args.interval_seconds < 60:
        parser.error("interval must be finite and at least 60 seconds (public API protection)")
    if args.attest_direct and any(v.strip().lower() in ("", "unconfirmed", "unknown")
                                  for v in (args.region, args.network)):
        parser.error("attestation requires actual --region and --network")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    started, begin = utc_now(), time.monotonic()
    token = uuid.uuid4().hex
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + token[:8]
    directory = args.output_root / run_id
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    samples = []
    interrupted, stop_reason = True, "running_or_process_terminated"
    print(f"Output: {directory}", flush=True)

    def checkpoint():
        save_summary(directory / "summary.json", summarize(
            samples, args, started, time.monotonic() - begin, interrupted, stop_reason))

    def terminate(signum, frame):
        raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        checkpoint()
        with (directory / "samples.jsonl").open("x", encoding="utf-8") as stream:
            blocked = proxy_variables()
            if blocked:
                stop_reason = "proxy_environment_rejected"
                print("Refusing network requests; proxy variable names: " + ", ".join(blocked), flush=True)
            else:
                for cycle in range(1, args.count + 1):
                    cycle_start = time.monotonic()
                    cases = POSITIVE_CASES + (negative_cases(token) if args.negative_controls and cycle == 1 else ())
                    for case in cases:
                        sample = probe(case, args, cycle)
                        samples.append(sample)
                        stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
                        stream.flush()
                        checkpoint()
                        timings = " ".join(f"{s.upper()}={sample[s + '_ms']}ms" for s in ("dns", "tcp", "tls", "http"))
                        print(f"cycle={cycle} {case.name} {case.provider} {timings} "
                              f"HTTP_status={sample['http_status']} actual={sample['actual']} "
                              f"functional={sample['functional']} record_doi={sample['record_doi']} "
                              f"failure_stage={sample['failure_stage']} exception={sample['exception_type']}", flush=True)
                        if sample["http_status"] == 429:
                            stop_reason = "rate_limited"
                            break
                        if sample["actual"] in ("interrupted", "rate_limited", "proxy_environment_rejected"):
                            stop_reason = sample["actual"]
                            break
                    if stop_reason != "running_or_process_terminated":
                        break
                    # Include the last observation interval: 288 x 300s is a full day.
                    # Never catch up with a burst after a delayed cycle.
                    if args.count > 1:
                        time.sleep(max(0, cycle_start + args.interval_seconds - time.monotonic()))
                else:
                    interrupted, stop_reason = False, None
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    finally:
        checkpoint()
        signal.signal(signal.SIGTERM, previous_term)
    return 130 if stop_reason == "interrupted" else (2 if interrupted or any(not s["functional"] for s in samples) else 0)


if __name__ == "__main__":
    sys.exit(main())
