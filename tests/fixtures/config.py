"""宿主 config.yaml 的读写助手.

Fixtures:
- host_config: 每次调 .read() 都拉最新;.write(d) 落盘并等 Watchdog 拾起;
  fixture teardown 强制 restore 到测试开始时的快照,防止污染下一个测试。

热重载等待策略:文件写入后 sleep(3s) — spec §5.3 #7 明确 3s 是热重载观察窗口。
"""
import time
import yaml
import pytest

from tests.conftest import HOST_CONFIG_YAML

HOT_RELOAD_WAIT_SEC = 3


class HostConfig:
    def __init__(self, path: str):
        self.path = path
        self._snapshot: str | None = None

    def read(self) -> dict:
        with open(self.path) as f:
            return yaml.safe_load(f) or {}

    def raw(self) -> str:
        with open(self.path) as f:
            return f.read()

    def write(self, new_data: dict, wait_hot_reload: bool = True) -> None:
        """Write config.yaml in-place (truncate + write, NOT atomic rename).

        Docker bind-mount tracks inode; atomic rename creates a new inode
        that the mount doesn't see, breaking Watchdog hot-reload.
        """
        dumped = yaml.safe_dump(new_data, allow_unicode=True, sort_keys=False)
        with open(self.path, "w") as f:
            f.write(dumped)
        if wait_hot_reload:
            time.sleep(HOT_RELOAD_WAIT_SEC)

    def snapshot(self) -> None:
        """Save current file bytes for restore()."""
        self._snapshot = self.raw()

    def restore(self) -> None:
        """Restore snapshot; wait for hot-reload."""
        if self._snapshot is None:
            return
        with open(self.path, "w") as f:
            f.write(self._snapshot)
        time.sleep(HOT_RELOAD_WAIT_SEC)


@pytest.fixture
def host_config():
    """Yield HostConfig with auto-snapshot on entry and auto-restore on teardown."""
    hc = HostConfig(HOST_CONFIG_YAML)
    hc.snapshot()
    try:
        yield hc
    finally:
        hc.restore()
