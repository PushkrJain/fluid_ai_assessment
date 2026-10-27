import pytest

from app.document import get_output_dir


def test_download_generated_file(happy, client):
    body = client.post("/agent", json={"request": "Create a project plan for an onboarding portal."}).json()
    r = client.get(body["download_url"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert r.content[:2] == b"PK"  # a .docx is a zip container


@pytest.mark.parametrize("name", [
    "..%2F..%2Fetc%2Fpasswd", "%2e%2e%2fsecret.docx", "..\\secret.docx", "notes.txt",
    "report.docx", ("a" * 32) + ".exe", ("A" * 32) + ".docx", ("a" * 31) + ".docx",
])
def test_bad_names_are_rejected(client, name):
    assert client.get(f"/documents/{name}").status_code in (400, 404)


def test_unknown_valid_name_is_404(client, tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    assert client.get(f"/documents/{'d' * 32}.docx").status_code == 404


def test_symlink_escape_is_rejected(client, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.docx").write_bytes(b"secret")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("OUTPUT_DIR", str(out))
    link = out / (("e" * 32) + ".docx")
    link.symlink_to(outside / "secret.docx")
    assert client.get(f"/documents/{link.name}").status_code == 404
    assert get_output_dir() == out.resolve()


@pytest.mark.parametrize("name", [("a" * 33) + ".docx", "..%2Fx.docx", "../x.docx", ("a" * 32) + ".docx.exe", "a.docx", ("g" * 32) + ".docx"])
def test_more_bad_names_never_serve_a_file(client, tmp_path, monkeypatch, name):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    (tmp_path / "x.docx").write_bytes(b"PK-secret")
    r = client.get(f"/documents/{name}")
    assert r.status_code in (400, 404) and b"PK-secret" not in r.content
