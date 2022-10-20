import uuid

import mock
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from exodus_gw import routers, schemas
from exodus_gw.main import app
from exodus_gw.models import Item, Publish, Task
from exodus_gw.settings import Environment, Settings, get_environment


@pytest.mark.parametrize(
    "env",
    [
        "test",
        "test2",
        "test3",
    ],
)
def test_publish_env_exists(env, db, auth_header):
    with TestClient(app) as client:
        r = client.post(
            "/%s/publish" % env,
            headers=auth_header(roles=["%s-publisher" % env]),
        )

    # Should succeed
    assert r.ok

    # Should have returned a publish object
    publish_id = r.json()["id"]

    publishes = db.query(Publish).filter(Publish.id == publish_id)
    assert publishes.count() == 1


def test_publish_env_doesnt_exist(auth_header):
    with TestClient(app) as client:
        r = client.post(
            "/foo/publish", headers=auth_header(roles=["foo-publisher"])
        )

    # It should fail
    assert r.status_code == 404

    # It should mention that it was a bad environment
    assert r.json() == {"detail": "Invalid environment='foo'"}


def test_publish_links(mock_db_session):
    publish = routers.publish.publish(
        env=Environment(
            "test",
            "some-profile",
            "some-bucket",
            "some-table",
            "some-config-table",
            "some/test/url",
            "a12c3b4fe56",
        ),
        db=mock_db_session,
    )

    # The schema (realistic result) of the publish
    # should contain accurate links.
    assert schemas.Publish(**publish.__dict__).links == {
        "self": "/test/publish/%s" % publish.id,
        "commit": "/test/publish/%s/commit" % publish.id,
    }


def test_update_publish_items_typical(db, auth_header):
    """PUTting some items on a publish creates expected objects in DB."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # Ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add some items to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "/uri1",
                    "object_key": "1" * 64,
                    "content_type": "application/octet-stream",
                },
                {
                    "web_uri": "/uri2",
                    "object_key": "2" * 64,
                    "content_type": "application/octet-stream",
                },
                {
                    "web_uri": "/uri3",
                    "link_to": "/uri1",
                },
                {
                    "web_uri": "/uri4",
                    "object_key": "absent",
                },
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    # It should have succeeded
    assert r.ok

    # Publish object should now have matching items
    db.refresh(publish)

    items = sorted(publish.items, key=lambda item: item.web_uri)
    item_dicts = [
        {
            "web_uri": item.web_uri,
            "object_key": item.object_key,
            "content_type": item.content_type,
            "link_to": item.link_to,
        }
        for item in items
    ]

    # Should have stored exactly what we asked for
    assert item_dicts == [
        {
            "web_uri": "/uri1",
            "object_key": "1" * 64,
            "content_type": "application/octet-stream",
            "link_to": "",
        },
        {
            "web_uri": "/uri2",
            "object_key": "2" * 64,
            "content_type": "application/octet-stream",
            "link_to": "",
        },
        {
            "web_uri": "/uri3",
            "object_key": "",
            "content_type": "",
            "link_to": "/uri1",
        },
        {
            "web_uri": "/uri4",
            "object_key": "absent",
            "content_type": "",
            "link_to": "",
        },
    ]


def test_update_publish_items_path_normalization(db, auth_header):
    """URI and link target paths are normalized in PUT items."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # Ensure a publish object exists
        db.add(publish)
        db.commit()

        # Add an item to it with some messy paths
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {"web_uri": "some/path", "object_key": "1" * 64},
                {"web_uri": "link/to/some/path", "link_to": "/some/path"},
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    # It should have succeeded
    assert r.ok

    # Publish object should now have matching items
    db.refresh(publish)

    item_dicts = [
        {
            "web_uri": item.web_uri,
            "object_key": item.object_key,
            "link_to": item.link_to,
        }
        for item in publish.items
    ]

    # Should have stored normalized web_uri and link_to paths
    assert item_dicts == [
        {"web_uri": "/some/path", "object_key": "1" * 64, "link_to": ""},
        {
            "web_uri": "/link/to/some/path",
            "object_key": "",
            "link_to": "/some/path",
        },
    ]


def test_update_publish_items_invalid_publish(db, auth_header):
    """PUTting items on a completed publish fails with code 409."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="COMPLETE"
    )

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add some items to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "/uri1",
                    "object_key": "1" * 64,
                },
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    # It should have failed with 409
    assert r.status_code == 409
    assert r.json() == {
        "detail": "Publish %s in unexpected state, 'COMPLETE'" % publish_id
    }


def test_update_publish_items_no_uri(db, auth_header):
    """PUTting an item with no web_uri fails validation."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add an item to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "",
                    "link_to": "/uri1",
                },
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    expected_item = {
        "web_uri": "",
        "object_key": "",
        "content_type": "",
        "link_to": "/uri1",
    }

    # It should have failed with 400
    assert r.status_code == 400
    assert r.json() == {"detail": ["No URI: %s" % expected_item]}


