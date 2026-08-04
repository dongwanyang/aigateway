from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one {label}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def patch_request_boundaries() -> None:
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


def patch_config_contract() -> None:
    config_impl = Path(
        "aigateway-core/src/aigateway_core/pipelines/generation/_common/"
        "_config_impl.py"
    )
    replace_once(
        config_impl,
        '''        target_fps_range: 允许的目标帧率范围 (默认: (24, 120))
        upscale_algorithm: 放大算法名称 (默认: "real-esrgan")
''',
        '''        target_fps_range: 允许的目标帧率范围 (默认: (24, 120))
        video_default_duration_seconds: 默认视频时长/秒 (默认: 5)
        video_supported_durations_seconds: 允许的视频时长档位 (默认: (3, 5, 8))
        video_default_fps: 默认视频帧率 (默认: 8)
        video_max_fps: 最大视频帧率 (默认: 60)
        video_min_frames: 视频最小帧数 (默认: 1)
        video_max_frames: 视频最大帧数 (默认: 481)
        upscale_algorithm: 放大算法名称 (默认: "real-esrgan")
''',
        "draft workflow timing documentation",
    )
    replace_once(
        config_impl,
        '''    target_fps: int = 60
    target_fps_range: tuple[int, int] = (24, 120)
    upscale_algorithm: str = "real-esrgan"
''',
        '''    target_fps: int = 60
    target_fps_range: tuple[int, int] = (24, 120)
    video_default_duration_seconds: int = 5
    video_supported_durations_seconds: tuple[int, ...] = (3, 5, 8)
    video_default_fps: int = 8
    video_max_fps: int = 60
    video_min_frames: int = 1
    video_max_frames: int = 481
    upscale_algorithm: str = "real-esrgan"
''',
        "draft workflow timing fields",
    )
    replace_once(
        config_impl,
        '''        "preview_video_fps": (1, 30),
        "target_fps": (24, 120),
''',
        '''        "preview_video_fps": (1, 30),
        "target_fps": (24, 120),
        "video_default_duration_seconds": (1, 300),
        "video_default_fps": (1, 60),
        "video_max_fps": (1, 60),
        "video_min_frames": (1, 10000),
        "video_max_frames": (1, 10000),
''',
        "draft workflow timing validation rules",
    )

    plugin_path = Path(
        "aigateway-core/src/aigateway_core/pipelines/generation/draft/"
        "draft_generator_plugin.py"
    )
    replace_once(
        plugin_path,
        '''NS_GENERATION_OPTIMIZATION = "generation_optimization"
_SUPPORTED_VIDEO_DURATIONS = (3.0, 5.0, 8.0)
_MAX_VIDEO_FPS = 60
_MAX_VIDEO_FRAMES = 481
''',
        '''NS_GENERATION_OPTIMIZATION = "generation_optimization"
''',
        "hard-coded video timing constants",
    )
    replace_once(
        plugin_path,
        '''                        options.get(
                            "duration_seconds",
                            ctx.request.get("duration_seconds", 5.0),
                        ),
                        options.get("fps", ctx.request.get("target_fps", 8)),
''',
        '''                        options.get(
                            "duration_seconds",
                            ctx.request.get(
                                "duration_seconds",
                                self._config.draft_workflow.video_default_duration_seconds,
                            ),
                        ),
                        options.get(
                            "fps",
                            ctx.request.get(
                                "target_fps",
                                self._config.draft_workflow.video_default_fps,
                            ),
                        ),
''',
        "video timing defaults",
    )
    replace_once(
        plugin_path,
        '''                    options.get(
                        "duration_seconds",
                        ctx.request.get("duration_seconds", 5.0),
                    ),
                    "duration_seconds",
                )
                target_fps = self._positive_integer(
                    options.get("fps", ctx.request.get("target_fps", 8)),
                    "fps",
                )
''',
        '''                    options.get(
                        "duration_seconds",
                        ctx.request.get(
                            "duration_seconds",
                            self._config.draft_workflow.video_default_duration_seconds,
                        ),
                    ),
                    "duration_seconds",
                )
                target_fps = self._positive_integer(
                    options.get(
                        "fps",
                        ctx.request.get(
                            "target_fps",
                            self._config.draft_workflow.video_default_fps,
                        ),
                    ),
                    "fps",
                )
''',
        "non-video generation timing defaults",
    )
    replace_once(
        plugin_path,
        '''    @classmethod
    def _normalize_video_timing(
        cls,
        duration_value: Any,
        fps_value: Any,
    ) -> tuple[float, int, int]:
        duration = cls._finite_positive_number(
            duration_value, "duration_seconds"
        )
        if not any(
            math.isclose(duration, allowed)
            for allowed in _SUPPORTED_VIDEO_DURATIONS
        ):
            raise ValueError("video_duration_unsupported")
        fps = cls._positive_integer(fps_value, "fps")
        if fps > _MAX_VIDEO_FPS:
            raise ValueError("fps_out_of_range")
        requested_count = round(duration * fps)
        normalized_count = ((requested_count - 1 + 3) // 4) * 4 + 1
        if normalized_count <= 0 or normalized_count > _MAX_VIDEO_FRAMES:
            raise ValueError("frame_count_out_of_range")
        return duration, fps, normalized_count
''',
        '''    def _normalize_video_timing(
        self,
        duration_value: Any,
        fps_value: Any,
    ) -> tuple[float, int, int]:
        timing = self._config.draft_workflow
        duration = self._finite_positive_number(
            duration_value, "duration_seconds"
        )
        supported_durations = tuple(
            self._finite_positive_number(value, "video_supported_duration")
            for value in timing.video_supported_durations_seconds
        )
        if not supported_durations or not any(
            math.isclose(duration, allowed)
            for allowed in supported_durations
        ):
            raise ValueError("video_duration_unsupported")

        fps = self._positive_integer(fps_value, "fps")
        max_fps = self._positive_integer(timing.video_max_fps, "video_max_fps")
        if fps > max_fps:
            raise ValueError("fps_out_of_range")

        min_frames = self._positive_integer(
            timing.video_min_frames, "video_min_frames"
        )
        max_frames = self._positive_integer(
            timing.video_max_frames, "video_max_frames"
        )
        if min_frames > max_frames:
            raise ValueError("video_frame_range_invalid")

        requested_count = max(min_frames, round(duration * fps))
        if requested_count > max_frames:
            raise ValueError("frame_count_out_of_range")
        normalized_count = ((requested_count - 1 + 3) // 4) * 4 + 1
        if normalized_count < min_frames or normalized_count > max_frames:
            raise ValueError("frame_count_out_of_range")
        return duration, fps, normalized_count
''',
        "video timing normalizer",
    )

    replace_once(
        Path("config.yaml"),
        '''  draft_workflow:
    store_dir: /app/data/drafts
    retention_period_hours: 24
''',
        '''  draft_workflow:
    store_dir: /app/data/drafts
    retention_period_hours: 24
    video_default_duration_seconds: 5
    video_supported_durations_seconds: [3, 5, 8]
    video_default_fps: 8
    video_max_fps: 60
    video_min_frames: 1
    video_max_frames: 481
''',
        "runtime video timing configuration",
    )
    replace_once(
        Path("config.yaml.template"),
        '''  draft_workflow:             # Draft-to-HiRes 草图工作流
    enabled: true             # 是否启用本地草图生成工作流
    store_dir: /app/data/drafts       # 草稿、预览和确认结果的持久化根目录
    retention_period_hours: 24        # 草稿会话保留时长（小时）
''',
        '''  draft_workflow:             # Draft-to-HiRes 草图工作流
    enabled: true             # 是否启用本地草图生成工作流
    store_dir: /app/data/drafts       # 草稿、预览和确认结果的持久化根目录
    retention_period_hours: 24        # 草稿会话保留时长（小时）
    video_default_duration_seconds: 5 # 默认视频时长（秒）
    video_supported_durations_seconds: [3, 5, 8] # 允许的时长档位
    video_default_fps: 8      # 默认视频帧率
    video_max_fps: 60         # 服务端允许的最大视频帧率
    video_min_frames: 1       # Wan 工作流最小帧数边界
    video_max_frames: 481     # Wan 工作流最大帧数边界
''',
        "template video timing configuration",
    )


def main() -> None:
    patch_request_boundaries()
    patch_config_contract()


if __name__ == "__main__":
    main()
