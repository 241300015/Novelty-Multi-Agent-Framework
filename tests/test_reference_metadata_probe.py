"""Offline contract and transport tests; no live DNS, sockets, or API calls."""

import json
import signal
import socket
import ssl
from types import SimpleNamespace

import pytest

from scripts import probe_reference_metadata_apis as p


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in list(p.os.environ):
        if key.lower() in p.PROXY_KEYS:
            monkeypatch.delenv(key)

    def forbidden(*args, **kwargs):
        raise AssertionError("Live network is forbidden in these tests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


def args(**kwargs):
    values = dict(region="unconfirmed", network="unconfirmed", attest_direct=False,
                  timeout_seconds=1, count=1, interval_seconds=300)
    values.update(kwargs)
    return SimpleNamespace(**values)


def payload(case, doi=None):
    doi = case.doi if doi is None else doi
    return {
        "doi.org": {"responseCode": 1, "handle": doi,
                    "values": [{"type": "URL", "data": {"value": "https://example.org"}}]},
        "crossref": {"status": "ok", "message": {"DOI": doi, "title": ["Deep learning"]}},
        "datacite": {"data": {"type": "dois", "id": doi, "attributes": {"doi": doi}}},
    }[case.provider]


@pytest.mark.parametrize("case", p.POSITIVE_CASES)
def test_success_and_doi_replacement(case):
    assert p.classify(case, 200, "application/json; charset=utf-8", json.dumps(payload(case)).encode()) == (
        "found", True, case.doi)
    assert p.classify(case, 200, "application/json", json.dumps(payload(case, case.doi.upper())).encode())[1]
    assert p.classify(case, 200, "application/json", json.dumps(payload(case, "10.9999/wrong")).encode())[:2] == (
        "doi_mismatch", False)


@pytest.mark.parametrize("case", p.POSITIVE_CASES)
@pytest.mark.parametrize("status,outcome", [(403, "access_denied"), (429, "rate_limited"), (502, "http_502"), (302, "http_302")])
def test_http_failure_never_means_nonexistent(case, status, outcome):
    for expected in ("found", "not_found"):
        test_case = p.Case(case.name, case.provider, case.doi, expected)
        assert p.classify(test_case, status, "application/json", b'{"errors": []}')[:2] == (outcome, False)


@pytest.mark.parametrize("content_type,body,outcome", [
    ("text/html", b"<html>captcha</html>", "non_json_response"),
    ("application/json", b"<html>error</html>", "invalid_json"),
    ("application/json", b"[]", "invalid_schema"),
    ("application/json", b'{"message":null}', "invalid_schema"),
])
def test_html_and_error_json(content_type, body, outcome):
    assert p.classify(p.POSITIVE_CASES[2], 200, content_type, body)[:2] == (outcome, False)


def missing_response(case):
    if case.provider == "doi.org":
        return "application/json", json.dumps({"responseCode": 100, "handle": case.doi}).encode()
    if case.provider == "crossref":
        return "text/plain", b"Resource not found."
    return "application/vnd.api+json", json.dumps({"errors": [{
        "status": "404", "title": "The resource you are looking for doesn't exist."}]}).encode()


@pytest.mark.parametrize("case", p.negative_cases("offline"))
def test_explicit_not_found_and_positive_404(case):
    content_type, body = missing_response(case)
    assert p.classify(case, 404, content_type, body)[:2] == ("not_found", True)
    positive = p.Case(case.name, case.provider, case.doi)
    assert p.classify(positive, 404, content_type, body)[:2] == ("not_found", False)
    assert not p.classify(case, 404, "text/html", b"<html>edge not found</html>")[1]
    assert not p.classify(case, 200, "application/json", json.dumps(payload(case)).encode())[1]


def test_handle_missing_values_and_wrong_negative_handle():
    case = p.POSITIVE_CASES[0]
    body = {"handle": case.doi, "responseCode": 1, "values": []}
    assert not p.classify(case, 200, "application/json", json.dumps(body).encode())[1]
    body.update(handle="10.9999/wrong", responseCode=100)
    assert p.classify(p.negative_cases("offline")[0], 404, "application/json", json.dumps(body).encode())[0] == "doi_mismatch"


@pytest.mark.parametrize("key", ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "hTtP_pRoXy"])
def test_proxy_rejected_before_dns(monkeypatch, key):
    monkeypatch.setenv(key, "http://secret:password@proxy.invalid")
    sample = p.probe(p.POSITIVE_CASES[0], args(), 1)
    assert sample["actual"] == "proxy_environment_rejected"
    assert sample["dns_ms"] is None
    assert "secret" not in json.dumps(sample)
    assert p.proxy_variables() == [key]


