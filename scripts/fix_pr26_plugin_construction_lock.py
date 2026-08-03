"""Replace global-lock plugin construction with per-registration coordination."""
from pathlib import Path

path = Path("aigateway-core/src/aigateway_core/shared/plugin_registry.py")
text = path.read_text(encoding="utf-8")
old_init = '''        self._registrations: dict[str, PluginRegistration] = {}
        self._instances: dict[str, Any] = {}
        self._lock = threading.Lock()
'''
new_init = '''        self._registrations: dict[str, PluginRegistration] = {}
        self._instances: dict[str, Any] = {}
        # id(registration) -> (completion event, constructing thread id).
        # Constructors run outside the global registry lock so one slow plugin
        # does not block unrelated reads, registrations or unregistrations.
        self._instance_builds: dict[int, tuple[threading.Event, int]] = {}
        self._lock = threading.Lock()
'''
if old_init not in text:
    raise SystemExit("registry init anchor not found")
text = text.replace(old_init, new_init, 1)

start = text.index("    def _get_or_create_instance(")
end = text.index("\n    def get_all(", start)
new_method = '''    def _get_or_create_instance(self, reg: PluginRegistration) -> Any | None:
        """Return one runtime instance for the currently live registration.

        First construction is coordinated per registration. The expensive and
        potentially re-entrant constructor runs outside ``self._lock``; other
        threads wait for that registration only. If the registration is removed
        or replaced while construction is in flight, the obsolete candidate is
        discarded and never published under the new registration.
        """
        build_key = id(reg)
        current_thread = threading.get_ident()

        while True:
            with self._lock:
                if self._registrations.get(reg.name) is not reg:
                    return None
                cached = self._instances.get(reg.name)
                if cached is not None:
                    return cached
                active_build = self._instance_builds.get(build_key)
                if active_build is None:
                    completion = threading.Event()
                    self._instance_builds[build_key] = (
                        completion,
                        current_thread,
                    )
                    is_builder = True
                else:
                    completion, owner_thread = active_build
                    # A constructor may inspect the registry. Waiting for its own
                    # in-flight instance would deadlock; omit that incomplete
                    # registration from the nested view instead.
                    if owner_thread == current_thread:
                        return None
                    is_builder = False
            if is_builder:
                break
            completion.wait()

        candidate: Any | None = None
        construction_error: BaseException | None = None
        try:
            candidate = reg.plugin_class(**reg.config)
        except TypeError as exc:
            logger.warning(
                "插件 '%s' 实例化失败（配置参数不匹配）: %s",
                reg.name,
                exc,
            )
        except BaseException as exc:  # preserve constructor semantics after wakeup
            construction_error = exc

        with self._lock:
            if (
                candidate is not None
                and self._registrations.get(reg.name) is reg
            ):
                published = self._instances.setdefault(reg.name, candidate)
            else:
                published = None
            active_build = self._instance_builds.pop(build_key, None)
            if active_build is not None:
                active_build[0].set()

        if construction_error is not None:
            raise construction_error
        return published
'''
text = text[:start] + new_method + text[end:]
path.write_text(text, encoding="utf-8")


test_path = Path("tests/unit/test_merge_readiness_followup.py")
test_text = test_path.read_text(encoding="utf-8")
if "test_plugin_constructor_can_read_registry_without_deadlock" not in test_text:
    test_text += '''


def test_plugin_constructor_can_read_registry_without_deadlock() -> None:
    registry = PluginRegistry()
    nested_snapshots: list[list[Any]] = []

    class ReentrantPlugin:
        def __init__(self) -> None:
            nested_snapshots.append(registry.get_all())

        async def execute(self, ctx: Any) -> Any:
            return ctx

    registry.register("reentrant", ReentrantPlugin)
    instances = registry.get_all()

    assert len(instances) == 1
    assert nested_snapshots == [[]]
    assert registry.get_all()[0] is instances[0]


def test_obsolete_inflight_plugin_is_never_published() -> None:
    registry = PluginRegistry()
    old_started = threading.Event()
    release_old = threading.Event()

    class OldPlugin:
        def __init__(self) -> None:
            old_started.set()
            assert release_old.wait(timeout=2)

        async def execute(self, ctx: Any) -> Any:
            return ctx

    class NewPlugin:
        async def execute(self, ctx: Any) -> Any:
            return ctx

    registry.register("replaceable", OldPlugin)
    with ThreadPoolExecutor(max_workers=2) as executor:
        old_future = executor.submit(registry.get_all)
        assert old_started.wait(timeout=2)
        registry.unregister("replaceable")
        registry.register("replaceable", NewPlugin)
        new_instance = registry.get_all()[0]
        release_old.set()
        assert old_future.result(timeout=2) == []

    assert isinstance(new_instance, NewPlugin)
    assert registry.get_all()[0] is new_instance
'''
    test_path.write_text(test_text, encoding="utf-8")
