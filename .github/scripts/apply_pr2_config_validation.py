from pathlib import Path


path = Path(
    "aigateway-core/src/aigateway_core/pipelines/generation/_common/"
    "_config_impl.py"
)
text = path.read_text(encoding="utf-8")
old = '''                except (TypeError, ValueError):
                    msg = (
                        f"generation_optimization.{section_name}.{field_name} = {value!r} "
                        f"类型错误，期望数值类型"
                    )
                    errors.append(msg)

        return errors
'''
new = '''                except (TypeError, ValueError):
                    msg = (
                        f"generation_optimization.{section_name}.{field_name} = {value!r} "
                        f"类型错误，期望数值类型"
                    )
                    errors.append(msg)

        timing = self.draft_workflow
        durations = timing.video_supported_durations_seconds
        valid_durations = (
            isinstance(durations, tuple)
            and bool(durations)
            and all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
                for value in durations
            )
        )
        if not valid_durations:
            errors.append(
                "generation_optimization.draft_workflow."
                "video_supported_durations_seconds 必须是非空的正整数元组"
            )
        elif timing.video_default_duration_seconds not in durations:
            errors.append(
                "generation_optimization.draft_workflow."
                "video_default_duration_seconds 必须属于 "
                "video_supported_durations_seconds"
            )

        if timing.video_default_fps > timing.video_max_fps:
            errors.append(
                "generation_optimization.draft_workflow.video_default_fps "
                "不得大于 video_max_fps"
            )
        if timing.video_min_frames > timing.video_max_frames:
            errors.append(
                "generation_optimization.draft_workflow.video_min_frames "
                "不得大于 video_max_frames"
            )

        if (
            valid_durations
            and timing.video_default_duration_seconds in durations
            and timing.video_default_fps <= timing.video_max_fps
            and timing.video_min_frames <= timing.video_max_frames
        ):
            requested_frames = round(
                timing.video_default_duration_seconds * timing.video_default_fps
            )
            normalized_frames = ((requested_frames - 1 + 3) // 4) * 4 + 1
            if not (
                timing.video_min_frames
                <= normalized_frames
                <= timing.video_max_frames
            ):
                errors.append(
                    "generation_optimization.draft_workflow 默认视频时序归一化后 "
                    "超出 video_min_frames/video_max_frames"
                )

        return errors
'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one validation tail, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
