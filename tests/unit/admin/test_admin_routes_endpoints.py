"""Behavioural tests for the stateful admin API endpoints.

These tests call the endpoint functions with production-shaped state objects.
Every assertion checks either an externally visible response/error or the
exact persistence operation that the endpoint is responsible for.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from aigateway_api import admin_routes as routes
from fastapi import HTTPException


def test_embedding_cache_and_image_signature_detection_are_stable():
    sentinel = object()
    routes._embedding_model_cache.clear()
    assert routes._get_embedding_model() is None
    routes._set_embedding_model(sentinel)
    assert routes._get_embedding_model() is sentinel
    assert routes._detect_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert routes._detect_image_mime(b"\xff\xd8\xffrest") == "image/jpeg"
    assert routes._detect_image_mime(b"RIFF0000WEBPdata") == "image/webp"
    assert routes._detect_image_mime(b"not-an-image") == "image/png"


def test_state_backed_admin_helpers_read_live_services_and_alert_threshold():
    key_store = object()
    metrics = object()
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: (
        {"budget_alert_threshold": 0.75} if key == "auth" else default
    )
    state = _state(
        key_store=key_store,
        metrics_collector=metrics,
        config_manager=config,
    )
    with patch("aigateway_api.app_state.get_state", return_value=state):
        assert routes._get_keystore_and_metrics(_request()) == (
            key_store,
            metrics,
        )
        assert routes._get_budget_alert_threshold() == 0.75


def _request(*, json_body=None):
    request = MagicMock()
    request.json = AsyncMock(return_value=json_body)
    return request


def _state(**overrides):
    values = {
        "config_manager": None,
        "key_store": None,
        "metrics_collector": None,
        "group_store": None,
        "redis_manager": None,
        "task_tracker": None,
        "litellm_bridge": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _active_key(**overrides):
    value = {
        "key_id": "key_one",
        "key_hash": "hash-one",
        "key_prefix": "gw-test",
        "user_id": "alice",
        "group_id": "grp-team",
        "status": "active",
        "daily_tokens_limit": "1000",
        "daily_tokens_used": "800",
        "monthly_cost_limit": "20",
        "monthly_cost_used": "18",
        "rate_limit_rpm": "60",
        "rate_limit_tpm": "1000",
        "rpm_window_count": "3",
        "tpm_window_count": "120",
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_api_key_lifecycle_endpoints_persist_expected_changes():
    store = MagicMock()
    store.conn.fetchall.return_value = [_active_key()]
    store.ensure_seeded = AsyncMock()
    store.get_group = AsyncMock(return_value={"name": "Team"})
    store.revoke = AsyncMock(return_value=True)
    store.rotate = AsyncMock(return_value={"id": "key_two", "key": "gw-secret"})
    store._find_key_hashes_by_id = AsyncMock(return_value=["hash-one"])
    store.get_api_key = AsyncMock(return_value=_active_key())
    store.set_api_key = AsyncMock()
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: (
        {"api_keys": [{"key": "seed"}]} if key == "auth" else default
    )
    state = _state(key_store=store, config_manager=config)

    with (
        patch.object(routes, "_get_keystore_and_metrics", return_value=(store, None)),
        patch("aigateway_api.app_state.get_state", return_value=state),
    ):
        listed = await routes.list_api_keys(_request(), page=1, page_size=10, _auth={})
        deleted = await routes.delete_api_key(_request(), "key_one", _auth={})
        rotated = await routes.rotate_api_key(
            _request(), "key_one", routes.RotateApiKeyRequest(expires_at="2030-01-01"), _auth={}
        )
        updated = await routes.update_api_key_quota(
            _request(),
            "key_one",
            routes.UpdateQuotaRequest(
                daily_tokens=2000,
                monthly_cost=30,
                rate_limit_rpm=70,
                rate_limit_tpm=2000,
            ),
            _auth={},
        )

    assert listed["data"]["items"][0]["group_name"] == "Team"
    assert listed["data"]["pagination"] == {"page": 1, "pageSize": 10, "total": 1}
    store.ensure_seeded.assert_awaited_once_with([{"key": "seed"}])
    assert deleted["data"]["status"] == "revoked"
    store.revoke.assert_awaited_once_with("key_one")
    assert rotated["data"]["key"] == "gw-secret"
    assert "shown only once" in rotated["data"]["warning"]
    store.rotate.assert_awaited_once_with("key_one", expires_at="2030-01-01")
    store.set_api_key.assert_awaited_once_with(
        "hash-one",
        {
            "daily_tokens_limit": "2000",
            "monthly_cost_limit": "30.0",
            "rate_limit_rpm": "70",
            "rate_limit_tpm": "2000",
        },
    )
    assert updated["data"]["user_id"] == "alice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        ("delete_bad_id", 400),
        ("delete_missing", 404),
        ("rotate_missing", 404),
        ("update_bad_id", 400),
        ("update_missing_hash", 404),
        ("update_missing_row", 404),
        ("update_empty", 400),
    ],
)
async def test_api_key_lifecycle_rejects_invalid_state(operation, expected_status):
    store = MagicMock()
    store.revoke = AsyncMock(return_value=False)
    store.rotate = AsyncMock(side_effect=ValueError("missing"))
    store._find_key_hashes_by_id = AsyncMock(
        return_value=[] if operation == "update_missing_hash" else ["hash-one"]
    )
    store.get_api_key = AsyncMock(
        return_value=None if operation == "update_missing_row" else _active_key()
    )
    with patch.object(routes, "_get_keystore_and_metrics", return_value=(store, None)):
        with pytest.raises(HTTPException) as caught:
            if operation == "delete_bad_id":
                await routes.delete_api_key(_request(), "bad", _auth={})
            elif operation == "delete_missing":
                await routes.delete_api_key(_request(), "key_missing", _auth={})
            elif operation == "rotate_missing":
                await routes.rotate_api_key(
                    _request(), "key_missing", routes.RotateApiKeyRequest(), _auth={}
                )
            elif operation == "update_bad_id":
                await routes.update_api_key_quota(
                    _request(), "bad", routes.UpdateQuotaRequest(daily_tokens=1), _auth={}
                )
            elif operation in {"update_missing_hash", "update_missing_row"}:
                await routes.update_api_key_quota(
                    _request(), "key_missing", routes.UpdateQuotaRequest(daily_tokens=1), _auth={}
                )
            else:
                await routes.update_api_key_quota(
                    _request(), "key_one", routes.UpdateQuotaRequest(), _auth={}
                )
    assert caught.value.status_code == expected_status


@pytest.mark.asyncio
async def test_get_quota_reports_real_limits_alerts_and_reset_times():
    store = MagicMock()
    store._find_key_hashes_by_id = AsyncMock(return_value=["hash-one"])
    store.get_api_key = AsyncMock(return_value=_active_key())
    with (
        patch.object(routes, "_get_keystore_and_metrics", return_value=(store, None)),
        patch.object(routes, "_get_auth_defaults", return_value={
            "daily_tokens": 1000,
            "monthly_cost": 20,
            "rate_limit_rpm": 60,
            "rate_limit_tpm": 1000,
        }),
        patch.object(routes, "_get_budget_alert_threshold", return_value=0.8),
    ):
        result = await routes.get_quota(_request(), "key_one", _auth={})

    data = result["data"]
    assert data["quotas"]["daily_tokens"]["used"] == 800
    assert data["quotas"]["monthly_cost"]["used"] == 18
    assert data["quotas"]["daily_tokens"]["reset_at"].endswith("Z")
    assert {alert["message"] for alert in data["alerts"]} == {
        "Daily token usage has reached 80%",
        "Monthly cost usage has reached 90%",
    }


@pytest.mark.asyncio
async def test_metrics_json_combines_registry_database_and_circuit_breakers():
    rows = [
        {"daily_tokens_used": "10", "monthly_cost_used": "1.5"},
        {"daily_tokens_used": "20", "monthly_cost_used": "2.5"},
    ]
    store = MagicMock()
    store.conn.fetchall.return_value = rows
    metrics = MagicMock()
    metrics._registry = object()
    metrics.get_uptime_seconds.return_value = 42
    bridge = MagicMock()
    bridge.get_cooldown_status.return_value = {"openai": "closed"}
    state = _state(key_store=store, metrics_collector=metrics, litellm_bridge=bridge)

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch("prometheus_client.generate_latest", return_value=(
            b'# HELP ignored ignored\nrequests_total{model="gpt"} 3.0\nqueue_depth 2\n'
        )),
    ):
        result = await routes.get_metrics_json(_request(), _auth={})

    assert result["data"]["prometheus"]["requests_total"] == {
        "labels": {"model": "gpt"},
        "value": 3.0,
    }
    assert result["data"]["keys"] == {
        "total_keys": 2,
        "total_daily_tokens_used": 30,
        "total_monthly_cost_used": 4.0,
    }
    assert result["data"]["circuit_breakers"] == {"openai": "closed"}
    assert result["data"]["uptime_seconds"] == 42


@pytest.mark.asyncio
async def test_request_logs_filter_and_pagination_use_persisted_entries():
    entries = [
        {
            "request_id": "keep",
            "trace_id": "trace-1",
            "user_id": "alice",
            "model": "gpt",
            "status": 200,
            "cache_hit": True,
        },
        {
            "request_id": "drop",
            "user_id": "bob",
            "model": "other",
            "status": 500,
            "cache_hit": False,
        },
    ]
    redis = MagicMock()
    redis.zcard = AsyncMock(return_value=2)
    redis.zrevrange = AsyncMock(
        return_value=[(json.dumps(item).encode(), index) for index, item in enumerate(entries)]
    )
    state = _state(redis_manager=SimpleNamespace(redis=redis))

    with patch("aigateway_api.app_state.get_state", return_value=state):
        filtered = await routes.get_request_logs(
            _request(),
            page=1,
            page_size=20,
            user_id="alice",
            model="gpt",
            status="200",
            cache_only=True,
            _auth={},
        )
        unfiltered = await routes.get_request_logs(
            _request(),
            page=1,
            page_size=20,
            user_id=None,
            model=None,
            status=None,
            cache_only=False,
            _auth={},
        )

    assert [item["request_id"] for item in filtered["data"]["items"]] == ["keep"]
    assert filtered["data"]["pagination"]["total"] == 1
    assert [item["request_id"] for item in unfiltered["data"]["items"]] == ["keep", "drop"]
    assert redis.zrevrange.await_args_list[1].args[1:3] == (0, 19)


@pytest.mark.asyncio
async def test_trace_prefers_hash_and_falls_back_to_request_log():
    redis = MagicMock()
    redis.hget = AsyncMock(return_value=json.dumps({
        "wall_start": 123,
        "events": [
            {"kind": "stage", "stage": "route", "status": "ok"},
            {"kind": "plugin", "stage": "cache", "duration_ms": 2, "status": "hit"},
        ],
    }).encode())
    redis.zrevrange = AsyncMock()
    state = _state(redis_manager=SimpleNamespace(redis=redis))

    with patch("aigateway_api.app_state.get_state", return_value=state):
        current = await routes.get_trace_detail(_request(), "trace-1", _auth={})
        redis.hget.return_value = None
        redis.zrevrange.return_value = [
            (json.dumps({
                "trace_id": "trace-2",
                "request_id": "req-2",
                "user_id": "alice",
                "model": "gpt",
                "status": 200,
                "plugin_trace": [{"plugin_name": "router"}],
            }).encode(), 1),
            (json.dumps({"trace_id": "other"}).encode(), 0),
        ]
        legacy = await routes.get_trace_detail(_request(), "trace-2", _auth={})

    assert current["data"]["plugin_trace"] == [
        {"plugin_name": "cache", "duration_ms": 2, "status": "hit"}
    ]
    assert current["data"]["meta"] == {"wall_start": 123}
    assert legacy["data"]["request_id"] == "req-2"
    assert legacy["data"]["events"] == []


@pytest.mark.asyncio
async def test_log_delete_endpoints_remove_exact_members():
    valid = json.dumps({"request_id": "req-1"}).encode()
    redis = MagicMock()
    redis.delete = AsyncMock(return_value=1)
    redis.zrevrange = AsyncMock(return_value=[
        (b"not-json", 2),
        (valid, 1),
        (json.dumps({"request_id": "other"}), 0),
    ])
    pipeline = MagicMock()
    pipeline.zrem = MagicMock()
    pipeline.execute = AsyncMock(return_value=[1])
    redis.pipeline.return_value = pipeline
    state = _state(redis_manager=SimpleNamespace(redis=redis))

    with patch("aigateway_api.app_state.get_state", return_value=state):
        deleted_all = await routes.delete_all_logs(_request(), _auth={})
        deleted_some = await routes.batch_delete_logs(
            routes.BatchDeleteLogsRequest(request_ids=["req-1"]),
            _request(),
            _auth={},
        )

    assert deleted_all["data"]["deleted"] is True
    assert deleted_some["data"] == {"deleted": 1, "requested": 1}
    pipeline.zrem.assert_called_once_with("aigateway:logs:requests", valid)
    pipeline.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_crud_and_key_assignment_call_real_store_contracts():
    groups = MagicMock()
    groups.list_groups = AsyncMock(return_value=[{"group_id": "grp-team", "name": "Team"}])
    groups.get_group_detail = AsyncMock(return_value={"group_id": "grp-team", "members": []})
    groups.create_group = AsyncMock(return_value={"group_id": "grp-new"})
    groups.update_group = AsyncMock(return_value={"group_id": "grp-team", "status": "suspended"})
    groups.delete_group = AsyncMock(return_value=True)
    store = MagicMock()
    store._find_key_hashes_by_id = AsyncMock(return_value=["hash-one"])
    store.get_group = AsyncMock(return_value={"group_id": "grp-team"})
    store.assign_key_to_group = AsyncMock()
    store.set_api_key = AsyncMock()
    state = _state(group_store=groups)

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch.object(routes, "_get_keystore_and_metrics", return_value=(store, None)),
    ):
        listed = await routes.list_groups(_request(), _auth={})
        detail = await routes.get_group(_request(), "grp-team", _auth={})
        created = await routes.create_group(
            _request(),
            routes.CreateGroupRequest(
                name="New",
                daily_tokens=100,
                monthly_cost=5,
                rate_limit_rpm=10,
                rate_limit_tpm=1000,
            ),
            _auth={},
        )
        updated = await routes.update_group(
            _request(),
            "grp-team",
            routes.UpdateGroupRequest(daily_tokens=200, status="suspended"),
            _auth={},
        )
        deleted = await routes.delete_group(_request(), "grp-team", _auth={})
        assigned = await routes.assign_key_to_group(
            _request(),
            "key_one",
            routes.AssignKeyGroupRequest(group_id="grp-team", cache_scope="private"),
            _auth={},
        )

    assert listed["data"]["total"] == 1
    assert detail["data"]["members"] == []
    assert created["data"]["group_id"] == "grp-new"
    groups.create_group.assert_awaited_once_with(
        name="New",
        quotas={
            "daily_tokens": 100,
            "monthly_cost": 5.0,
            "rate_limit_rpm": 10,
            "rate_limit_tpm": 1000,
        },
    )
    assert updated["data"]["status"] == "suspended"
    groups.update_group.assert_awaited_once_with(
        group_id="grp-team", quotas={"daily_tokens": 200}, status="suspended"
    )
    assert deleted == {"message": "deleted"}
    assert assigned["data"]["group_id"] == "grp-team"
    store.assign_key_to_group.assert_awaited_once_with("hash-one", "grp-team")
    store.set_api_key.assert_awaited_once_with("hash-one", {"cache_scope": "private"})


@pytest.mark.asyncio
async def test_cost_and_task_endpoints_query_persistent_services():
    store = MagicMock()
    store.query_ledger = AsyncMock(return_value=[{"request_id": "r1", "cost_usd": 1.25}])
    store.prune_ledger = AsyncMock()
    store.ledger_summary = AsyncMock(return_value={"total_cost": 3.5})
    tracker = MagicMock()
    tracker.list_active = AsyncMock(return_value=[{"task_id": "video-1"}])
    state = _state(key_store=store, task_tracker=tracker)

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch.object(routes, "_LAST_LEDGER_PRUNE_TS", 0.0),
    ):
        ledger = await routes.get_cost_ledger(
            limit=20,
            offset=5,
            start=100,
            end=200,
            user_id="alice",
            group_id="grp-team",
            model="gpt",
            _auth={},
        )
        summary = await routes.get_cost_summary(
            days=2, start=None, end=200_000, _auth={}
        )
        tasks = await routes.list_chat_tasks(task_type="video", _auth={})

    assert ledger["rows"][0]["cost_usd"] == 1.25
    store.query_ledger.assert_awaited_once_with(
        limit=20,
        offset=5,
        start_unix=100,
        end_unix=200,
        user_id="alice",
        group_id="grp-team",
        model="gpt",
    )
    store.prune_ledger.assert_awaited_once_with(keep_days=90)
    store.ledger_summary.assert_awaited_once_with(
        start_unix=200_000 - 2 * 86400,
        end_unix=200_000,
    )
    assert summary == {"total_cost": 3.5}
    assert tasks == {"tasks": [{"task_id": "video-1"}]}
    tracker.list_active.assert_awaited_once_with(task_type="video")


def _config_manager(path, initial):
    manager = MagicMock()
    manager.config_path = str(path)
    manager._config = initial.copy()
    manager.get.side_effect = lambda key, default=None: manager._config.get(key, default)

    def set_value(key, value):
        manager._config[key] = value

    manager.set.side_effect = set_value
    return manager


@pytest.mark.asyncio
async def test_plugin_config_read_and_regular_toggle_persist_and_reload(tmp_path):
    config_path = tmp_path / "config.yaml"
    initial = {
        "plugins": [
            {"name": "cache", "enabled": True, "config": {"ttl": 10}},
            {"name": "prompt_compress", "enabled": True},
        ],
        "unrelated": {"preserved": True},
    }
    config_path.write_text(yaml.safe_dump(initial), encoding="utf-8")
    manager = _config_manager(config_path, initial)
    cache_reg = SimpleNamespace(
        enabled=True,
        depends_on=["router"],
        config={"runtime": object(), "limit": 3},
        pipeline_kind="understanding",
        priority=20,
    )
    generated_reg = SimpleNamespace(
        enabled=False,
        depends_on=[],
        config={"strategy": object(), "retries": 2},
        pipeline_kind="generation",
        priority=5,
    )
    registry = SimpleNamespace(_registrations={
        "cache": cache_reg,
        "draft_generator": generated_reg,
    })
    state = _state(config_manager=manager)
    state.plugin_registry = registry
    debug = SimpleNamespace(per_plugin={"cache": True, "draft_generator": False})

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch("aigateway_core.shared.debug_config.get_debug_config", return_value=debug),
    ):
        listed = await routes.get_plugins_config(_request(), _auth={})
        changed = await routes.update_plugins_config(
            _request(json_body={"name": "cache", "enabled": False}), _auth={}
        )

    by_name = {item["name"]: item for item in listed["data"]["plugins"]}
    assert by_name["cache"]["pipeline_kind"] == "understanding"
    assert by_name["cache"]["debug"] is True
    assert by_name["prompt_compress"]["debug"] is None
    assert by_name["draft_generator"]["config"]["retries"] == 2
    assert by_name["draft_generator"]["config"]["strategy"] == "<non-serializable: object>"
    assert changed["data"] == {"name": "cache", "enabled": False}
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["plugins"][0]["enabled"] is False
    assert persisted["unrelated"] == {"preserved": True}
    manager._set_nested.assert_called_once()
    manager.atomic_swap.assert_called_once()


@pytest.mark.asyncio
async def test_generation_plugin_toggle_updates_nested_gate(tmp_path):
    config_path = tmp_path / "config.yaml"
    initial = {"plugins": [], "unrelated": 7}
    config_path.write_text(yaml.safe_dump(initial), encoding="utf-8")
    manager = _config_manager(config_path, initial)
    state = _state(config_manager=manager)

    with patch("aigateway_api.app_state.get_state", return_value=state):
        result = await routes.update_plugins_config(
            _request(json_body={"name": "token_compressor", "enabled": True}),
            _auth={},
        )

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result["data"] == {"name": "token_compressor", "enabled": True}
    assert persisted["generation_optimization"] == {
        "enabled": True,
        "token_compressor": {"enabled": True},
    }
    assert persisted["unrelated"] == 7
    assert manager._set_nested.call_args_list[0].args[1:] == (
        "generation_optimization.token_compressor.enabled",
        True,
    )
    assert manager._set_nested.call_args_list[1].args[1:] == (
        "generation_optimization.enabled",
        True,
    )


@pytest.mark.asyncio
async def test_debug_and_global_config_endpoints_round_trip_real_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    initial = {
        "hot_reload": False,
        "debug_mode": False,
        "debug": {"entry": False, "plugins": {"per_plugin": {"cache": False}}},
        "observability": {"log_level": "warning"},
        "providers": {"openai": {"api_key": "secret-value-123456"}},
    }
    config_path.write_text(yaml.safe_dump(initial), encoding="utf-8")
    manager = _config_manager(config_path, initial)
    state = _state(config_manager=manager)

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch("aigateway_core.shared.logger.setup_logging") as setup_logging,
    ):
        debug_change = await routes.set_plugin_debug(
            "cache", _request(json_body={"enabled": True}), _auth={}
        )
        global_before = await routes.get_global_config(_request(), _auth={})
        global_after = await routes.update_global_config(
            _request(json_body={
                "hot_reload": True,
                "debug_mode": True,
                "debug": {"entry": True, "plugins_enabled": True},
            }),
            _auth={},
        )

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert debug_change["data"] == {"plugin": "cache", "debug": True}
    assert global_before["data"]["debug"]["plugins"]["per_plugin"]["cache"] is True
    assert global_after["data"]["hot_reload"] is True
    assert global_after["data"]["debug"]["plugins"] == {"enabled": True}
    assert persisted["debug"]["plugins_enabled"] is True
    assert persisted["debug"]["plugins"]["enabled"] is True
    assert persisted["providers"]["openai"]["api_key"] == "secret-value-123456"
    manager.start_watching.assert_called_once()
    setup_logging.assert_called_once_with(log_level="DEBUG")


@pytest.mark.asyncio
async def test_full_config_masks_secret_and_preserves_it_on_update(tmp_path):
    config_path = tmp_path / "config.yaml"
    initial = {
        "server": {"port": 8000},
        "providers": {
            "openai": {"api_key": "1234567890-real-secret", "base_url": "old"},
            "env": {"api_key": "${OPENAI_API_KEY}"},
        },
        "auth": {"api_keys": ["must-not-change"]},
    }
    config_path.write_text(yaml.safe_dump(initial), encoding="utf-8")
    manager = _config_manager(config_path, initial)
    state = _state(config_manager=manager)

    with patch("aigateway_api.app_state.get_state", return_value=state):
        visible = await routes.get_full_config(_request(), _auth={})
        changed = await routes.update_full_config(
            _request(json_body={
                "server": {"port": 9000},
                "providers": {
                    "openai": {"api_key": "12345678***", "base_url": "new"},
                },
                "auth": {"api_keys": ["attacker-value"]},
            }),
            _auth={},
        )

    assert visible["data"]["providers"]["openai"]["api_key"] == "12345678***"
    assert visible["data"]["providers"]["env"]["api_key"] == "${OPENAI_API_KEY}"
    assert changed["data"]["updated"] is True
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["server"]["port"] == 9000
    assert persisted["providers"]["openai"] == {
        "api_key": "1234567890-real-secret",
        "base_url": "new",
    }
    assert persisted["auth"]["api_keys"] == ["must-not-change"]
    manager.load.assert_called_once()


@pytest.mark.asyncio
async def test_rag_document_import_is_idempotent_and_deletion_cleans_both_stores():
    class _Vector(list):
        def tolist(self):
            return list(self)

    model = MagicMock()
    model.encode.return_value = [_Vector([0.1, 0.2]), _Vector([0.3, 0.4])]
    qdrant_response = MagicMock()
    qdrant_response.raise_for_status.return_value = None
    qdrant = MagicMock()
    qdrant._http = MagicMock()
    qdrant._http.put = AsyncMock(return_value=qdrant_response)
    qdrant._http.post = AsyncMock(return_value=qdrant_response)
    qdrant.upsert_collection = AsyncMock()
    redis = MagicMock()
    existing = json.dumps({"doc_id": "placeholder"}).encode()
    redis.lrange = AsyncMock(return_value=[b"invalid-json", existing])
    redis.lrem = AsyncMock()
    redis.rpush = AsyncMock()
    state = _state(redis_manager=SimpleNamespace(redis=redis))
    state.qdrant_manager = qdrant
    content = "First paragraph has enough content.\n\nSecond paragraph also has enough content."

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch.object(routes, "_get_embedding_model", return_value=model),
    ):
        imported = await routes.import_rag_document(
            _request(json_body={
                "content": content,
                "filename": "guide.txt",
                "chunk_strategy": "paragraph",
                "chunk_size": 45,
                "chunk_overlap": 0,
            }),
            _auth={},
        )
        doc_id = imported["data"]["doc_id"]
        redis.lrange.return_value = [
            b"bad",
            json.dumps({"doc_id": doc_id, "filename": "guide.txt"}).encode(),
        ]
        listed = await routes.list_rag_documents(_request(), _auth={})
        deleted = await routes.delete_rag_document(_request(), doc_id, _auth={})

    assert imported["data"]["chunk_count"] == 2
    assert imported["data"]["filename"] == "guide.txt"
    assert doc_id.startswith("doc_")
    qdrant.upsert_collection.assert_awaited_once_with(
        name="rag_documents", size=1024, distance="COSINE"
    )
    assert qdrant._http.put.await_count == 2
    first_point = qdrant._http.put.await_args_list[0].kwargs["json"]["points"][0]
    assert first_point["payload"]["document_id"] == doc_id
    assert first_point["payload"]["chunk_text"].startswith("First paragraph")
    assert listed["data"]["documents"] == [{"doc_id": doc_id, "filename": "guide.txt"}]
    assert deleted["data"] == {"doc_id": doc_id, "deleted": True}
    delete_filter = qdrant._http.post.await_args.kwargs["json"]
    assert delete_filter["filter"]["must"][0]["match"]["value"] == doc_id
    redis.lrem.assert_awaited_once()


@pytest.mark.asyncio
async def test_l3_cache_management_reads_filters_updates_and_deletes_real_payloads():
    manager = MagicMock()
    manager.get.return_value = {
        "l3": {
            "default_mode": "manual",
            "default_ttl_hours": 2,
            "auto_cleanup_interval_minutes": 15,
        }
    }
    scheduler = MagicMock()
    qdrant = MagicMock()
    qdrant._http = object()
    qdrant.scroll_points = AsyncMock(return_value={"points": [
        {
            "id": "older",
            "payload": {
                "prompt_normalized": "old prompt",
                "model": "gpt",
                "user_id": "alice",
                "created_at": 1,
                "ttl": 100,
                "management_mode": "auto",
                "hit_count": 2,
                "token_count": 10,
            },
        },
        {
            "id": "popular",
            "payload": {
                "prompt_normalized": "popular prompt",
                "model": "claude",
                "user_id": "alice",
                "created_at": 2,
                "ttl": 0,
                "management_mode": "manual",
                "hit_count": 20,
                "token_count": 30,
            },
        },
    ]})
    qdrant.update_payload = AsyncMock()
    qdrant.delete_points = AsyncMock()
    cache = MagicMock()
    cache.cleanup_expired_l3 = AsyncMock(return_value=4)
    state = _state(config_manager=manager)
    state.qdrant_manager = qdrant
    state.l3_cleanup_scheduler = scheduler
    state.cache_manager = cache

    with patch("aigateway_api.app_state.get_state", return_value=state):
        config = await routes.get_l3_cache_config(_request(), _auth={})
        updated_config = await routes.update_l3_cache_config(
            _request(json_body={
                "default_mode": "auto",
                "auto_cleanup_interval_minutes": 30,
                "default_ttl_hours": 6,
                "ignored": "value",
            }),
            _auth={},
        )
        entries = await routes.list_l3_entries(
            _request(),
            page=1,
            page_size=20,
            mode="auto",
            user_id="alice",
            sort_by="hit_count",
            _auth={},
        )
        manual = await routes.update_entry_mode(
            _request(json_body={"mode": "manual"}),
            "popular",
            _auth={},
        )
        automatic = await routes.update_entry_mode(
            _request(json_body={"mode": "auto", "ttl_hours": 3}),
            "older",
            _auth={},
        )
        removed = await routes.delete_l3_entry(_request(), "older", _auth={})
        cleaned = await routes.trigger_l3_cleanup(_request(), _auth={})

    assert config["data"]["default_mode"] == "manual"
    assert config["data"]["default_ttl_hours"] == 2
    assert updated_config["data"]["auto_cleanup_interval_minutes"] == 30
    scheduler.update_interval.assert_called_once_with(30)
    assert [item["id"] for item in entries["data"]["items"]] == ["popular", "older"]
    assert entries["data"]["items"][0]["expiresAt"] is None
    qdrant.scroll_points.assert_awaited_once_with(
        collection="semantic_cache",
        filter={"must": [
            {"key": "management_mode", "match": {"value": "auto"}},
            {"key": "user_id", "match": {"value": "alice"}},
        ]},
        limit=20,
        with_payload=True,
    )
    assert manual["data"]["ttl"] == 0
    assert automatic["data"]["ttl"] > 0
    assert qdrant.update_payload.await_args_list[0].kwargs["payload"] == {
        "management_mode": "manual",
        "ttl": 0,
    }
    qdrant.delete_points.assert_awaited_once_with(
        collection="semantic_cache", point_ids=["older"]
    )
    assert removed["data"]["deleted"] is True
    assert cleaned["data"]["deleted_count"] == 4


class _HTTPClientContext:
    def __init__(self, response):
        self.response = response
        self.get = AsyncMock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_provider_connectivity_and_models_use_configured_upstream(tmp_path):
    config_path = tmp_path / "config.yaml"
    file_config = {
        "providers": {
            "custom": {
                "api_key": "${CUSTOM_API_KEY}",
                "base_url": "https://provider.test/v1",
            },
        },
    }
    config_path.write_text(yaml.safe_dump(file_config), encoding="utf-8")
    manager = _config_manager(
        config_path,
        {
            "providers": {
                "custom": {
                    "api_key": "secret",
                    "base_url": "https://provider.test/v1",
                },
            },
        },
    )
    state = _state(config_manager=manager)
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    response.json.return_value = {
        "data": [{"id": "z-model"}, {"id": "a-model"}, {"missing": "id"}],
    }
    response.raise_for_status.return_value = None
    clients = []

    def client_factory(*args, **kwargs):
        client = _HTTPClientContext(response)
        clients.append(client)
        return client

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch("httpx.AsyncClient", side_effect=client_factory),
    ):
        connectivity = await routes.test_provider_connectivity(
            _request(), "custom", _auth={}
        )
        models = await routes.get_provider_models(_request(), "custom", _auth={})

    assert connectivity["data"]["success"] is True
    assert models["data"]["models"] == ["a-model", "z-model"]
    for client in clients:
        client.get.assert_awaited_once_with(
            "https://provider.test/v1/models",
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
            },
        )


@pytest.mark.asyncio
async def test_draft_image_workflow_exposes_preview_confirm_result_reject_and_cleanup():
    draft = SimpleNamespace(
        draft_id="draft-1",
        status="pending",
        previews=[b"\x89PNG\r\n\x1a\npreview"],
        generation_params={"prompt": "cat"},
        attempt_number=1,
        max_attempts=5,
        created_at=100.0,
        expires_at=200.0,
        user_id="alice",
        group_id="grp-team",
    )
    replacement = SimpleNamespace(
        draft_id="draft-2", attempt_number=2, max_attempts=5
    )
    upscale = SimpleNamespace(
        output_data=b"\xff\xd8\xffimage",
        target_resolution=(1920, 1080),
        algorithm_used="RealESRGAN",
        duration_ms=12.5,
    )
    strategy = MagicMock()
    strategy.get_draft = AsyncMock(return_value=draft)
    strategy.confirm_draft = AsyncMock(return_value=upscale)
    strategy.get_result_bytes = AsyncMock(return_value=b"RIFF0000WEBPresult")
    strategy.delete_session = AsyncMock(return_value=3)
    strategy.reject_draft = AsyncMock(return_value=replacement)
    record_log = AsyncMock()
    auth = {"user_id": "alice", "group_id": "grp-team"}

    with (
        patch.object(routes, "_get_draft_strategy", return_value=strategy),
        patch("aigateway_api.openai_compat._record_request_log", new=record_log),
    ):
        status = await routes.get_draft_status("draft-1", _auth=auth)
        preview = await routes.get_draft_preview("draft-1", _auth=auth)
        confirmed = await routes.confirm_draft(
            "draft-1", _request(), _auth=auth
        )
        result = await routes.get_draft_result("draft-1", _auth=auth)
        cleaned = await routes.delete_session_drafts("grp-team:session", _auth=auth)
        rejected = await routes.reject_draft("draft-1", _auth=auth)

    assert status == {
        "draft_id": "draft-1",
        "status": "pending",
        "preview_count": 1,
        "generation_params": {"prompt": "cat"},
        "attempt_number": 1,
        "max_attempts": 5,
        "created_at": 100.0,
        "expires_at": 200.0,
        "progress": 0.0,
        "stage": "pending",
        "workflow_version": "",
        "comfy_prompt_id": None,
        "gpu_seconds": 0.0,
        "progress_source": "stage",
    }
    assert preview["preview_data_url"].startswith("data:image/png;base64,")
    assert confirmed["upscaled_url"].startswith("data:image/jpeg;base64,")
    assert confirmed["target_resolution"] == [1920, 1080]
    assert confirmed["algorithm"] == "RealESRGAN"
    assert result["result_data_url"].startswith("data:image/webp;base64,")
    assert cleaned == {"session_id": "grp-team:session", "deleted_count": 3}
    assert rejected["new_draft_id"] == "draft-2"
    assert rejected["preview_url"] == "/admin/draft/draft-2/preview"
    strategy.confirm_draft.assert_awaited_once_with("draft-1")
    strategy.get_result_bytes.assert_awaited_once_with("draft-1")
    strategy.delete_session.assert_awaited_once_with(
        "grp-team:session",
        user_id="alice",
        group_id="grp-team",
    )
    strategy.reject_draft.assert_awaited_once_with("draft-1")
    record_log.assert_awaited_once()
    assert record_log.await_args.kwargs["model"] == "RealESRGAN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (RuntimeError("draft_session_forbidden"), 403),
        (RuntimeError("draft_session_owner_unknown"), 403),
        (RuntimeError("invalid_session_id"), 400),
    ],
)
async def test_delete_session_drafts_fails_closed(error, expected_status):
    strategy = MagicMock()
    strategy.delete_session = AsyncMock(side_effect=error)

    with patch.object(routes, "_get_draft_strategy", return_value=strategy):
        with pytest.raises(HTTPException) as raised:
            await routes.delete_session_drafts(
                "session-1",
                _auth={"user_id": "alice", "group_id": "grp-team"},
            )

    assert raised.value.status_code == expected_status


@pytest.mark.asyncio
async def test_confirm_draft_maps_comfyui_oom_to_retryable_503():
    draft = SimpleNamespace(user_id="alice", group_id="grp-team")
    strategy = MagicMock()
    strategy.get_draft = AsyncMock(return_value=draft)
    strategy.confirm_draft = AsyncMock(
        side_effect=RuntimeError("comfyui_gpu_out_of_memory")
    )

    with patch.object(routes, "_get_draft_strategy", return_value=strategy):
        with pytest.raises(HTTPException) as raised:
            await routes.confirm_draft(
                "draft-1",
                _request(),
                _auth={"user_id": "alice", "group_id": "grp-team"},
            )

    assert raised.value.status_code == 503
    assert raised.value.detail["error"]["code"] == "gpu_out_of_memory"
    assert raised.value.detail["error"]["retryable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft", "expected_status", "expected_code"),
    [
        (None, 404, "draft_not_found"),
        (
            SimpleNamespace(status="generating", previews=[]),
            202,
            "generating",
        ),
        (
            SimpleNamespace(status="failed", previews=[]),
            410,
            "draft_failed",
        ),
        (
            SimpleNamespace(status="pending", previews=[]),
            404,
            "no_preview",
        ),
    ],
)
async def test_draft_preview_reports_each_persisted_state(
    draft, expected_status, expected_code
):
    strategy = MagicMock()
    strategy.get_draft = AsyncMock(return_value=draft)
    # generating/queued/running/refining 状态会触发 sync_draft_runtime_state 回查；
    # 用 AsyncMock 返回同一 draft，避免 MagicMock 不可 await。
    strategy.sync_draft_runtime_state = AsyncMock(return_value=draft)
    with patch.object(routes, "_get_draft_strategy", return_value=strategy):
        if expected_status == 202:
            response = await routes.get_draft_preview("draft-state", _auth={})
            assert response.status_code == 202
            assert json.loads(response.body)["status"] == expected_code
        else:
            with pytest.raises(HTTPException) as caught:
                await routes.get_draft_preview("draft-state", _auth={})
            assert caught.value.status_code == expected_status
            assert caught.value.detail["error"]["code"] == expected_code


@pytest.mark.asyncio
async def test_draft_ownership_is_enforced_for_all_draft_reads_and_actions():
    draft = SimpleNamespace(
        user_id="alice", group_id="grp-team", previews=[]
    )
    strategy = MagicMock()
    strategy.get_draft = AsyncMock(return_value=draft)
    strategy.confirm_draft = AsyncMock()
    strategy.get_result_bytes = AsyncMock()
    strategy.reject_draft = AsyncMock()
    auth = {"user_id": "mallory", "group_id": "grp-other"}

    with patch.object(routes, "_get_draft_strategy", return_value=strategy):
        for operation in (
            lambda: routes.get_draft_status("draft-1", _auth=auth),
            lambda: routes.get_draft_preview("draft-1", _auth=auth),
            lambda: routes.confirm_draft("draft-1", _request(), _auth=auth),
            lambda: routes.get_draft_result("draft-1", _auth=auth),
            lambda: routes.reject_draft("draft-1", _auth=auth),
        ):
            with pytest.raises(HTTPException) as caught:
                await operation()
            assert caught.value.status_code == 403
            assert caught.value.detail["error"]["code"] == "forbidden"

    strategy.confirm_draft.assert_not_awaited()
    strategy.get_result_bytes.assert_not_awaited()
    strategy.reject_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_embedding_uses_configured_provider_and_normalizes_dimensions():
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "embedding": {},
        "providers": {
            "valid": {
                "api_key": "a" * 24,
                "base_url": "https://embedding.test/v1",
            },
            "placeholder": {"api_key": "sk-xxx"},
        },
    }.get(key, default)
    state = _state(config_manager=config)
    embedding = AsyncMock(return_value=SimpleNamespace(data=[
        {"embedding": list(range(1030))},
        {"embedding": [1.0, 2.0]},
    ]))
    fake_litellm = SimpleNamespace(aembedding=embedding)

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch.dict(sys.modules, {"litellm": fake_litellm}),
    ):
        vectors = await routes._compute_embeddings_via_litellm(["one", "two"])

    assert len(vectors) == 2
    assert len(vectors[0]) == 1024
    assert vectors[0][-1] == 1023
    assert len(vectors[1]) == 1024
    assert vectors[1][:2] == [1.0, 2.0]
    assert vectors[1][2:] == [0.0] * 1022
    embedding.assert_awaited_once_with(
        model="openai/text-embedding-3-small",
        input=["one", "two"],
        api_key="a" * 24,
        api_base="https://embedding.test/v1",
    )


@pytest.mark.asyncio
async def test_prometheus_proxy_forwards_instant_and_range_queries(monkeypatch):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": "success", "data": {"result": [1]}}
    clients = []

    def client_factory(*args, **kwargs):
        client = _HTTPClientContext(response)
        clients.append(client)
        return client

    monkeypatch.setenv("AI_GATEWAY_PROMETHEUS_URL", "http://metrics.test/")
    with patch("httpx.AsyncClient", side_effect=client_factory):
        instant = await routes.prometheus_query(
            _request(), query="up", time="123", _auth={}
        )
        ranged = await routes.prometheus_query_range(
            _request(),
            query="rate(requests[5m])",
            start="100",
            end="200",
            step="15",
            _auth={},
        )

    assert instant["status"] == "success"
    assert ranged["data"]["result"] == [1]
    clients[0].get.assert_awaited_once_with(
        "http://metrics.test/api/v1/query",
        params={"query": "up", "time": "123"},
    )
    clients[1].get.assert_awaited_once_with(
        "http://metrics.test/api/v1/query_range",
        params={
            "query": "rate(requests[5m])",
            "start": "100",
            "end": "200",
            "step": "15",
        },
    )


@pytest.mark.asyncio
async def test_provider_error_responses_remain_actionable(tmp_path):
    config_path = tmp_path / "config.yaml"
    initial = {
        "providers": {
            "unknown": {"api_key": "secret"},
            "custom": {
                "api_key": "secret",
                "base_url": "https://provider.test/v1",
            },
        },
    }
    config_path.write_text(yaml.safe_dump(initial), encoding="utf-8")
    manager = _config_manager(config_path, initial)
    state = _state(config_manager=manager)
    rejected = MagicMock()
    rejected.status_code = 401
    rejected.text = "invalid key"
    client = _HTTPClientContext(rejected)

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch("httpx.AsyncClient", return_value=client),
    ):
        no_url = await routes.test_provider_connectivity(
            _request(), "unknown", _auth={}
        )
        unauthorized = await routes.test_provider_connectivity(
            _request(), "custom", _auth={}
        )

    assert no_url["data"] == {
        "provider": "unknown",
        "success": False,
        "latency_ms": 0,
        "error": "No base_url configured for this provider",
    }
    assert unauthorized["data"]["success"] is False
    assert unauthorized["data"]["error"] == "HTTP 401: invalid key"

    with (
        patch("aigateway_api.app_state.get_state", return_value=state),
        patch("httpx.AsyncClient", side_effect=RuntimeError("network down")),
    ):
        failed = await routes.test_provider_connectivity(
            _request(), "custom", _auth={}
        )
    assert failed["data"]["success"] is False
    assert failed["data"]["error"] == "network down"


@pytest.mark.asyncio
async def test_debug_config_endpoint_serializes_all_runtime_dimensions():
    debug = SimpleNamespace(
        frontend=True,
        entry=False,
        cache=True,
        bridge=False,
        plugins_enabled=True,
        per_plugin={"cache": True},
    )
    with patch(
        "aigateway_core.shared.debug_config.get_debug_config",
        return_value=debug,
    ):
        result = await routes.get_debug_config_endpoint(_request(), _auth={})
    assert result["data"] == {
        "frontend": True,
        "entry": False,
        "cache": True,
        "bridge": False,
        "plugins_enabled": True,
        "per_plugin": {"cache": True},
    }