def fake_transport(monkeypatch, failure_stage=None, error=None, status=200, case=None, read_error=None):
    events = []
    case = p.POSITIVE_CASES[2] if case is None else case
    host = p.PROVIDERS[case.provider][0]

    def fail(stage):
        events.append(stage)
        if stage == failure_stage:
            raise error

    def resolve(*a):
        fail("dns")
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]

    class Socket:
        def settimeout(self, timeout):
            assert timeout == 1

        def connect(self, address):
            assert address[0] == "2001:db8::1"
            fail("tcp")

        def getpeername(self):
            return ("2001:db8::1", 443)

        def sendall(self, data):
            assert f"Host: {host}\r\n".encode() in data
            assert b"Authorization" not in data
            fail("http")

        def close(self):
            events.append("close")

    class Context:
        def wrap_socket(self, raw, server_hostname):
            assert server_hostname == host
            fail("tls")
            return raw

    class Response:
        def __init__(self, sock):
            self.status = status

        def begin(self):
            pass

        def getheader(self, name, default=None):
            return "application/json" if name == "Content-Type" else default

        def read(self, limit):
            assert limit == p.MAX_BODY_BYTES + 1
            if read_error is not None:
                raise read_error
            return json.dumps(payload(case)).encode()

        def close(self):
            events.append("response_close")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", lambda *a: Socket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(p.http.client, "HTTPResponse", Response)
    return events


@pytest.mark.parametrize("stage,error,outcome", [
    ("dns", socket.gaierror(-2, "not known"), "network_or_protocol_error"),
    ("dns", p.StageTimeout(), "timeout"),
    ("tcp", ConnectionRefusedError(), "network_or_protocol_error"),
    ("tcp", TimeoutError(), "timeout"),
    ("tls", ssl.SSLCertVerificationError(), "tls_certificate_error"),
    ("tls", ssl.SSLError(), "tls_error"),
    ("http", p.StageTimeout(), "timeout"),
    ("http", KeyboardInterrupt(), "interrupted"),
])
def test_transport_failures_no_retries(monkeypatch, stage, error, outcome):
    events = fake_transport(monkeypatch, stage, error)
    sample = p.probe(p.POSITIVE_CASES[2], args(), 1)
    assert sample["actual"] == outcome
    assert sample["failure_stage"] == stage
    assert sample[stage + "_ms"] is not None
    assert not sample["functional"]
    assert sample["reachable"] == (stage == "http")
    assert events.count(stage) == 1
    if stage != "dns":
        assert "close" in events


def test_native_transport_success(monkeypatch):
    events = fake_transport(monkeypatch)
    sample = p.probe(p.POSITIVE_CASES[2], args(), 1)
    assert sample["functional"] and sample["tls_verified"] and sample["reachable"]
    assert sample["remote_ip"] == "2001:db8::1" and sample["ip_version"] == "IPv6"
    assert len(sample["resolved_addresses"]) == 2
    assert sample["failure_stage"] is None
    assert events[:4] == ["dns", "tcp", "tls", "http"]


def test_stage_deadline_restores_handler():
    previous = signal.getsignal(signal.SIGALRM)
    with pytest.raises(p.StageTimeout):
        with p.stage_timer(.01):
            p.time.sleep(.1)
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def sample(case, cycle=1, success=True, elapsed=100):
    return dict(provider=case.provider, expected=case.expected, cycle=cycle,
                functional=success, total_ms=elapsed, reachable=True,
                actual="found" if success else "timeout",
                failure_stage=None if success else "http")


def test_summary_separates_controls_failures_and_incomplete_windows():
    case = p.POSITIVE_CASES[2]
    rows = [sample(case, 1, True, 100), sample(case, 2, True, 500)]
    rows += [sample(case, cycle, False) for cycle in (3, 4, 5)]
    rows += [sample(p.negative_cases("offline")[1])]
    result = p.summarize(rows, args(count=5), "start", 1500, False, None)
    stats = result["providers"]["crossref"]
    assert stats["positive_functional_success_rate"] == .4
    assert stats["successful_positive_p50_ms"] == 100
    assert stats["successful_positive_p95_ms"] == 500
    assert stats["negative_controls_passed"] == 1
    assert stats["max_consecutive_all_failed_cycles"] == 3
    assert stats["positive_timeout_count"] == 3
    assert stats["failures"] == {"http:timeout": 3}
    assert not stats["full_24h_window"]
    assert not result["mainland_direct_evidence_eligible"]
    assert result["mainland_execution_status"] == "NOT_EXECUTED_ON_MAINLAND"


