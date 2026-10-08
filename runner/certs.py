"""certs — a throwaway CA and one leaf for the run's target hosts, so Safari on the simulator trusts the runner proxy
under the real hostnames. The CA goes into the booted simulator's keychain (`xcrun simctl keychain <udid>
add-root-cert`) and dies with the runner. `guide iphone:tunnel`.
"""

import datetime
import json
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VALID_DAYS = 2
CA_NAME = "Nexus iPhone run CA"
CA_KEY_USAGE = x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                             data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                             encipher_only=False, decipher_only=False)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _window():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now - datetime.timedelta(hours=1), now + datetime.timedelta(days=VALID_DAYS)


def _ca():
    key = ec.generate_private_key(ec.SECP256R1())
    not_before, not_after = _window()
    cert = (x509.CertificateBuilder().subject_name(_name(CA_NAME)).issuer_name(_name(CA_NAME))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(not_before).not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(CA_KEY_USAGE, critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca_pair, hosts):
    ca_key, ca_cert = ca_pair
    key = ec.generate_private_key(ec.SECP256R1())
    not_before, not_after = _window()
    cert = (x509.CertificateBuilder().subject_name(_name(hosts[0])).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(not_before).not_valid_after(not_after)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host) for host in hosts]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def _write_pair(stem: Path, pair):
    key, cert = pair
    stem.with_suffix(".pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    stem.with_suffix(".key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def main():
    run_dir = Path(sys.argv[1])
    run = json.loads((run_dir / "run.json").read_text())
    hosts = sorted({origin["host"] for origin in run["origins"]})
    ca_pair = _ca()
    _write_pair(run_dir / "ca", ca_pair)
    _write_pair(run_dir / "leaf", _leaf(ca_pair, hosts))
    print(f"certs: CA + leaf for {len(hosts)} host(s)")


if __name__ == "__main__":
    main()
