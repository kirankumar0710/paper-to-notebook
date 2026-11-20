"""Tests for the shared utility helpers."""

from util.files import save_unique
from util.text_utils import sanitize_filename


class TestSanitizeFilename:
    def test_removes_unsafe_characters(self):
        assert sanitize_filename('A/B: "C"?', ".txt") == "AB C.txt"

    def test_truncates(self):
        assert sanitize_filename("x" * 100, ".txt", max_length=5) == (
            "xxxxx.txt"
        )

    def test_uses_default_when_empty(self):
        assert sanitize_filename("???", ".ipynb", default="nb") == "nb.ipynb"

    def test_drops_non_ascii_letters(self):
        # HTTP headers must be Latin-1, so non-ASCII titles are dropped.
        assert sanitize_filename("Über 注意力 Net", ".ipynb") == (
            "ber  Net.ipynb"
        )

    def test_keeps_dots_inside_name(self):
        assert sanitize_filename("2403.03507v2", ".ipynb") == (
            "2403.03507v2.ipynb"
        )

    def test_strips_leading_and_trailing_dots(self):
        assert sanitize_filename("..", ".ipynb", default="nb") == "nb.ipynb"
        assert sanitize_filename(".hidden.", ".ipynb") == "hidden.ipynb"
        assert sanitize_filename("../../etc/passwd", ".x") == "etcpasswd.x"


class TestSaveUnique:
    def test_writes_file_and_creates_folder(self, tmp_path):
        path = save_unique(tmp_path / "new", "a.ipynb", "{}")
        assert path == tmp_path / "new" / "a.ipynb"
        assert path.read_text() == "{}"

    def test_never_overwrites(self, tmp_path):
        first = save_unique(tmp_path, "a.ipynb", "1")
        second = save_unique(tmp_path, "a.ipynb", "2")
        third = save_unique(tmp_path, "a.ipynb", "3")
        assert [p.name for p in (first, second, third)] == [
            "a.ipynb",
            "a (1).ipynb",
            "a (2).ipynb",
        ]
        assert first.read_text() == "1"