def test_full_window_needs_all_samples_and_attestation():
    config = args(count=288, attest_direct=True, region="cn-test", network="operator-test")
    rows = [sample(case, cycle) for cycle in range(1, 289) for case in p.POSITIVE_CASES]
    result = p.summarize(rows, config, "start", 86400, False, None)
    assert result["mainland_direct_evidence_eligible"]
    assert all(v["meets_single_egress_metrics"] for v in result["providers"].values())
    assert result["cross_egress_validation"] == "pending"
    assert not p.summarize(rows, config, "start", 86399, False, None)["providers"]["doi.org"]["full_24h_window"]
    assert not p.summarize(rows[:-4], config, "start", 86400, False, None)["providers"]["doi.org"]["full_24h_window"]
    assert not p.summarize(rows, config, "start", 86400, True, "interrupted")["providers"]["doi.org"]["full_24h_window"]


@pytest.mark.parametrize("extra", [["--count", "0"], ["--interval-seconds", "1"],
                                   ["--timeout-seconds", "nan"], ["--attest-direct"]])
def test_cli_invalid_configuration(extra):
    with pytest.raises(SystemExit):
        p.parse_args(extra)


def test_runner_negative_once_and_full_window(monkeypatch, tmp_path):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(p.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(p.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def fake_probe(case, config, cycle):
        calls.append((case.name, cycle))
        return dict(sample(case, cycle), case=case.name, dns_ms=1, tcp_ms=1, tls_ms=1,
                    http_ms=1, http_status=200, record_doi=case.doi, exception_type=None)

    monkeypatch.setattr(p, "probe", fake_probe)
    assert p.main(["--count", "288", "--negative-controls", "--output-root", str(tmp_path)]) == 0
    assert len(calls) == 1155
    assert sum(name.startswith("N") for name, _ in calls) == 3
    assert clock[0] == 86400
    summary = json.loads(next(tmp_path.glob("*/summary.json")).read_text())
    assert summary["positive_samples"] == 1152 and summary["negative_samples"] == 3
    assert not summary["interrupted"]
    assert not summary["mainland_direct_evidence_eligible"]
    assert len(next(tmp_path.glob("*/samples.jsonl")).read_text().splitlines()) == 1155


def test_runner_proxy_block_writes_audit_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("ALL_PROXY", "socks5://secret:password@proxy.invalid")
    assert p.main(["--output-root", str(tmp_path)]) == 2
    summary = json.loads(next(tmp_path.glob("*/summary.json")).read_text())
    assert summary["stop_reason"] == "proxy_environment_rejected"
    assert summary["positive_samples"] == 0
    assert "secret" not in json.dumps(summary)


@pytest.mark.parametrize("outcome,code", [("rate_limited", 2), ("interrupted", 130)])
def test_runner_stops_and_persists(monkeypatch, tmp_path, outcome, code):
    fake_transport(monkeypatch, "http", KeyboardInterrupt(), case=p.POSITIVE_CASES[0])
    if outcome == "rate_limited":
        fake_transport(monkeypatch, status=429, case=p.POSITIVE_CASES[0])
    assert p.main(["--count", "2", "--timeout-seconds", "1", "--output-root", str(tmp_path)]) == code
    summary = json.loads(next(tmp_path.glob("*/summary.json")).read_text())
    assert summary["interrupted"] and summary["stop_reason"] == outcome
    assert summary["positive_samples"] == 1


def test_malformed_datacite_error_is_not_not_found():
    case = p.negative_cases("offline")[2]
    for title in (None, [], {}, 404, "CDN cache miss"):
        body = json.dumps({"errors": [{"status": "404", "title": title}]}).encode()
        assert not p.classify(case, 404, "application/json", body)[1]


def test_429_stops_even_when_body_times_out(monkeypatch, tmp_path):
    fake_transport(monkeypatch, status=429, case=p.POSITIVE_CASES[0], read_error=p.StageTimeout())
    assert p.main(["--count", "2", "--timeout-seconds", "1", "--output-root", str(tmp_path)]) == 2
    summary = json.loads(next(tmp_path.glob("*/summary.json")).read_text())
    assert summary["stop_reason"] == "rate_limited" and summary["positive_samples"] == 1
    row = json.loads(next(tmp_path.glob("*/samples.jsonl")).read_text())
    assert row["http_status"] == 429 and row["actual"] == "timeout"
