from pathlib import Path


path = Path(
    "aigateway-core/src/aigateway_core/pipelines/generation/director/"
    "ai_director_plugin.py"
)
text = path.read_text(encoding="utf-8")
old = '''        duration_seconds = self._number_option(
            options,
            "duration_seconds",
            ctx.request.get("duration_seconds", 5.0),
        )
        fps = self._integer_option(
            options,
            "fps",
            ctx.request.get("target_fps", 8),
        )
'''
new = '''        timing = self._config.draft_workflow
        duration_seconds = self._number_option(
            options,
            "duration_seconds",
            ctx.request.get(
                "duration_seconds",
                timing.video_default_duration_seconds,
            ),
        )
        fps = self._integer_option(
            options,
            "fps",
            ctx.request.get("target_fps", timing.video_default_fps),
        )
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one AI director timing default block, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
