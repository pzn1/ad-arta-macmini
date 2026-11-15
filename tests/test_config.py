from pathlib import Path

import pytest

from ad_arta.config import load_config


def test_token_path_is_relative_to_config(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[tv]\nhost = "10.0.0.5"\n')
    cfg = load_config(str(cfg_file))
    assert cfg.tv.host == "10.0.0.5"
    assert cfg.tv.token_file == str(tmp_path / "tv-token.txt")
    assert cfg.appletv.identifier == ""


def test_invalid_toml_exits_with_message(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("this is not toml")
    with pytest.raises(SystemExit) as exc:
        load_config(str(cfg_file))
    assert "config.toml" in str(exc.value)
    assert "valid TOML" in str(exc.value)


def test_example_config_loads():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(str(root / "config.example.toml"))
    assert cfg.tv.mac


@pytest.mark.parametrize("text", [
    '[tv]\nhost = "10.0.0.5"\n[bridge]\noff_delay = 0\n',
    '[tv]\nhost = "10.0.0.5"\ninput_key_first = true\n',
])
def test_unknown_keys_exit_with_message(tmp_path: Path, text):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(text)
    with pytest.raises(SystemExit, match="unknown"):
        load_config(str(cfg_file))


@pytest.mark.parametrize("text", ['tv = "1.2.3.4"', '[tv]\nhost = "1.2.3.4"\n[appletv.x]\n', 'appletv = 1\n[tv]\nhost = "1.2.3.4"'])
def test_wrong_shape_exits_with_message(tmp_path: Path, text):
    cfg = tmp_path / "config.toml"
    cfg.write_text(text)
    with pytest.raises(SystemExit) as err:
        load_config(str(cfg))
    assert str(cfg) in str(err.value)
