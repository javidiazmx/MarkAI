"""Saving a photo against a log entry: real images only, re-encoded, EXIF stripped, and
never readable outside the attachments folder it was written under."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from markai.web.photos import MAX_UPLOAD_BYTES, PhotoError, delete_photo, photo_file, save_photo


def _jpeg_bytes(size=(50, 50), color=(200, 50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def test_a_real_photo_is_saved_and_reads_back(tmp_path):
    raw = _jpeg_bytes()
    relative = save_photo(tmp_path, "account:abc", "entry-1", raw)
    assert relative.startswith("account_abc/")
    path = photo_file(tmp_path, relative)
    assert path is not None and path.is_file()
    with Image.open(path) as saved:
        assert saved.format == "JPEG"


def test_a_huge_photo_is_shrunk_to_the_max_dimension(tmp_path):
    raw = _jpeg_bytes(size=(4000, 3000))
    relative = save_photo(tmp_path, "account:abc", "entry-1", raw)
    with Image.open(photo_file(tmp_path, relative)) as saved:
        assert max(saved.size) <= 1600


def test_exif_does_not_survive_the_re_encode(tmp_path):
    buf = io.BytesIO()
    img = Image.new("RGB", (20, 20))
    exif = img.getexif()
    exif[271] = "TotallyRealCameraCo"  # a GPS-adjacent example tag would be 34853
    img.save(buf, format="JPEG", exif=exif)
    relative = save_photo(tmp_path, "account:abc", "entry-1", buf.getvalue())
    with Image.open(photo_file(tmp_path, relative)) as saved:
        assert not saved.getexif()


def test_not_actually_an_image_is_refused(tmp_path):
    with pytest.raises(PhotoError):
        save_photo(tmp_path, "account:abc", "entry-1", b"not a photo, just text")


def test_an_empty_upload_is_refused(tmp_path):
    with pytest.raises(PhotoError):
        save_photo(tmp_path, "account:abc", "entry-1", b"")


def test_an_oversized_upload_is_refused(tmp_path):
    with pytest.raises(PhotoError):
        save_photo(tmp_path, "account:abc", "entry-1", b"x" * (MAX_UPLOAD_BYTES + 1))


def test_one_owners_path_cannot_escape_the_attachments_folder(tmp_path):
    """Even though a real photo_path is only ever produced by save_photo, reading one back
    trusts nothing - a tampered value trying to walk out of attachments/ must fail closed."""
    assert photo_file(tmp_path, "../../etc/passwd") is None
    assert photo_file(tmp_path, "account_abc/../../../etc/passwd") is None
    assert photo_file(tmp_path, "") is None


def test_deleting_a_photo_removes_the_file(tmp_path):
    relative = save_photo(tmp_path, "account:abc", "entry-1", _jpeg_bytes())
    path = photo_file(tmp_path, relative)
    assert path.is_file()
    delete_photo(tmp_path, relative)
    assert not path.is_file()


def test_deleting_a_missing_photo_does_not_raise(tmp_path):
    delete_photo(tmp_path, "account_abc/does-not-exist.jpg")
