import os

from app import config


def _reset(monkeypatch, tmp_path, secret=""):
    monkeypatch.setattr(config, "WEBHOOK_SECRET", secret)
    monkeypatch.setattr(config, "REQUIRE_SECRET", True)
    monkeypatch.setattr(config, "SECRET_SOURCE", "")
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SECRET_FILE", str(tmp_path / "webhook_secret"))


def test_env_secret_wins(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path, secret="from-env")
    config.load_or_create_secret()
    assert (config.WEBHOOK_SECRET, config.SECRET_SOURCE) == ("from-env", "env")
    assert not (tmp_path / "webhook_secret").exists()


def test_generates_then_reuses_secret(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    config.load_or_create_secret()
    first = config.WEBHOOK_SECRET
    assert config.SECRET_SOURCE == "generated" and len(first) == 64
    assert oct(os.stat(tmp_path / "webhook_secret").st_mode & 0o777) == "0o600"

    _reset(monkeypatch, tmp_path)
    config.load_or_create_secret()
    assert (config.WEBHOOK_SECRET, config.SECRET_SOURCE) == (first, "file")


def test_unwritable_config_dir_is_ephemeral(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(config, "CONFIG_DIR", str(blocker))
    monkeypatch.setattr(config, "SECRET_FILE", str(blocker / "webhook_secret"))
    config.load_or_create_secret()
    assert config.SECRET_SOURCE == "ephemeral" and config.WEBHOOK_SECRET
    assert config.validate() == []


def test_webhook_url(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path, secret="s3")
    monkeypatch.setattr(config, "PUBLIC_URL", "https://tv.example.com")
    assert config.webhook_url(True) == "https://tv.example.com/webhook?secret=s3"
    assert config.webhook_url(False) == "https://tv.example.com/webhook?secret=<your secret>"


def test_default_zmq_host_is_docker_gateway():
    if not os.environ.get("ZMQ_HOST"):
        assert config.ZMQ_HOST == "172.17.0.1"