def test_update_publish_items_invalid_item(db, auth_header):
    """PUTting an item without object_key or link_to fails validation."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add an item to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[{"web_uri": "/uri1"}],
            headers=auth_header(roles=["test-publisher"]),
        )

    expected_item = {
        "web_uri": "/uri1",
        "object_key": "",
        "content_type": "",
        "link_to": "",
    }

    # It should have failed with 400
    assert r.status_code == 400
    assert r.json() == {
        "detail": ["No object key or link target: %s" % expected_item]
    }


def test_update_publish_items_rejects_autoindex(db, auth_header):
    """PUTting an item explicitly using the autoindex filename fails validation."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add an item to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "/foo/bar/.__exodus_autoindex",
                    "object_key": "1" * 64,
                }
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    # It should have failed with 400
    assert r.status_code == 400

    # It should tell the reason why
    assert r.json() == {
        "detail": [
            "Invalid URI /foo/bar/.__exodus_autoindex: filename is reserved"
        ]
    }


def test_update_publish_items_link_and_key(db, auth_header):
    """PUTting an item with both link_to and object_key fails validation."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add an item to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "/uri2",
                    "object_key": "1" * 64,
                    "link_to": "/uri1",
                },
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    expected_item = {
        "web_uri": "/uri2",
        "object_key": "1" * 64,
        "content_type": "",
        "link_to": "/uri1",
    }

    # It should have failed with 400
    assert r.status_code == 400
    assert r.json() == {
        "detail": [
            "Both link target and object key present: %s" % expected_item
        ]
    }


def test_update_publish_items_link_content_type(db, auth_header):
    """PUTting an item with link_to and content_type fails validation."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add an item to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "/uri2",
                    "link_to": "/uri1",
                    "content_type": "application/octet-stream",
                },
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    expected_item = {
        "web_uri": "/uri2",
        "object_key": "",
        "content_type": "application/octet-stream",
        "link_to": "/uri1",
    }
    # It should have failed with 400
    assert r.status_code == 400
    assert r.json() == {
        "detail": ["Content type specified for link: %s" % expected_item]
    }


def test_update_publish_items_invalid_object_key(db, auth_header):
    """PUTting an item with an non-sha256sum object_key fails validation."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add an item to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "/uri2",
                    "object_key": "somethingshyof64_with!non-alphanum$",
                },
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    expected_item = {
        "web_uri": "/uri2",
        "object_key": "somethingshyof64_with!non-alphanum$",
        "content_type": "",
        "link_to": "",
    }

    # It should have failed with 400
    assert r.status_code == 400
    assert r.json() == {
        "detail": ["Invalid object key; must be sha256sum: %s" % expected_item]
    }


def test_update_publish_absent_items_with_content_type(db, auth_header):
    """PUTting an absent item with a content type fails validation."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add an item to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "/uri1",
                    "object_key": "absent",
                    "content_type": "application/octet-stream",
                },
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    expected_item = {
        "web_uri": "/uri1",
        "object_key": "absent",
        "content_type": "application/octet-stream",
        "link_to": "",
    }

    # It should have failed with 400
    assert r.status_code == 400
    assert r.json() == {
        "detail": [
            "Cannot set content type when object_key is 'absent': %s"
            % expected_item
        ]
    }


