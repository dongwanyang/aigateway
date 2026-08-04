from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected one {label}, found {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def main() -> None:
    replace_once(
        Path("aigateway-core/src/aigateway_core/dispatch/dispatcher.py"),
        "body.generation_options.model_dump()",
        "body.generation_options.model_dump(exclude_none=True)",
        "generation options model_dump call",
    )

    plugin_path = Path(
        "aigateway-core/src/aigateway_core/pipelines/generation/draft/"
        "draft_generator_plugin.py"
    )
    old_build = '''            request = self._build_generation_request(ctx)
            self._assert_video_plan_ready(ctx, request)
            backend = str(options.get("backend") or "auto")
'''
    new_build = '''            try:
                request = self._build_generation_request(ctx)
                self._assert_video_plan_ready(ctx, request)
            except ValueError as exc:
                duration_ms = (time.monotonic() - started_at) * 1000.0
                ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                    "draft_generator"
                ] = {
                    "applicable": False,
                    "reason": "invalid_generation_options",
                    "local_error": str(exc),
                    "duration_ms": duration_ms,
                }
                ctx.add_plugin_trace(
                    "draft_generator",
                    duration_ms,
                    "failed",
                    payload={"reason": "invalid_generation_options"},
                )
                ctx.should_stop = True
                return ctx
            backend = str(options.get("backend") or "auto")
'''
    replace_once(plugin_path, old_build, new_build, "generation request build block")

    old_outer = '''        except ValueError as exc:
            duration_ms = (time.monotonic() - started_at) * 1000.0
            ctx.extra.setdefault(NS_GENERATION_OPTIMIZATION, {})[
                "draft_generator"
            ] = {
                "applicable": False,
                "reason": "invalid_generation_options",
                "local_error": str(exc),
                "duration_ms": duration_ms,
            }
            ctx.add_plugin_trace(
                "draft_generator",
                duration_ms,
                "failed",
                payload={"reason": "invalid_generation_options"},
            )
            ctx.should_stop = True
'''
    replace_once(plugin_path, old_outer, "", "broad ValueError handler")


if __name__ == "__main__":
    main()
