from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one {label}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def main() -> None:
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


if __name__ == "__main__":
    main()
