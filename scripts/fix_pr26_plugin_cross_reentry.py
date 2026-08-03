"""Prevent cross-thread constructor reentry cycles in PluginRegistry."""
from pathlib import Path

path = Path("aigateway-core/src/aigateway_core/shared/plugin_registry.py")
text = path.read_text(encoding="utf-8")
old = '''                else:
                    completion, owner_thread = active_build
                    # A constructor may inspect the registry. Waiting for its own
                    # in-flight instance would deadlock; omit that incomplete
                    # registration from the nested view instead.
                    if owner_thread == current_thread:
                        return None
                    is_builder = False
'''
new = '''                else:
                    completion, owner_thread = active_build
                    # A constructor may inspect the registry. If this thread is
                    # itself constructing any plugin, waiting for another active
                    # constructor can form an A->B / B->A cycle. Nested registry
                    # views therefore omit all currently in-flight registrations.
                    current_thread_is_building = any(
                        builder_thread == current_thread
                        for _, builder_thread in self._instance_builds.values()
                    )
                    if owner_thread == current_thread or current_thread_is_building:
                        return None
                    is_builder = False
'''
if old not in text:
    raise SystemExit("plugin reentry anchor not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")


test_path = Path("tests/unit/test_merge_readiness_followup.py")
test_text = test_path.read_text(encoding="utf-8")
if "test_concurrent_plugin_constructors_do_not_cross_deadlock" not in test_text:
    test_text += '''


def test_concurrent_plugin_constructors_do_not_cross_deadlock() -> None:
    registry = PluginRegistry()
    a_started = threading.Event()
    b_started = threading.Event()
    snapshots: list[list[Any]] = []
    snapshots_lock = threading.Lock()

    class PluginA:
        def __init__(self) -> None:
            a_started.set()
            assert b_started.wait(timeout=2)
            snapshot = registry.get_all()
            with snapshots_lock:
                snapshots.append(snapshot)

        async def execute(self, ctx: Any) -> Any:
            return ctx

    class PluginB:
        def __init__(self) -> None:
            b_started.set()
            assert a_started.wait(timeout=2)
            snapshot = registry.get_all()
            with snapshots_lock:
                snapshots.append(snapshot)

        async def execute(self, ctx: Any) -> Any:
            return ctx

    registry.register("a", PluginA, priority=0)
    registry.register("b", PluginB, priority=1)

    # Force each registration's first build onto a different thread. Calling the
    # private helper is intentional: get_all() builds in priority order and cannot
    # establish the cross-constructor interleaving deterministically.
    reg_a = registry.get("a")
    reg_b = registry.get("b")
    assert reg_a is not None and reg_b is not None
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(registry._get_or_create_instance, reg_a)
        future_b = executor.submit(registry._get_or_create_instance, reg_b)
        instance_a = future_a.result(timeout=3)
        instance_b = future_b.result(timeout=3)

    assert isinstance(instance_a, PluginA)
    assert isinstance(instance_b, PluginB)
    assert len(snapshots) == 2
    assert all(isinstance(snapshot, list) for snapshot in snapshots)
    final = registry.get_all()
    assert final == [instance_a, instance_b]
'''
    test_path.write_text(test_text, encoding="utf-8")
