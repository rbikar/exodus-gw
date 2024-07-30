import gzip
import json
import logging
import uuid
from base64 import b64encode
from datetime import datetime, timezone

import mock

from exodus_gw import models, settings, worker
from exodus_gw.models.path import PublishedPath

NOW_UTC = datetime.now(timezone.utc)


def _task():
    return models.Task(
        id="8d8a4692-c89b-4b57-840f-b3f0166148d2",
        state="NOT_STARTED",
    )


@mock.patch("exodus_gw.worker.deploy.CurrentMessage.get_current_message")
def test_deploy_config(
    mock_get_message, db, fake_config, caplog, mock_boto3_client
):
    caplog.set_level(logging.DEBUG, logger="exodus-gw")

    # Construct task that would be generated by caller.
    t = _task()

    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(message_id=t.id)

    # Simulate successful write by batch_write.
    mock_boto3_client.batch_write_item.return_value = {"UnprocessedItems": {}}

    db.add(t)
    db.commit()

    # disable cache flush for listings
    updated_settings = settings.Settings()
    updated_settings.cdn_listing_flush = False

    worker.deploy_config(
        fake_config, "test", NOW_UTC, settings=updated_settings
    )

    # It should've created an appropriate put request.
    request = {
        "my-config": [
            {
                "PutRequest": {
                    "Item": {
                        "from_date": {"S": NOW_UTC},
                        "config_id": {"S": "exodus-config"},
                        "config": {
                            "B": b64encode(
                                gzip.compress(json.dumps(fake_config).encode())
                            ).decode()
                        },
                    }
                }
            },
        ]
    }

    # It should've set task state to IN_PROGRESS.
    db.refresh(t)
    assert t.state == "IN_PROGRESS"

    assert "Task %s writing config from %s" % (t.id, NOW_UTC) in caplog.text

    # It should've called batch_write with the expected request.
    mock_boto3_client.batch_write_item.assert_called_with(RequestItems=request)

    # It should've sent task id to complete_deploy_config_task.
    messages = db.query(models.DramatiqMessage).all()

    assert len(messages) == 1

    msg = messages[0]
    body = msg.body

    assert (
        "Sent task %s for completion via message %s" % (t.id, msg.id)
        in caplog.text
    )

    # It should've sent message with this actor & kwargs.
    assert msg.actor == "complete_deploy_config_task"
    assert body["kwargs"]["task_id"] == str(t.id)
    assert body["kwargs"]["env"] == "test"
    assert body["kwargs"]["flush_paths"] == []

    # And actor call should have been delayed by this long.
    delay = body["options"]["eta"] - body["message_timestamp"]
    assert abs(delay - 120000) < 1000


@mock.patch("exodus_gw.worker.deploy.CurrentMessage.get_current_message")
def test_deploy_config_with_flush(
    mock_get_message, db, fake_config, caplog, mock_boto3_client
):
    caplog.set_level(logging.DEBUG, logger="exodus-gw")

    # Construct task that would be generated by caller.
    t = _task()

    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(message_id=t.id)

    # Simulate successful write by batch_write.
    mock_boto3_client.batch_write_item.return_value = {"UnprocessedItems": {}}

    db.add(t)

    # Add some published paths to the DB, will be looked up due to alias update.
    db.add(
        PublishedPath(
            env="test",
            web_uri="/content/testproduct/1/file1",
            updated=datetime.now(tz=timezone.utc),
        )
    )
    db.add(
        PublishedPath(
            env="test",
            web_uri="/content/testproduct/1/file2",
            updated=datetime.now(tz=timezone.utc),
        )
    )
    db.add(
        PublishedPath(
            env="test",
            web_uri="/content/testproduct/1.1.0/file3",
            updated=datetime.now(tz=timezone.utc),
        )
    )

    db.commit()

    # We're updating the alias in the config.
    updated_config = json.loads(json.dumps(fake_config))
    updated_config["releasever_alias"] = [
        {
            "dest": "/content/testproduct/1.2.0",
            "src": "/content/testproduct/1",
        },
    ]
    worker.deploy_config(updated_config, "test", NOW_UTC)

    # It should've created an appropriate put request.
    request = {
        "my-config": [
            {
                "PutRequest": {
                    "Item": {
                        "from_date": {"S": NOW_UTC},
                        "config_id": {"S": "exodus-config"},
                        "config": {
                            "B": b64encode(
                                gzip.compress(
                                    json.dumps(updated_config).encode()
                                )
                            ).decode()
                        },
                    }
                }
            },
        ]
    }

    # It should've set task state to IN_PROGRESS.
    db.refresh(t)
    assert t.state == "IN_PROGRESS"

    assert "Task %s writing config from %s" % (t.id, NOW_UTC) in caplog.text

    # It should've called batch_write with the expected request.
    mock_boto3_client.batch_write_item.assert_called_with(RequestItems=request)

    # It should've sent task id to complete_deploy_config_task.
    messages = db.query(models.DramatiqMessage).all()

    assert len(messages) == 1

    msg = messages[0]
    body = msg.body

    assert (
        "Sent task %s for completion via message %s" % (t.id, msg.id)
        in caplog.text
    )

    # It should've sent message with this actor & kwargs.
    assert msg.actor == "complete_deploy_config_task"
    assert body["kwargs"]["task_id"] == str(t.id)
    assert body["kwargs"]["env"] == "test"
    assert body["kwargs"]["flush_paths"] == [
        # It figured out that cache will need to be flushed for these.
        "/content/dist/rhel/server/8/listing",
        "/content/dist/rhel/server/listing",
        "/content/testproduct/1/file1",
        "/content/testproduct/1/file2",
    ]

    # And actor call should have been delayed by this long.
    delay = body["options"]["eta"] - body["message_timestamp"]
    assert abs(delay - 120000) < 1000


