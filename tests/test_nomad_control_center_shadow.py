from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SHADOW_ROOT = REPOSITORY_ROOT / "deploy" / "nomad" / "control-center-shadow"
ARTIFACT = SHADOW_ROOT / "live15-control-center-shadow.ps1"
JOBSPEC = SHADOW_ROOT / "live15-control-center-shadow.nomad.hcl"
STAGER = REPOSITORY_ROOT / "tools" / "stage_nomad_control_center_shadow.ps1"


class NomadControlCenterShadowTest(unittest.TestCase):
    def test_jobspec_is_nomad_native_and_pins_the_artifact(self) -> None:
        artifact_hash = hashlib.sha256(ARTIFACT.read_bytes()).hexdigest().upper()
        jobspec = JOBSPEC.read_text(encoding="utf-8")
        attributes = (REPOSITORY_ROOT / ".gitattributes").read_text(encoding="utf-8")

        self.assertIn('job "live15-control-center-shadow"', jobspec)
        self.assertIn('provider = "nomad"', jobspec)
        self.assertIn('health_check      = "checks"', jobspec)
        self.assertIn('auto_revert       = true', jobspec)
        self.assertIn('path     = "/_nomad/healthz"', jobspec)
        self.assertIn('static       = 18081', jobspec)
        self.assertIn(artifact_hash, jobspec)
        self.assertIn(
            "deploy/nomad/control-center-shadow/live15-control-center-shadow.ps1 -text",
            attributes,
        )
        self.assertNotIn("LIVE15_KALSHI_PRODUCTION", jobspec)
        self.assertNotIn("LIVE15_QUANT", jobspec)

    def test_artifact_is_read_only_and_fails_closed_without_a_projection_source(self) -> None:
        artifact_hash = hashlib.sha256(ARTIFACT.read_bytes()).hexdigest().upper()
        with tempfile.TemporaryDirectory() as directory:
            evidence_log = Path(directory) / "artifact.log"
            port = self._available_port()
            process = subprocess.Popen(
                [
                    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ARTIFACT),
                    "-Port",
                    str(port),
                    "-ExpectedSha256",
                    artifact_hash,
                    "-EvidenceLog",
                    str(evidence_log),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                health = self._request_until_ready(port, "/_nomad/healthz")
                projection = self._request(port, "/api/health")
                markets = self._request(port, "/api/markets")
                mutation = self._request(port, "/api/recorder/pause", method="POST")
            finally:
                process.terminate()
                process.wait(timeout=10)

        self.assertEqual(health[0], 200)
        self.assertEqual(json.loads(health[1])["production"], False)
        self.assertEqual(json.loads(health[1])["read_only"], True)
        self.assertEqual(projection[0], 503)
        self.assertEqual(markets[0], 503)
        self.assertEqual(json.loads(projection[1])["fail_closed"], True)
        self.assertEqual(mutation[0], 405)

    def test_artifact_refuses_a_mismatched_hash_before_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ARTIFACT),
                    "-Port",
                    str(self._available_port()),
                    "-ExpectedSha256",
                    "0" * 64,
                    "-EvidenceLog",
                    str(Path(directory) / "artifact.log"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)

    def test_stager_rejects_a_source_other_than_its_own_checkout(self) -> None:
        result = subprocess.run(
            [
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(STAGER),
                "-ProjectRoot",
                r"D:\LIVE15_NOMAD_POC\generic-poc",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source root must match the staging script checkout", result.stderr)

    def test_stager_is_read_only_sealed_preflight(self) -> None:
        stager = STAGER.read_text(encoding="utf-8")

        self.assertIn('Owner -ne "BUILTIN\\Administrators"', stager)
        self.assertIn("Assert-SealedDescendants", stager)
        self.assertIn("staged child ACL is not the exact inherited sealed policy", stager)
        self.assertIn("staged root is a reparse point", stager)
        self.assertLess(
            stager.index("staged root is a reparse point"),
            stager.index("required externally provisioned sealed input"),
        )
        self.assertIn("VALIDATED_STAGED", stager)
        self.assertNotIn("New-Item", stager)
        self.assertNotIn("Copy-Item", stager)
        self.assertNotIn("/setowner", stager)
        self.assertNotIn("/grant:r", stager)
        self.assertNotIn("[IO.File]::Open", stager)

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _request_until_ready(self, port: int, path: str) -> tuple[int, str]:
        deadline = time.monotonic() + 10
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            try:
                return self._request(port, path)
            except OSError as error:
                last_error = error
                time.sleep(0.1)
        self.fail(f"shadow artifact did not bind within 10 seconds: {last_error}")

    @staticmethod
    def _request(port: int, path: str, *, method: str = "GET") -> tuple[int, str]:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            request = (
                f"{method} {path} HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            connection.sendall(request)
            response = bytearray()
            while data := connection.recv(4096):
                response.extend(data)
        header, body = bytes(response).split(b"\r\n\r\n", maxsplit=1)
        return int(header.split(b" ")[1]), body.decode("utf-8")
