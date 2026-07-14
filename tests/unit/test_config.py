import pytest

from app.config import load_config


def test_loads_temp_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JobBoss2__ApiBaseUrl=https://api.example.com\n"
        "JobBoss2__ClientId=abc123\n"
        "# a comment, ignored\n"
        "\n"
        "DATABASE_URL=postgresql://mes:mes@localhost/mes\n"
    )
    cfg = load_config(env_file)
    assert cfg.jobboss2_api_base_url == "https://api.example.com"
    assert cfg.jobboss2_client_id == "abc123"
    assert cfg.database_url == "postgresql://mes:mes@localhost/mes"
    assert cfg.mes_secret_key is None


def test_env_var_overrides_dotenv_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MES_SECRET_KEY=from-file\n")
    monkeypatch.setenv("MES_SECRET_KEY", "from-real-env")
    cfg = load_config(env_file)
    assert cfg.mes_secret_key == "from-real-env"


def test_missing_var_raises_on_validate(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.env")
    with pytest.raises(RuntimeError):
        cfg.validate(["database_url"])


def test_repr_never_prints_secret_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MES_SECRET_KEY=super-secret-value\n")
    cfg = load_config(env_file)
    assert "super-secret-value" not in repr(cfg)