@mock.patch("exodus_gw.worker.deploy.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.deploy.DynamoDB.batch_write")
def test_deploy_config_exception(
    mock_batch_write,
    mock_get_message,
    db,
    fake_config,
    caplog,
    mock_boto3_client,
):
    caplog.set_level(logging.INFO, logger="exodus-gw")

    # Construct task that would be generated by caller.
    t = _task()

    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(message_id=t.id)

    # Simulate failed batch_write.
    mock_batch_write.side_effect = RuntimeError()

    db.add(t)
    db.commit()

    worker.deploy_config(fake_config, "test", NOW_UTC)

    # It should've set task state to FAILED.
    db.refresh(t)
    assert t.state == "FAILED"

    assert "Task %s writing config from %s" % (t.id, NOW_UTC) in caplog.text
    assert "Task %s encountered an error" % t.id in caplog.text


@mock.patch("exodus_gw.worker.deploy.CurrentMessage.get_current_message")
@mock.patch("exodus_gw.worker.deploy.DynamoDB.batch_write")
def test_deploy_config_bad_state(
    mock_batch_write,
    mock_get_message,
    db,
    fake_config,
    caplog,
    mock_boto3_client,
):
    # Construct task that would be generated by caller.
    t = _task()

    # Construct dramatiq message that would be generated by caller.
    mock_get_message.return_value = mock.MagicMock(message_id=t.id)

    db.add(t)
    # Simulate prior completion of task.
    t.state = "COMPLETE"
    db.commit()

    worker.deploy_config(fake_config, "test", NOW_UTC)

    # It shouldn't have called batch_write.
    mock_batch_write.assert_not_called()

    # It should've logged a warning message.
    assert "Task %s in unexpected state, 'COMPLETE'" % t.id in caplog.text


def test_complete_deploy_config_task(db, caplog):
    caplog.set_level(logging.INFO, logger="exodus-gw")

    # Construct task that would be generated by caller.
    t = _task()
    t.state = "IN_PROGRESS"

    db.add(t)
    db.commit()

    worker.deploy.complete_deploy_config_task(t.id)

    # It should've set task state to COMPLETE.
    db.refresh(t)
    assert t.state == "COMPLETE"


def test_complete_deploy_config_task_with_flush(db, caplog):
    caplog.set_level(logging.INFO, logger="exodus-gw")

    # Construct task that would be generated by caller.
    t = _task()
    t.state = "IN_PROGRESS"

    db.add(t)
    db.commit()

    with mock.patch("exodus_gw.worker.deploy.Flusher") as mock_flusher:
        worker.deploy.complete_deploy_config_task(
            t.id, env="test", flush_paths=["/some/path1", "/some/path2"]
        )

    # It should've set task state to COMPLETE.
    db.refresh(t)
    assert t.state == "COMPLETE"

    # It should've used Flusher to flush those paths.
    mock_flusher.assert_called_once_with(
        paths=["/some/path1", "/some/path2"],
        env="test",
        aliases=[],
        settings=mock.ANY,
    )


def test_complete_deploy_config_task_bad_state(db, caplog):
    caplog.set_level(logging.INFO, logger="exodus-gw")

    # Simulate direct call which leaves state as NOT_STARTED.
    t = _task()

    db.add(t)
    db.commit()

    worker.deploy.complete_deploy_config_task(t.id)

    # It should've logged a warning message.
    assert "Task %s in unexpected state, 'NOT_STARTED'" % t.id in caplog.text

    # It shouldn't alter the task's state.
    db.refresh(t)
    assert t.state == "NOT_STARTED"