def test_update_publish_items_invalid_content_type(db, auth_header):
    """PUTting an item with a non-MIME type content type fails validation."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to add an item to it
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "/uri2",
                    "object_key": "1" * 64,
                    "content_type": "type_nosubtype",
                },
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    expected_item = {
        "web_uri": "/uri2",
        "object_key": "1" * 64,
        "content_type": "type_nosubtype",
        "link_to": "",
    }

    # It should have failed with 400
    assert r.status_code == 400
    assert r.json() == {"detail": ["Invalid content type: %s" % expected_item]}


def test_update_publish_items_no_publish(auth_header):
    publish_id = "11224567-e89b-12d3-a456-426614174000"
    with TestClient(app) as client:
        # Try to add an item to non-existent publish
        r = client.put(
            "/test/publish/%s" % publish_id,
            json=[
                {
                    "web_uri": "/uri2",
                    "object_key": "1" * 64,
                    "content_type": "text/plain",
                },
            ],
            headers=auth_header(roles=["test-publisher"]),
        )

    assert r.status_code == 404
    assert r.json() == {"detail": "No publish found for ID %s" % publish_id}


@pytest.mark.parametrize(
    "deadline",
    [None, "2022-07-25T15:47:47Z"],
    ids=["typical", "with deadline"],
)
def test_commit_publish(deadline, auth_header, db):
    """Ensure commit_publish delegates to worker and creates task."""

    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    url = "/test/publish/11224567-e89b-12d3-a456-426614174000/commit"
    if deadline:
        url += "?deadline=%s" % deadline

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to commit it
        r = client.post(url, headers=auth_header(roles=["test-publisher"]))

    # It should have succeeded
    assert r.ok

    # It should return an appropriate task object
    json_r = r.json()
    assert json_r["links"]["self"] == "/task/%s" % json_r["id"]
    assert json_r["publish_id"] == "11224567-e89b-12d3-a456-426614174000"
    if deadline:
        # 'Z' suffix is dropped when stored as datetime in the database
        assert json_r["deadline"] == "2022-07-25T15:47:47"


def test_commit_publish_bad_deadline(auth_header, db):
    publish_id = "11224567-e89b-12d3-a456-426614174000"

    publish = Publish(
        id=uuid.UUID("{%s}" % publish_id), env="test", state="PENDING"
    )

    url = "/test/publish/11224567-e89b-12d3-a456-426614174000/commit"
    url += "?deadline=07/25/2022 3:47:47 PM"

    with TestClient(app) as client:
        # ensure a publish object exists
        db.add(publish)
        db.commit()

        # Try to commit it
        r = client.post(url, headers=auth_header(roles=["test-publisher"]))

    assert r.status_code == 400
    assert r.json()["detail"] == (
        "ValueError(\"time data '07/25/2022 3:47:47 PM' does not match "
        "format '%Y-%m-%dT%H:%M:%SZ'\")"
    )


@mock.patch("exodus_gw.worker.commit")
def test_commit_publish_prev_completed(mock_commit, fake_publish, db):
    """Ensure commit_publish fails for publishes in invalid state."""

    db.add(fake_publish)
    # Simulate that this publish was published.
    fake_publish.state = schemas.PublishStates.committed
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        routers.publish.commit_publish(
            env=get_environment("test"),
            publish_id=fake_publish.id,
            db=db,
            settings=Settings(),
        )

    assert exc_info.value.status_code == 409
    assert (
        exc_info.value.detail
        == "Publish %s in unexpected state, 'COMMITTED'" % fake_publish.id
    )

    mock_commit.assert_not_called()


@mock.patch("exodus_gw.worker.commit")
def test_commit_publish_linked_items(mock_commit, fake_publish, db):
    """Ensure commit_publish correctly resolves links."""

    # Whole items
    item1 = Item(
        web_uri="/some/path",
        object_key="1" * 64,
        publish_id=fake_publish.id,
        link_to=None,  # It should be able to handle None/NULL link_to values...
        content_type="some type",
    )
    item2 = Item(
        web_uri="/another/path",
        object_key="2" * 64,
        publish_id=fake_publish.id,
        link_to="",  # ...and empty string link_to values...
        content_type="another type",
    )
    item3 = Item(
        web_uri="/some/different/path",
        object_key="3" * 64,
        publish_id=fake_publish.id,
    )
    # Linked items
    ln_item1 = Item(
        web_uri="/alternate/route/to/some/path",
        link_to="/some/path",
        publish_id=fake_publish.id,
    )
    ln_item2 = Item(
        web_uri="/alternate/route/to/another/path",
        link_to="/another/path",
        publish_id=fake_publish.id,
    )
    fake_publish.items.extend([item1, item2, item3, ln_item1, ln_item2])

    db.add(fake_publish)
    db.commit()

    publish_task = routers.publish.commit_publish(
        env=get_environment("test"),
        publish_id=fake_publish.id,
        db=db,
        settings=Settings(),
    )

    # Should've filled ln_item1's object_key with that of item1.
    assert ln_item1.object_key == "1" * 64
    # Should've filled ln_item2's object_key with that of item2.
    assert ln_item2.object_key == "2" * 64

    # Should've filled ln_item1's content_type with that of item1.
    assert ln_item1.content_type == "some type"
    # Should've filled ln_item2's content_type with that of item2.
    assert ln_item2.content_type == "another type"

    # Should've created and sent task.
    assert isinstance(publish_task, Task)

    mock_commit.assert_has_calls(
        calls=[
            mock.call.send(
                publish_id="123e4567-e89b-12d3-a456-426614174000",
                env="test",
                from_date=mock.ANY,
            )
        ],
    )


@mock.patch("exodus_gw.worker.commit")
def test_commit_publish_unresolved_links(mock_commit, fake_publish, db):
    """Ensure commit_publish raises for unresolved links."""

    # Add an item with link to a non-existent item.
    ln_item = Item(
        web_uri="/alternate/route/to/bad/path",
        object_key="",
        link_to="/bad/path",
        publish_id=fake_publish.id,
    )
    fake_publish.items.append(ln_item)

    db.add(fake_publish)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        routers.publish.commit_publish(
            env=get_environment("test"),
            publish_id=fake_publish.id,
            db=db,
            settings=Settings(),
        )

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.detail
        == "Unable to resolve item object_key:\n\tURI: '%s'\n\tLink: '%s'"
        % (ln_item.web_uri, ln_item.link_to)
    )

    mock_commit.assert_not_called()


def test_commit_no_publish(auth_header):
    publish_id = "11224567-e89b-12d3-a456-426614174000"
    url = "/test/publish/%s/commit" % publish_id
    with TestClient(app) as client:
        # Try to commit non-existent publish
        r = client.post(url, headers=auth_header(roles=["test-publisher"]))

    assert r.status_code == 404
    assert r.json() == {"detail": "No publish found for ID %s" % publish_id}
